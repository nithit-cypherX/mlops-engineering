"""Offline checks for the dashboard definition, not a KQL execution engine."""
import json
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "monitoring" / "dashboard.json"
PANEL_NAMES = ("request_rate", "error_rate", "latency", "feature_load", "model_version")


@pytest.fixture
def workbook():
    return json.loads(DASHBOARD.read_text())


@pytest.fixture
def panels(workbook):
    return {item["name"]: item["content"] for item in workbook["items"] if item["type"] == 3}


def test_workbook_has_five_scoped_panels_and_no_embedded_account_ids(workbook, panels):
    assert workbook["version"] == "Notebook/1.0"
    assert tuple(panels) == PANEL_NAMES
    assert "/subscriptions/" not in DASHBOARD.read_text().lower()
    parameters = next(item["content"]["parameters"] for item in workbook["items"] if item["type"] == 9)
    workspace, time_range = parameters
    assert workspace["name"] == "Workspace" and workspace["isRequired"]
    assert workspace["type"] == 5 and workspace["value"] == []
    assert time_range["name"] == "TimeRange" and time_range["type"] == 4
    assert time_range["value"]["durationMs"] == 86400000


@pytest.mark.parametrize("name", PANEL_NAMES)
def test_each_panel_uses_the_selected_workspace_and_handles_no_data(panels, name):
    panel = panels[name]
    assert panel["queryType"] == 0
    assert panel["resourceType"] == "microsoft.operationalinsights/workspaces"
    assert panel["crossComponentResources"] == ["{Workspace}"]
    assert panel["noDataMessage"]
    query = panel["query"]
    assert "ContainerAppConsoleLogs_CL" in query
    assert 'ContainerAppName_s == "itcs355-lab4-staging"' in query
    assert 'tostring(Event.message) == "request completed"' in query
    assert "union isfuzzy" not in query  # A missing source must not be hidden as success.


@pytest.mark.parametrize("name", PANEL_NAMES[:4])
def test_prediction_panels_exclude_probes_and_use_selected_time(panels, name):
    query = panels[name]["query"]
    assert 'tostring(Event.path) in ("/predict", "/predict/batch")' in query
    assert '"/health"' not in query and '"/ready"' not in query
    assert "let Start = {TimeRange:start};" in query
    assert "let End = {TimeRange:end};" in query
    assert panels[name]["visualization"] == "timechart"


@pytest.mark.parametrize("name", PANEL_NAMES[:3])
def test_request_metrics_use_complete_one_minute_bins(panels, name):
    query = panels[name]["query"]
    assert "Time = bin(TimeGenerated, 1m)" in query
    assert "Time >= Start and Time + 1m <= End" in query


@pytest.mark.parametrize("name", PANEL_NAMES[:4])
def test_timecharts_hide_aggregate_totals_but_keep_series_labels(panels, name):
    settings = panels[name]["chartSettings"]
    # Summing per-window rates, percentiles or means is not a whole-range metric.
    assert settings.get("showMetrics") is False
    assert settings["showLegend"] is True


def test_rates_and_percentiles_have_explicit_units_and_denominators(panels):
    assert "Requests / 60.0" in panels["request_rate"]["query"]
    errors = panels["error_rate"]["query"]
    assert "between (400 .. 499)" in errors and "between (500 .. 599)" in errors
    assert "100.0 * Errors4xx / Requests" in errors
    assert "100.0 * Errors5xx / Requests" in errors
    assert panels["error_rate"]["chartSettings"]["yAxis"] == ["Error4xxPct", "Error5xxPct"]
    for percentile in (50, 95, 99):
        assert f"percentile(LatencyMs, {percentile})" in panels["latency"]["query"]


def test_feature_window_expands_each_batch_row_before_averaging(panels):
    panel = panels["feature_load"]
    query = panel["query"]
    assert "TimeGenerated >= Start - 5m" in query
    assert "mv-expand LoadPct = Event.load_pct_values" in query
    assert query.index("mv-expand LoadPct") < query.index("avg(LoadPct)")
    assert "range(bin(TimeGenerated, 1m) + 1m, bin(TimeGenerated, 1m) + 5m, 1m)" in query
    assert "Time >= Start and Time <= End" in query
    assert "SampleCount = count()" in query
    assert panel["chartSettings"]["yAxis"] == ["MeanLoadPct"]
    # The query needs the five-minute lookback before the selected start.
    assert "timeContextFromParameter" not in panel


def test_current_model_uses_fresh_successful_signals_not_historical_picker(panels):
    panel = panels["model_version"]
    query = panel["query"]
    assert "TimeGenerated >= ago(5m)" in query
    assert "toint(Event.status) == 200" in query
    assert 'ModelVersion != "unknown"' in query
    assert "by ModelVersion" in query  # Do not hide a second recently observed version.
    assert "{TimeRange" not in query and "timeContextFromParameter" not in panel
    assert panel["timeContext"]["durationMs"] == 300000
    assert "LastSeen" in query
