"""Synthetic billing fixtures only; no real Azure queries or spend claims."""
import copy
import json
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from cloudlayer import azure_cost as cost
from cloudlayer import lab4_scope as scope
from scripts import cost_report as report
from src import config

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
END = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    fail = Mock(side_effect=AssertionError("Unexpected real boundary"))
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(cost, "access_token", fail)
    for method in ("post", "get", "put", "patch", "delete", "request"):
        monkeypatch.setattr(cost.requests, method, fail)


@pytest.fixture
def cfg(tmp_path):
    return config.Config(
        provider="azure", project_id=scope.GROUP, region="malaysiawest",
        blob_uri="", container_registry="", mlflow_tracking_uri="",
        model_registry_name="", identity_ref="", reports_dir=tmp_path,
        azure_subscription_id=scope.SUBSCRIPTION, azure_ml_workspace="mlw-itcs355-u6688124",
    )


def page(rows, link=None):
    return {"columns": [{"name": n} for n in ("PreTaxCost", "UsageDate", "ResourceId", "Currency")],
            "rows": rows, "nextLink": link}


@pytest.fixture
def billing():
    return page([["0.2", 20261002, scope.APP, "USD"],
                 ["0.1", 20261002, scope.ACR, "USD"],
                 ["0", 20261002, scope.LOGS, "USD"]])


def test_separate_direct_shared_missing_and_zero(cfg, billing):
    text = report.render_report(cfg, [billing], END, NOW)
    assert "| Staging app | Lab 4 | 0.2 |" in text
    assert "| Log Analytics | Shared | 0 |" in text
    assert "| Drift compute | Lab 4 | No cost rows reported |" in text
    assert "Lab 4 resources reported subtotal: **USD 0.2**" in text
    assert "Shared resources reported subtotal: **USD 0.1**" in text
    assert "All reported rows in this resource group: **USD 0.3**" in text
    assert "not a measured Lab 4-only cost" in text
    assert "not remaining student credit or a final bill" in text
    assert scope.SUBSCRIPTION not in text  # Public report need not expose subscription UUID.
    assert "2026-09-29 to 2026-10-05" in text


def test_shared_only_is_not_zero_direct_cost(cfg):
    text = report.render_report(cfg, [page([["0.1", 20260929, scope.ACR, "USD"]])], END, NOW)
    assert "Lab 4 resources reported subtotal: **No cost rows reported; not assumed zero**" in text


def test_reviewed_resources_cover_inventory_without_scope_prefix_guessing(cfg):
    resources = cost.reviewed_resources(cfg)
    assert len(resources) == 19
    assert resources[scope.COMPUTE.lower()][1] == "Lab 4"
    assert resources[scope.WORKSPACE.lower()][1] == "Shared"
    assert scope.WORKSPACE.lower() + "/computes/unreviewed" not in resources


@pytest.mark.parametrize("column,value", [
    (0, "NaN"), (0, "Infinity"), (0, "unknown"), (0, True),
    (1, 20260928), (1, 20261006), (1, "not-a-date"),
    (2, "/unreviewed/resource"), (2, None), (3, "THB"), (3, None),
])
def test_invalid_rows_fail(cfg, billing, column, value):
    billing["rows"][0][column] = value
    with pytest.raises(ValueError):
        report.render_report(cfg, [billing], END, NOW)


@pytest.mark.parametrize("fault", ["duplicate", "missing-column", "duplicate-column", "empty", "pending-page", "short-row"])
def test_invalid_or_incomplete_pages_fail(cfg, billing, fault):
    if fault == "duplicate":
        billing["rows"].append(billing["rows"][0].copy())
    elif fault == "missing-column":
        billing["columns"][0]["name"] = "wrong"
    elif fault == "duplicate-column":
        billing["columns"][0]["name"] = "Currency"
    elif fault == "empty":
        billing["rows"] = []
    elif fault == "pending-page":
        billing["nextLink"] = "not-fetched"
    else:
        billing["rows"][0].pop()
    with pytest.raises(ValueError):
        report.render_report(cfg, [billing], END, NOW)


def test_reordered_columns_multiple_pages_and_credit(cfg, billing):
    first = page(copy.deepcopy(billing["rows"][:2]), "already-read")
    second = page([list(reversed(billing["rows"][2]))])
    second["columns"].reverse()
    first["rows"].append(["-0.01", 20261003, scope.APP.upper(), "USD"])
    text = report.render_report(cfg, [first, second], END, NOW)
    assert "**USD 0.29**" in text and "all 2 response page(s) read" in text


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(cost, "access_token", Mock(return_value="test-only"))
    post = Mock()
    monkeypatch.setattr(cost.requests, "post", post)
    return post


def response(properties, status=200):
    return SimpleNamespace(status_code=status, json=Mock(return_value={"properties": properties}))


def test_read_query_follows_all_pages_and_preserves_decimal(cfg, billing, http):
    endpoint = f"https://management.azure.com{scope.SCOPE}/providers/Microsoft.CostManagement/query"
    second_url = endpoint + "?api-version=2025-03-01&$skiptoken=next"
    first = page(billing["rows"][:2], second_url)
    second = page(billing["rows"][2:])
    http.side_effect = [response(first), response(second)]
    assert cost.fetch_pages(cfg, END, NOW) == [first, second]
    assert http.call_count == 2 and http.call_args.args == (second_url,)
    request = http.call_args_list[0]
    assert request.kwargs["json"]["timePeriod"] == {
        "from": "2026-09-29T00:00:00Z", "to": "2026-10-05T10:00:00Z"}
    assert request.kwargs["json"]["type"] == "ActualCost"
    assert "filter" not in request.kwargs["json"]["dataset"]
    assert request.kwargs["allow_redirects"] is False
    raw = requests.Response()
    raw.status_code = 200
    raw._content = b'{"properties":{"rows":[[0.123456789]],"nextLink":null}}'
    http.side_effect = None
    http.return_value = raw
    assert cost.fetch_pages(cfg, END, NOW)[0]["rows"][0][0] == Decimal("0.123456789")


@pytest.mark.parametrize("status", [204, 301, 401, 403, 429, 500])
def test_http_failure_no_retry_or_fake_zero(cfg, http, status):
    http.return_value = response({}, status)
    with pytest.raises((RuntimeError, ValueError)):
        cost.fetch_pages(cfg, END, NOW)
    http.assert_called_once()


@pytest.mark.parametrize("link", [
    "https://example.com/steal", "http://management.azure.com/other", "/relative",
    "https://management.azure.com/subscriptions/other/providers/Microsoft.CostManagement/query",
    "", False,
])
def test_bad_pagination_stops_before_token_forwarding(cfg, billing, http, link):
    billing["nextLink"] = link
    http.return_value = response(billing)
    with pytest.raises(ValueError):
        cost.fetch_pages(cfg, END, NOW)
    http.assert_called_once()


@pytest.mark.parametrize("field,value", [("provider", "local"), ("project_id", "other"), ("azure_subscription_id", "other")])
def test_scope_rejected_before_credentials(cfg, field, value):
    with pytest.raises(ValueError):
        cost.fetch_pages(replace(cfg, **{field: value}), END, NOW)
    cost.access_token.assert_not_called()


def test_dates_and_timezone_are_checked():
    for end, now in [(date(2026, 9, 28), NOW), (date(2026, 10, 6), NOW),
                     (END, NOW.replace(tzinfo=None)),
                     (END, NOW.replace(tzinfo=timezone(timedelta(hours=7))))]:
        with pytest.raises(ValueError):
            cost.window_end(end, now)
    assert cost.window_end(date(2026, 10, 4), NOW) == "2026-10-04T23:59:59Z"


def test_main_writes_report_only_and_default_end_is_today(cfg, billing, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(report, "datetime", Clock)
    monkeypatch.setattr(sys, "argv", ["report"])
    monkeypatch.setattr(report.config, "load", Mock(return_value=cfg))
    fetch = Mock(return_value=[billing])
    monkeypatch.setattr(report, "fetch_pages", fetch)
    assert report.main() == 0 and fetch.call_args.args[1] == END
    assert [p.name for p in cfg.reports_dir.iterdir()] == ["lab4-cost.md"]
    assert "USD 0.3" in (cfg.reports_dir / "lab4-cost.md").read_text()


@pytest.mark.parametrize("failure", [RuntimeError("rate-limited"), requests.Timeout(), ValueError("invalid data")])
def test_failed_query_preserves_previous_report(cfg, monkeypatch, failure):
    output = cfg.reports_dir / "lab4-cost.md"
    output.write_text("previous snapshot")
    monkeypatch.setattr(sys, "argv", ["report", "--end", "2026-10-05"])
    monkeypatch.setattr(report.config, "load", Mock(return_value=cfg))
    monkeypatch.setattr(report, "fetch_pages", Mock(side_effect=failure))
    assert report.main() == 1 and output.read_text() == "previous snapshot"


@pytest.mark.parametrize("end,code", [("", 0), ("2026-10-05", 0), ("2026-10-05", 7), ('bad"; exit 9; "', 0)])
def test_make_passes_date_as_one_argument_and_propagates_failure(end, code):
    runner = f"import json,sys; print(json.dumps(sys.argv[1:])); sys.exit({code})"
    python = f"{shlex.quote(sys.executable)} -c {shlex.quote(runner)}"
    result = subprocess.run(["make", "--silent", "cost-report", f"PYTHON={python}", f"COST_END={end}"],
                            cwd=config.REPO_ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=20)
    assert (result.returncode == 0) == (code == 0)
    assert json.loads(result.stdout) == ["-m", "scripts.cost_report", "--end", end]
