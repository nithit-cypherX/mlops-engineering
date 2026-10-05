"""Offline one-shot job tests: real PSI, fake cloud adapter, no training."""
import json
import socket
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import numpy as np
import pytest

from monitoring import drift, run_drift
from src import config

AS_OF = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
REFERENCE = config.REPO_ROOT / "data/raw/sensors.csv"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network is forbidden in these tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def cfg():
    return config.Config(provider="azure", project_id="", region="", blob_uri="",
                         container_registry="", mlflow_tracking_uri="",
                         model_registry_name="", identity_ref="", model_version="1")


def records(values):
    return [{"timestamp": "2026-10-02T09:50:00Z", "path": "/predict/batch",
             "status": 200, "model_version": "1", "load_pct_values": list(values[i:i+100])}
            for i in range(0, len(values), 100)]


def run(cfg, adapter, path=REFERENCE):
    return run_drift.run(cfg, adapter, reference_path=path, as_of=AS_OF)


@pytest.mark.parametrize("count", [0, 1, 499])
def test_not_enough_data_never_publishes_zero(count, cfg):
    adapter = Mock()
    adapter.read_prediction_logs.return_value = records([65.0] * count)
    result = run(cfg, adapter)
    assert result["status"] == "insufficient_data" and result["psi"] is None
    assert result["current_count"] == count and result["metric_export"] == "not_sent"
    adapter.emit_metric.assert_not_called()
    assert result["window_start"] == "2026-10-02T09:43:00+00:00"
    assert result["window_end"] == "2026-10-02T09:58:00+00:00"


@pytest.mark.parametrize("shift,expected", [(0, "below_threshold"), (20, "drift_detected")])
def test_clean_and_shifted_publish_exact_psi(shift, expected, cfg):
    reference = drift.load_reference(REFERENCE)[drift.FEATURE].to_numpy()
    values = np.clip(reference + shift, 0, 100).tolist()
    adapter = Mock()
    adapter.read_prediction_logs.return_value = records(values)
    result = run(cfg, adapter)
    assert result["status"] == expected and result["metric_export"] == "accepted"
    assert result["psi"] == drift.psi(reference, values)
    assert result["reference_count"] == 3600 and result["current_count"] == 3600
    adapter.emit_metric.assert_called_once_with(run_drift.METRIC_NAME, result["psi"])


def test_invalid_telemetry_does_not_publish(cfg):
    adapter = Mock()
    bad = records([65.0] * 500)
    bad[0]["load_pct_values"] = None
    adapter.read_prediction_logs.return_value = bad
    with pytest.raises(ValueError):
        run(cfg, adapter)
    adapter.emit_metric.assert_not_called()


def test_query_failure_does_not_publish(cfg):
    adapter = Mock()
    adapter.read_prediction_logs.side_effect = RuntimeError("failure")
    with pytest.raises(RuntimeError):
        run(cfg, adapter)
    adapter.emit_metric.assert_not_called()


def test_bad_reference_stops_before_query(cfg, tmp_path):
    adapter = Mock()
    with pytest.raises(OSError):
        run(cfg, adapter, tmp_path / "missing.csv")
    adapter.read_prediction_logs.assert_not_called()


def test_other_model_not_silently_compared(cfg):
    adapter = Mock()
    with pytest.raises(ValueError, match="model version 1"):
        run(replace(cfg, model_version="2"), adapter)
    adapter.read_prediction_logs.assert_not_called()


def test_cli_export_failure_returns_safe_error(cfg, monkeypatch, capsys):
    adapter = Mock()
    adapter.read_prediction_logs.return_value = records([90.0] * 500)
    adapter.emit_metric.side_effect = RuntimeError("DO-NOT-LEAK")
    monkeypatch.setattr(config, "load_monitoring", lambda: cfg)
    monkeypatch.setattr(run_drift, "get_adapter", lambda _: adapter)
    assert run_drift.main(["--as-of", AS_OF.isoformat()]) == 1
    output = capsys.readouterr().out
    assert "DO-NOT-LEAK" not in output
    result = json.loads(output)
    assert result["status"] == "error" and result["metric_export"] == "unconfirmed"


def test_cli_no_data_is_successful_check_not_healthy(cfg, monkeypatch, capsys):
    adapter = Mock()
    adapter.read_prediction_logs.return_value = []
    monkeypatch.setattr(config, "load_monitoring", lambda: cfg)
    monkeypatch.setattr(run_drift, "get_adapter", lambda _: adapter)
    assert run_drift.main(["--as-of", AS_OF.isoformat()]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "insufficient_data"
    assert result["metric_export"] == "not_sent" and result["psi"] is None
    adapter.emit_metric.assert_not_called()


def test_monitoring_config_independent_of_serving(monkeypatch):
    keys = {"CLOUD_PROVIDER": "azure", "ENDPOINT_NAME": "test-staging", "MODEL_VERSION": "1",
            "LOG_WORKSPACE_ID": "test-id", "MONITORING_CLIENT_ID": "test-client",
            "APPLICATIONINSIGHTS_CONNECTION_STRING": "test-connection"}
    for key in config.CAPABILITY_SLOTS + config.AZURE_WORKSPACE_SLOTS:
        monkeypatch.delenv(key, raising=False)
    for key, value in keys.items():
        monkeypatch.setenv(key, value)
    cfg = config.load_monitoring()
    assert cfg.log_workspace_id == "test-id" and cfg.monitoring_client_id == "test-client"
    for key in keys:
        monkeypatch.delenv(key)
        with pytest.raises(RuntimeError, match=key):
            config.load_monitoring()
        monkeypatch.setenv(key, keys[key])
