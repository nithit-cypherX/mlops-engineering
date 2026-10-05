"""Offline adapter tests; token, HTTP and exporter boundaries never reach Azure."""
import copy
import json
import logging
import os
import socket
import sys
from dataclasses import replace
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from opentelemetry.sdk.metrics.export import MetricExportResult

from cloudlayer import azure
from cloudlayer.azure import AzureAdapter
from src import config

WORKSPACE = "00000000-0000-0000-0000-000000000001"
CLIENT = "00000000-0000-0000-0000-000000000002"
START = datetime(2026, 10, 2, 9, 43, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, 9, 58, tzinfo=timezone.utc)
NAME = "lab4.drift.load_pct.psi"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Real network/CLI access is forbidden in these tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(azure.requests, "post", blocked)
    monkeypatch.setattr(azure.subprocess, "run", blocked)


@pytest.fixture
def cfg():
    return config.Config(
        provider="azure", project_id="test-lab", region="test-region", blob_uri="",
        container_registry="", mlflow_tracking_uri="", model_registry_name="test-model",
        identity_ref="", model_version="1", endpoint_name="lab4-staging",
        log_workspace_id=WORKSPACE, monitoring_client_id=CLIENT,
        applicationinsights_connection_string="not-a-real-connection-string",
    )


@pytest.fixture
def credential(monkeypatch):
    client = Mock()
    client.get_token.return_value = SimpleNamespace(token="test-token-do-not-print")
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    constructor = Mock(return_value=client)
    monkeypatch.setattr(azure, "ManagedIdentityCredential", constructor)
    return constructor, client


def payload(records=None):
    event = {
        "message": "request completed", "path": "/predict/batch", "status": 200,
        "model_version": "1", "load_pct_values": [65.0, 70.0],
    }
    rows = [[START.isoformat(), json.dumps(event)]] if records is None else records
    return {"tables": [{"name": "PrimaryResult",
                        "columns": [{"name": "timestamp", "type": "datetime"},
                                    {"name": "record", "type": "string"}],
                        "rows": rows}]}


def response(monkeypatch, data, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    post = Mock(return_value=result)
    monkeypatch.setattr(azure.requests, "post", post)
    return post


def read(cfg):
    return AzureAdapter(cfg).read_prediction_logs(start=START, end=END, model_version="1")


def test_log_scope_and_original_types(cfg, credential, monkeypatch):
    data = payload()
    post = response(monkeypatch, data)
    records = read(cfg)
    assert records == [{
        "timestamp": START.isoformat(), "path": "/predict/batch",
        "status": 200, "model_version": "1", "load_pct_values": [65.0, 70.0],
    }]
    constructor, client = credential
    constructor.assert_called_once_with(client_id=CLIENT)
    client.get_token.assert_called_once_with("https://api.loganalytics.io/.default")
    args, kwargs = post.call_args
    assert args == (f"https://api.loganalytics.azure.com/v1/workspaces/{WORKSPACE}/query",)
    assert kwargs["timeout"] == (10, 40) and kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Authorization"] == "Bearer test-token-do-not-print"
    query = kwargs["json"]["query"]
    assert 'ContainerAppName_s == "lab4-staging"' in query
    assert f"TimeGenerated >= datetime({START.isoformat()})" in query
    assert f"TimeGenerated < datetime({END.isoformat()})" in query
    assert '"request completed"' in query and '"/predict/batch"' in query
    assert 'in ("1", "", "unknown")' in query
    assert "take 10001" in query
    assert "summarize" not in query and "toint" not in query  # No dedup/type coercion.
    assert kwargs["json"]["timespan"] == f"{START.isoformat()}/{END.isoformat()}"


def test_empty_query_is_empty_not_a_score(cfg, credential, monkeypatch):
    response(monkeypatch, payload([]))
    assert read(cfg) == []


@pytest.mark.parametrize("status", [206, 302, 401, 403, 429, 500])
def test_http_failure_is_not_no_data(status, cfg, credential, monkeypatch):
    response(monkeypatch, {"sensitive": "DO-NOT-LEAK"}, status=status)
    with pytest.raises(RuntimeError, match="incomplete") as caught:
        read(cfg)
    assert "DO-NOT-LEAK" not in str(caught.value)


@pytest.mark.parametrize("bad", [
    {"error": {"code": "PartialError"}, **payload()},
    {"tables": []}, {"tables": [payload()["tables"][0]] * 2},
    {"tables": [{"name": "Other", "columns": [], "rows": []}]},
    {"tables": [None]}, None,
])
def test_partial_or_wrong_schema_fails(bad, cfg, credential, monkeypatch):
    response(monkeypatch, bad)
    with pytest.raises(RuntimeError):
        read(cfg)


@pytest.mark.parametrize("rows", [
    None, "not rows", [[START.isoformat()]],
    [[START.isoformat(), "{broken"]], [[START.isoformat(), "[]"]],
    [[START.isoformat(), '{"message":"not completed"}']],
])
def test_malformed_rows_fail(rows, cfg, credential, monkeypatch):
    data = payload()
    data["tables"][0]["rows"] = rows
    response(monkeypatch, data)
    with pytest.raises(RuntimeError):
        read(cfg)


def test_query_cap_detects_truncation(cfg, credential, monkeypatch):
    data = payload()
    data["tables"][0]["rows"] *= 10001
    response(monkeypatch, data)
    with pytest.raises(RuntimeError):
        read(cfg)


@pytest.mark.parametrize("failure", [requests.Timeout("DO-NOT-LEAK"), ValueError("DO-NOT-LEAK")])
def test_transport_or_token_failure_is_safe(failure, cfg, credential, monkeypatch):
    credential[1].get_token.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        read(cfg)
    assert "DO-NOT-LEAK" not in str(caught.value)


def test_post_timeout_is_safe(cfg, credential, monkeypatch):
    monkeypatch.setattr(azure.requests, "post", Mock(side_effect=requests.Timeout("DO-NOT-LEAK")))
    with pytest.raises(RuntimeError, match="Prediction-log"):
        read(cfg)


def test_credential_logs_do_not_leak(cfg, credential, caplog):
    before = logging.root.manager.disable
    def fail(*args):
        logging.getLogger("azure.identity").error("DO-NOT-LEAK")
        raise RuntimeError("DO-NOT-LEAK")
    credential[1].get_token.side_effect = fail
    with pytest.raises(RuntimeError) as caught:
        read(cfg)
    assert "DO-NOT-LEAK" not in caplog.text + str(caught.value)
    assert logging.root.manager.disable == before


@pytest.mark.parametrize("field,value", [
    ("log_workspace_id", "../other"), ("monitoring_client_id", ""),
    ("endpoint_name", 'app" | take 0 //'),
])
def test_invalid_config_rejected_before_auth(field, value, cfg, credential):
    with pytest.raises(RuntimeError):
        read(replace(cfg, **{field: value}))
    credential[0].assert_not_called()


def test_json_types_and_duplicates_are_preserved(cfg, credential, monkeypatch):
    event = {"message": "request completed", "path": "/predict", "status": "200",
             "model_version": "1", "request_id": "same", "load_pct_values": [True]}
    response(monkeypatch, payload([[START.isoformat(), json.dumps(event)]] * 2))
    records = read(cfg)
    assert len(records) == 2
    assert records[0]["status"] == "200" and records[0]["load_pct_values"] == [True]


@pytest.fixture
def exporter(monkeypatch):
    instance = Mock()
    instance.export.return_value = MetricExportResult.SUCCESS
    constructor = Mock(return_value=instance)
    fake = ModuleType("azure.monitor.opentelemetry.exporter")
    fake.AzureMonitorMetricExporter = constructor
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry.exporter", fake)
    return constructor, instance


def test_one_gauge_explicit_identity_and_no_buffer(cfg, credential, exporter):
    before_env = copy.deepcopy(dict(os.environ))
    before_logging = logging.root.manager.disable
    AzureAdapter(cfg).emit_metric(NAME, 0.63)
    constructor, instance = exporter
    kwargs = constructor.call_args.kwargs
    assert kwargs["credential"] is credential[1]
    assert kwargs["disable_offline_storage"] is True and kwargs["retry_total"] == 0
    assert kwargs["timeout"] == 10 and kwargs["read_timeout"] == 30
    batch = instance.export.call_args.args[0]
    metric = batch.resource_metrics[0].scope_metrics[0].metrics[0]
    assert metric.name == NAME and metric.unit == "1"
    point, = metric.data.data_points
    assert point.value == 0.63
    assert point.attributes == {"endpoint": "lab4-staging", "model_version": "1"}
    assert point.time_unix_nano > 0
    instance.shutdown.assert_called_once()
    instance.force_flush.assert_not_called()
    assert dict(os.environ) == before_env
    assert logging.root.manager.disable == before_logging


@pytest.mark.parametrize("failure", ["result", "raise", "init", "shutdown"])
def test_export_failure_is_not_success_and_restores_state(
    failure, cfg, credential, exporter, caplog,
):
    constructor, instance = exporter
    before_env = dict(os.environ)
    before_logging = logging.root.manager.disable
    if failure == "result":
        instance.export.return_value = MetricExportResult.FAILURE
    elif failure == "raise":
        def fail(*args):
            logging.getLogger("azure.test").error("DO-NOT-LEAK")
            raise RuntimeError("DO-NOT-LEAK")
        instance.export.side_effect = fail
    elif failure == "init":
        constructor.side_effect = ValueError("DO-NOT-LEAK")
    else:
        instance.shutdown.side_effect = ValueError("DO-NOT-LEAK")
    with pytest.raises(RuntimeError) as caught:
        AzureAdapter(cfg).emit_metric(NAME, 0.63)
    assert "DO-NOT-LEAK" not in str(caught.value) + caplog.text
    assert dict(os.environ) == before_env
    assert logging.root.manager.disable == before_logging


@pytest.mark.parametrize("value", [None, True, "0.6", float("nan"), float("inf")])
def test_invalid_metric_does_not_export(value, cfg, credential, exporter):
    with pytest.raises(ValueError):
        AzureAdapter(cfg).emit_metric(NAME, value)
    exporter[0].assert_not_called()


def test_connection_string_not_in_config_repr(cfg):
    assert cfg.applicationinsights_connection_string not in repr(cfg)
