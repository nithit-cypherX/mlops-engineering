"""Fake billing responses only; no Azure jobs or network calls."""
import ast
import copy
import inspect
import json
import os
import shlex
import socket
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

import pytest
import requests

from cloudlayer import azure_cost
from scripts import cost_report as report
from src import config

NOW = datetime(2026, 9, 19, 9, 30, tzinfo=timezone.utc)
END = date(2026, 9, 19)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))


@pytest.fixture
def cfg(tmp_path):
    return config.Config(
        provider="azure", project_id="course-rg", region="malaysiawest",
        blob_uri="", container_registry="", mlflow_tracking_uri="",
        model_registry_name="", identity_ref="", reports_dir=tmp_path,
        azure_subscription_id="test-subscription", azure_ml_workspace="course-workspace",
        azure_ml_compute="cpu-lab2",
    )


def page(rows):
    return {"columns": [{"name": name} for name in
                        ("PreTaxCost", "UsageDate", "ResourceId", "Currency")],
            "rows": rows, "nextLink": None}


@pytest.fixture
def billing():
    scope = "/subscriptions/test-subscription/resourceGroups/course-rg/providers/"
    workspace = scope + "Microsoft.MachineLearningServices/workspaces/course-workspace"
    acr = scope + "Microsoft.ContainerRegistry/registries/itcs355u6688124"
    storage = scope + "Microsoft.Storage/storageAccounts/itcs355u6688124"
    return page([
        ["0.0883873352444444", 20260918, workspace, "USD"],
        ["0.1666013328", 20260917, acr, "USD"],
        ["0.1666013328", 20260918, acr, "USD"],
        ["0.0138834444", 20260919, acr, "USD"],
        ["0.0000072", 20260917, storage, "USD"],
        ["0.0001296", 20260918, storage, "USD"],
    ])


def test_snapshot_totals_and_missing_row_labels(cfg, billing):
    content = report.render_report(cfg, [billing], END, NOW)
    assert "Direct reported subtotal: **USD 0.08838734**" in content
    assert "Shared-resource allocation: **USD 0.34722291**" in content
    assert "USD 0.43561025 / about THB 14.33" in content
    assert "1 USD = 32.88749433 THB" in content
    assert "| Key Vault | Direct | No cost rows reported | Not assumed zero |" in content
    assert "Trial estimates are not added again" in content
    assert "not as measured Lab 2-only usage" in content
    assert "not a final budget pass" in content


def test_column_order_case_and_multiple_pages(cfg, billing):
    second = copy.deepcopy(billing)
    second["columns"].reverse()
    second["rows"] = [list(reversed(row)) for row in second["rows"][2:]]
    second["rows"][0][1] = second["rows"][0][1].upper()
    content = report.render_report(cfg, [page(billing["rows"][:2]), second], END, NOW)
    assert "USD 0.43561025 / about THB 14.33" in content
    assert "all 2 response page(s) read" in content


@pytest.mark.parametrize("column,value,message", [
    (3, "THB", "Expected USD"), (0, "NaN", "Non-finite"), (0, "unknown", "Invalid cost"),
    (1, 20260916, "outside"), (1, 20260920, "outside"), (1, "bad-date", "does not match"),
    (2, "/unknown/resource", "Unreviewed"),
])
def test_invalid_billing_rows_stop(cfg, billing, column, value, message):
    billing["rows"][0][column] = value
    with pytest.raises(ValueError, match=message):
        report.render_report(cfg, [billing], END, NOW)


@pytest.mark.parametrize("fault", ["duplicate", "columns", "short-row", "empty"])
def test_incomplete_or_duplicate_response_stops(cfg, billing, fault):
    if fault == "duplicate":
        billing["rows"].append(billing["rows"][0].copy())
    elif fault == "columns":
        billing["columns"][0]["name"] = "Currency"
    elif fault == "short-row":
        billing["rows"][0].pop()
    else:
        billing["rows"] = []
    with pytest.raises(ValueError):
        report.render_report(cfg, [billing], END, NOW)


def test_negative_adjustments_are_retained(cfg, billing):
    billing["rows"][0][0] = "-0.01"
    billing["rows"][1][0] = "0"
    content = report.render_report(cfg, [billing], END, NOW)
    assert "Direct reported subtotal: **USD -0.01000000**" in content
    assert "Shared-resource allocation: **USD 0.18062158**" in content
    assert "USD 0.17062158" in content


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


def test_query_reads_all_pages_in_exact_scope_without_tag_filter(cfg, billing, http):
    endpoint = (f"https://management.azure.com{report.resource_scope(cfg)}"
                "/providers/Microsoft.CostManagement/query")
    first = page(billing["rows"][:2])
    first["nextLink"] = endpoint + "?api-version=2025-03-01&$skiptoken=next"
    second = page(billing["rows"][2:])
    http.side_effect = [response(first), response(second)]
    assert report.fetch_pages(cfg, END, NOW) == [first, second]
    assert http.call_count == 2
    assert http.call_args_list[1].args == (first["nextLink"],)
    request = http.call_args_list[0]
    assert request.args[0] == endpoint + "?api-version=2025-03-01"
    assert request.kwargs["json"] == {
        "type": "ActualCost", "timeframe": "Custom",
        "timePeriod": {"from": "2026-09-17T00:00:00Z", "to": "2026-09-19T09:30:00Z"},
        "dataset": {"granularity": "Daily",
                    "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
                    "grouping": [{"type": "Dimension", "name": "ResourceId"}]},
    }
    assert request.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status", [204, 401, 403, 429, 500])
def test_http_failures_do_not_retry_or_invent_zero(cfg, http, status):
    http.return_value = response({}, status)
    with pytest.raises((ValueError, RuntimeError)):
        report.fetch_pages(cfg, END, NOW)
    http.assert_called_once()
    http.return_value.json.assert_not_called()


@pytest.mark.parametrize("link", [
    "https://example.com/steal", "http://management.azure.com/other", "/relative/path",
])
def test_pagination_cannot_send_token_elsewhere(cfg, http, billing, link):
    billing["nextLink"] = link
    http.return_value = response(billing)
    with pytest.raises(ValueError, match="pagination"):
        report.fetch_pages(cfg, END, NOW)
    http.assert_called_once()


@pytest.mark.parametrize("failure", [RuntimeError("HTTP 429"), requests.Timeout("Timed out"),
                                    ValueError("No cost rows reported")])
def test_failure_keeps_previous_report(cfg, monkeypatch, failure, capsys):
    output = cfg.reports_dir / "lab2-cost.md"
    output.write_text("previous verified snapshot")
    monkeypatch.setattr(sys, "argv", ["cost_report", "--end", "2026-09-19"])
    monkeypatch.setattr(report.config, "load", Mock(return_value=cfg))
    monkeypatch.setattr(report, "fetch_pages", Mock(side_effect=failure))
    assert report.main() == 1
    assert output.read_text() == "previous verified snapshot"
    assert "Previous report left unchanged" in capsys.readouterr().err


def test_success_leaves_only_one_report(cfg, billing, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cost_report", "--end", "2026-09-19"])
    monkeypatch.setattr(report.config, "load", Mock(return_value=cfg))
    monkeypatch.setattr(report, "fetch_pages", Mock(return_value=[billing]))
    assert report.main() == 0
    output = cfg.reports_dir / "lab2-cost.md"
    assert list(cfg.reports_dir.iterdir()) == [output]
    assert "USD 0.43561025 / about THB 14.33" in output.read_text()


@pytest.mark.parametrize("end", ["2026-09-16", "2999-01-01", "not-a-date"])
def test_invalid_end_stops_before_azure(monkeypatch, end):
    monkeypatch.setattr(sys, "argv", ["cost_report", "--end", end])
    fetch = Mock()
    monkeypatch.setattr(report, "fetch_pages", fetch)
    assert report.main() == 1
    fetch.assert_not_called()


@pytest.mark.parametrize("end,code", [("", 0), ("2026-09-19", 0), ("2026-09-19", 7)])
def test_make_calls_only_cost_report_and_propagates_failure(end, code):
    runner = f"import json,sys; print(json.dumps(sys.argv[1:])); sys.exit({code})"
    python = f"{shlex.quote(sys.executable)} -c {shlex.quote(runner)}"
    result = subprocess.run(
        ["make", "--silent", "cost-report", f"PYTHON={python}", f"COST_END={end}"],
        cwd=config.REPO_ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) == (code == 0)
    assert json.loads(result.stdout) == ["scripts/cost_report.py", "--end", end]


def test_reference_conversion_uses_matched_rates():
    assert report.THB_PER_USD * Decimal("0.0882") == Decimal("2.900677")


def test_report_keeps_provider_sdk_imports_in_cloudlayer():
    tree = ast.parse(inspect.getsource(report))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not any(name == "azure" or name.startswith("azure.") for name in imports)
    assert report.fetch_pages is azure_cost.fetch_pages
