"""Offline billing/report checks; no real Azure credentials or HTTP requests."""
import ast
import copy
import inspect
import json
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import requests
from azure.core.exceptions import ClientAuthenticationError

from cloudlayer import azure_cost
from scripts import cost_report as report
from src import config

NOW = datetime(2026, 9, 24, 12, 0, 1, tzinfo=timezone.utc)
END = date(2026, 9, 24)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    fail = Mock(side_effect=AssertionError("Unexpected real boundary"))
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(azure_cost, "AzureCliCredential", fail)
    monkeypatch.setattr(azure_cost.requests, "post", fail)


@pytest.fixture
def cfg(tmp_path):
    return config.Config(
        provider="azure", project_id=azure_cost.GROUP, region="malaysiawest",
        blob_uri="", container_registry="", mlflow_tracking_uri="",
        model_registry_name="", identity_ref="", reports_dir=tmp_path,
        azure_subscription_id=azure_cost.SUBSCRIPTION, azure_ml_workspace="mlw-itcs355-u6688124",
    )


def page(rows):
    return {"columns": [{"name": name} for name in
                        ("PreTaxCost", "UsageDate", "ResourceId", "Currency")],
            "rows": rows, "nextLink": None}


def resource(cfg, label):
    return next(rid for rid, (name, _) in azure_cost.reviewed_resources(cfg).items() if name == label)


@pytest.fixture
def billing(cfg):
    # Same values as the read-only snapshot retrieved on 2026-09-24 12:00:01 UTC.
    rows = []
    for day, acr, storage in [
        (20260921, "0.1666013328", "0.00000252"),
        (20260922, "0.1666013328", "0.00000216"),
        (20260923, "0.1666013328", "0.000009"),
        (20260924, "0.0416503332", "0.0000702"),
    ]:
        rows += [[acr, day, resource(cfg, "ACR"), "USD"],
                 ["0.0", day, resource(cfg, "Log Analytics"), "USD"],
                 [storage, day, resource(cfg, "Storage"), "USD"]]
    return page(rows)


def test_snapshot_sums_preserve_unknown_direct_cost_and_reported_zero(cfg, billing):
    content = report.render_report(cfg, [billing], END, NOW)
    assert "| ACR | Shared | 0.5414543316 |" in content
    assert "| Storage | Shared | 0.00008388 |" in content
    assert "| Log Analytics | Shared | 0 |" in content
    assert "| Container App | Lab 3 | No cost rows reported |" in content
    assert "Lab 3 resources reported subtotal: **No cost rows reported; not assumed zero**" in content
    assert "Shared resources reported subtotal: **USD 0.5415382116**" in content
    assert "All reported rows in this resource group: **USD 0.5415382116**" in content
    assert "not a measured Lab 3-only cost" in content
    assert "Task 5 retail estimates are not added" in content
    assert "not remaining student credit or a final bill" in content
    assert "2026-09-24T12:00:01Z" in content
    assert "https://learn.microsoft.com/en-us/azure/cost-management-billing/" in content


def test_resource_classification_matches_reviewed_inventory(cfg):
    resources = azure_cost.reviewed_resources(cfg)
    assert len(resources) == 10
    assert {name for name, kind in resources.values() if kind == "Lab 3"} == {
        "Container App", "Environment", "Managed identity"}
    assert sum(kind == "Shared" for _, kind in resources.values()) == 7


@pytest.mark.parametrize("amount,subtotal,total", [
    ("0", "0", "0.5415382116"),
    ("0.12", "0.12", "0.6615382116"),
    ("-0.01", "-0.01", "0.5315382116"),
])
def test_reported_direct_cost_or_credit_is_not_missing(cfg, billing, amount, subtotal, total):
    billing["rows"].append([amount, 20260923, resource(cfg, "Container App"), "USD"])
    content = report.render_report(cfg, [billing], END, NOW)
    assert f"Lab 3 resources reported subtotal: **USD {subtotal}**" in content
    assert f"All reported rows in this resource group: **USD {total}**" in content
    assert "| Environment | Lab 3 | No cost rows reported |" in content


def test_only_direct_rows_do_not_invent_a_shared_zero(cfg):
    content = report.render_report(
        cfg, [page([["0.2", 20260921, resource(cfg, "Environment"), "USD"]])], END, NOW)
    assert "Shared resources reported subtotal: **No cost rows reported; not assumed zero**" in content


def test_column_order_case_multiple_pages_and_extra_columns(cfg, billing):
    first = page(copy.deepcopy(billing["rows"][:6]))
    second = page(copy.deepcopy(billing["rows"][6:]))
    first["nextLink"] = "already fetched"
    second["rows"][0][2] = second["rows"][0][2].upper()
    second["columns"].append({"name": "AdditionalColumn"})
    second["rows"] = [row + ["ignored field"] for row in second["rows"]]
    second["columns"].reverse()
    second["rows"] = [list(reversed(row)) for row in second["rows"]]
    content = report.render_report(cfg, [first, second], END, NOW)
    assert "USD 0.5415382116" in content
    assert "all 2 response page(s) read" in content


@pytest.mark.parametrize("column,value", [
    (3, "THB"), (3, None), (0, "NaN"), (0, "Infinity"), (0, "-Infinity"),
    (0, "unknown"), (0, True), (1, 20260920), (1, 20260925), (1, "bad-date"),
    (2, "/unknown/resource"), (2, None),
])
def test_invalid_row_is_rejected(cfg, billing, column, value):
    billing["rows"][0][column] = value
    with pytest.raises(ValueError):
        report.render_report(cfg, [billing], END, NOW)


@pytest.mark.parametrize("fault", [
    "duplicate", "duplicate-across-pages", "duplicate-column", "missing-column",
    "short-row", "row-object", "empty", "pending-page", "no-pages",
    "not-a-page", "columns-type", "rows-type",
])
def test_incomplete_or_duplicate_data_is_rejected(cfg, billing, fault):
    pages = [billing]
    if fault == "duplicate":
        billing["rows"].append(billing["rows"][0].copy())
    elif fault == "duplicate-across-pages":
        pages.append(page([billing["rows"][0].copy()]))
    elif fault == "duplicate-column":
        billing["columns"][0]["name"] = "Currency"
    elif fault == "missing-column":
        billing["columns"][0]["name"] = "WrongName"
    elif fault == "short-row":
        billing["rows"][0].pop()
    elif fault == "row-object":
        billing["rows"][0] = {}
    elif fault == "empty":
        billing["rows"] = []
    elif fault == "pending-page":
        billing["nextLink"] = "not fetched yet"
    elif fault == "no-pages":
        pages = []
    elif fault == "not-a-page":
        pages = [None]
    elif fault == "columns-type":
        billing["columns"] = None
    else:
        billing["rows"] = None
    with pytest.raises(ValueError):
        report.render_report(cfg, pages, END, NOW)


@pytest.fixture
def http(monkeypatch):
    manager = MagicMock()
    manager.__enter__.return_value.get_token.return_value = SimpleNamespace(token="test-token")
    monkeypatch.setattr(azure_cost, "AzureCliCredential", Mock(return_value=manager))
    post = Mock()
    monkeypatch.setattr(azure_cost.requests, "post", post)
    return post


def response(properties, status=200):
    return SimpleNamespace(status_code=status, json=Mock(return_value={"properties": properties}))


def test_query_reads_all_pages_in_fixed_scope_without_tag_filter(cfg, billing, http):
    endpoint = (f"https://management.azure.com{azure_cost.resource_scope(cfg)}"
                "/providers/Microsoft.CostManagement/query")
    first = page(billing["rows"][:6])
    first["nextLink"] = endpoint + "?api-version=2025-03-01&$skiptoken=next"
    second = page(billing["rows"][6:])
    http.side_effect = [response(first), response(second)]
    assert azure_cost.fetch_pages(cfg, END, NOW) == [first, second]
    assert http.call_count == 2
    assert http.call_args_list[1].args == (first["nextLink"],)
    request = http.call_args_list[0]
    assert request.args[0] == endpoint + "?api-version=2025-03-01"
    assert request.kwargs["json"] == {
        "type": "ActualCost", "timeframe": "Custom",
        "timePeriod": {"from": "2026-09-21T00:00:00Z", "to": "2026-09-24T12:00:01Z"},
        "dataset": {"granularity": "Daily",
                    "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
                    "grouping": [{"type": "Dimension", "name": "ResourceId"}]},
    }
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["timeout"] == 45
    assert http.call_args_list[1].kwargs["json"] == request.kwargs["json"]


def test_json_money_is_parsed_as_decimal(cfg, http):
    raw = requests.Response()
    raw.status_code = 200
    raw._content = b'{"properties":{"columns":[],"rows":[[0.5415382116]],"nextLink":null}}'
    http.return_value = raw
    assert azure_cost.fetch_pages(cfg, END, NOW)[0]["rows"][0][0] == Decimal("0.5415382116")


@pytest.mark.parametrize("status", [204, 301, 302, 401, 403, 404, 429, 500])
def test_http_failure_does_not_retry_or_invent_zero(cfg, http, status):
    http.return_value = response({}, status)
    with pytest.raises((ValueError, RuntimeError)):
        azure_cost.fetch_pages(cfg, END, NOW)
    http.assert_called_once()
    http.return_value.json.assert_not_called()


@pytest.mark.parametrize("link", [
    "https://example.com/steal", "http://management.azure.com/other", "/relative/path",
    "https://management.azure.com/subscriptions/other/providers/Microsoft.CostManagement/query",
    "https://management.azure.com:443/other", "", False, [],
])
def test_invalid_pagination_cannot_forward_token(cfg, http, billing, link):
    billing["nextLink"] = link
    http.return_value = response(billing)
    with pytest.raises(ValueError, match="pagination"):
        azure_cost.fetch_pages(cfg, END, NOW)
    http.assert_called_once()


def test_repeated_pagination_stops(cfg, http, billing):
    billing["nextLink"] = (f"https://management.azure.com{azure_cost.resource_scope(cfg)}"
                          "/providers/Microsoft.CostManagement/query?api-version=2025-03-01")
    http.return_value = response(billing)
    with pytest.raises(ValueError, match="repeated"):
        azure_cost.fetch_pages(cfg, END, NOW)
    http.assert_called_once()


@pytest.mark.parametrize("payload", [[], {}, {"properties": []}])
def test_malformed_payload_stops(cfg, http, payload):
    http.return_value = SimpleNamespace(status_code=200, json=Mock(return_value=payload))
    with pytest.raises(ValueError, match="invalid cost response"):
        azure_cost.fetch_pages(cfg, END, NOW)


def test_auth_failure_does_not_print_credential_details(cfg, monkeypatch, http):
    monkeypatch.setattr(azure_cost, "AzureCliCredential",
                        Mock(side_effect=ClientAuthenticationError("sensitive auth detail")))
    with pytest.raises(RuntimeError, match="authentication failed") as exc:
        azure_cost.fetch_pages(cfg, END, NOW)
    assert "sensitive auth detail" not in str(exc.value)
    http.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("provider", "local"), ("project_id", "other-group"), ("azure_subscription_id", "other-sub"),
])
def test_wrong_scope_stops_before_credentials(cfg, field, value):
    with pytest.raises(ValueError, match="reviewed"):
        azure_cost.fetch_pages(replace(cfg, **{field: value}), END, NOW)
    azure_cost.AzureCliCredential.assert_not_called()


@pytest.mark.parametrize("end,now", [
    (date(2026, 9, 20), NOW), (date(2026, 9, 25), NOW),
    (END, NOW.replace(day=23)), (END, NOW.replace(tzinfo=None)),
    (END, NOW.replace(tzinfo=timezone(timedelta(hours=7)))),
])
def test_wrong_window_stops_before_credentials(cfg, end, now):
    with pytest.raises(ValueError):
        azure_cost.fetch_pages(cfg, end, now)
    azure_cost.AzureCliCredential.assert_not_called()


def test_refresh_keeps_teardown_day_as_end(cfg, billing, http):
    http.return_value = response(billing)
    azure_cost.fetch_pages(cfg, END, NOW.replace(day=27))
    assert http.call_args.kwargs["json"]["timePeriod"]["to"] == "2026-09-24T23:59:59Z"


@pytest.mark.parametrize("failure", [
    RuntimeError("HTTP 429"), requests.Timeout("Timed out"), ValueError("No cost rows reported"),
])
def test_failed_fetch_keeps_previous_report(cfg, monkeypatch, failure, capsys):
    output = cfg.reports_dir / "lab3-cost.md"
    output.write_text("previous verified snapshot")
    monkeypatch.setattr(sys, "argv", ["cost_report", "--end", "2026-09-24"])
    monkeypatch.setattr(report.config, "load", Mock(return_value=cfg))
    monkeypatch.setattr(report, "fetch_pages", Mock(side_effect=failure))
    assert report.main() == 1
    assert output.read_text() == "previous verified snapshot"
    assert "Previous report left unchanged" in capsys.readouterr().err


def test_invalid_data_keeps_previous_report(cfg, monkeypatch):
    output = cfg.reports_dir / "lab3-cost.md"
    output.write_text("previous verified snapshot")
    monkeypatch.setattr(sys, "argv", ["cost_report"])
    monkeypatch.setattr(report.config, "load", Mock(return_value=cfg))
    monkeypatch.setattr(report, "fetch_pages", Mock(return_value=[page([])]))
    assert report.main() == 1
    assert output.read_text() == "previous verified snapshot"


def test_success_default_end_writes_only_one_report(cfg, billing, monkeypatch):
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(day=27)

    monkeypatch.setattr(report, "datetime", Later)
    monkeypatch.setattr(sys, "argv", ["cost_report"])
    monkeypatch.setattr(report.config, "load", Mock(return_value=cfg))
    fetch = Mock(return_value=[billing])
    monkeypatch.setattr(report, "fetch_pages", fetch)
    assert report.main() == 0
    assert fetch.call_args.args[1] == END
    output = cfg.reports_dir / "lab3-cost.md"
    assert list(cfg.reports_dir.iterdir()) == [output]
    assert "USD 0.5415382116" in output.read_text()
    assert "2026-09-24T23:59:59Z" in output.read_text()


def test_replace_failure_preserves_previous_report(cfg, billing, monkeypatch):
    output = cfg.reports_dir / "lab3-cost.md"
    output.write_text("previous verified snapshot")
    monkeypatch.setattr(sys, "argv", ["cost_report"])
    monkeypatch.setattr(report.config, "load", Mock(return_value=cfg))
    monkeypatch.setattr(report, "fetch_pages", Mock(return_value=[billing]))
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("test write failure")))
    assert report.main() == 1
    assert output.read_text() == "previous verified snapshot"
    assert list(cfg.reports_dir.iterdir()) == [output]


@pytest.mark.parametrize("end", ["2026-09-20", "2026-09-25", "2999-01-01", "not-a-date"])
def test_invalid_end_stops_before_fetch(monkeypatch, end):
    monkeypatch.setattr(sys, "argv", ["cost_report", "--end", end])
    fetch = Mock()
    monkeypatch.setattr(report, "fetch_pages", fetch)
    assert report.main() == 1
    fetch.assert_not_called()


@pytest.mark.parametrize("end,code", [("", 0), ("2026-09-24", 0), ("2026-09-24", 7),
                                     ('invalid"; exit 9; "', 0)])
def test_make_only_calls_cost_report_and_propagates_failure(end, code):
    runner = f"import json,sys; print(json.dumps(sys.argv[1:])); sys.exit({code})"
    python = f"{shlex.quote(sys.executable)} -c {shlex.quote(runner)}"
    result = subprocess.run(
        ["make", "--silent", "cost-report", f"PYTHON={python}", f"COST_END={end}"],
        cwd=config.REPO_ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) == (code == 0)
    assert json.loads(result.stdout) == ["-m", "scripts.cost_report", "--end", end]


def test_report_keeps_sdk_imports_and_resource_inventory_in_cloudlayer():
    tree = ast.parse(inspect.getsource(report))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not any(name == "azure" or name.startswith("azure.") for name in imports)
    assert report.fetch_pages is azure_cost.fetch_pages
    assert report.reviewed_resources is azure_cost.reviewed_resources
