"""Lab 3 Task 1.2: registry loading through the adapter, without cloud access."""
from pathlib import Path
from unittest.mock import Mock

import joblib
import pytest
from fastapi.testclient import TestClient

from cloudlayer.azure import AzureAdapter
from service import app as service
from src import config


def test_serving_config_needs_only_serving_settings():
    cfg = config.load_serving()
    assert cfg.provider == "azure"
    assert cfg.model_registry_name == "test-model"
    assert cfg.model_version == "1"
    assert cfg.azure_ml_workspace == ""
    assert cfg.blob_uri == ""


@pytest.mark.parametrize("key", [
    "CLOUD_PROVIDER", "MLFLOW_TRACKING_URI", "MODEL_REGISTRY_NAME", "MODEL_VERSION",
])
def test_missing_serving_setting_is_rejected(monkeypatch, key):
    monkeypatch.delenv(key)
    with pytest.raises(RuntimeError, match=key):
        config.load_serving()


def test_blank_tracking_uri_is_rejected(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "   ")
    with pytest.raises(RuntimeError, match="MLFLOW_TRACKING_URI"):
        config.load_serving()


def test_adapter_loads_exact_version_from_configured_registry(offline_registry):
    cfg = config.load_serving()
    model = object()
    offline_registry.load.side_effect = None
    offline_registry.load.return_value = model

    result = AzureAdapter(cfg).load_model(cfg.model_registry_name, cfg.model_version)

    assert result is model
    offline_registry.tracking.assert_called_once_with(cfg.mlflow_tracking_uri)
    offline_registry.registry.assert_called_once_with(cfg.mlflow_tracking_uri)
    offline_registry.load.assert_called_once_with("models:/test-model/1")


@pytest.mark.parametrize("version", ["latest", "Staging", "0", "-1", "1.0"])
def test_adapter_rejects_unpinned_version(offline_registry, version):
    adapter = AzureAdapter(config.load_serving())
    with pytest.raises(ValueError, match="MODEL_VERSION"):
        adapter.load_model("test-model", version)
    offline_registry.load.assert_not_called()
    offline_registry.tracking.assert_not_called()


@pytest.mark.parametrize("name", ["", "model/1"])
def test_adapter_rejects_invalid_name(offline_registry, name):
    adapter = AzureAdapter(config.load_serving())
    with pytest.raises(ValueError, match="MODEL_REGISTRY_NAME"):
        adapter.load_model(name, "1")
    offline_registry.load.assert_not_called()


def test_adapter_rejects_local_registry_for_azure(monkeypatch, offline_registry):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db")
    adapter = AzureAdapter(config.load_serving())
    with pytest.raises(ValueError, match="MLFLOW_TRACKING_URI"):
        adapter.load_model("test-model", "1")
    offline_registry.load.assert_not_called()
    offline_registry.tracking.assert_not_called()


def test_adapter_preserves_registry_error(offline_registry):
    failure = PermissionError("registry access denied")
    offline_registry.load.side_effect = failure
    adapter = AzureAdapter(config.load_serving())
    with pytest.raises(PermissionError) as raised:
        adapter.load_model("test-model", "1")
    assert raised.value is failure


def test_service_delegates_loading_to_adapter(monkeypatch, offline_registry):
    model = object()
    adapter = Mock()
    adapter.load_model.return_value = model
    factory = Mock(return_value=adapter)
    monkeypatch.setattr(service, "get_adapter", factory, raising=False)

    serving_cfg = config.load_serving()
    assert service._load_model(serving_cfg) is model

    factory.assert_called_once()
    cfg = factory.call_args.args[0]
    assert cfg.model_version == "1"
    adapter.load_model.assert_called_once_with("test-model", "1")
    assert cfg is serving_cfg
    assert service.STATE == {"model": None, "version": "unknown"}
    offline_registry.load.assert_not_called()


def test_missing_registry_settings_do_not_use_model_path(monkeypatch, offline_registry):
    monkeypatch.delenv("MODEL_REGISTRY_NAME")
    monkeypatch.setenv("MODEL_PATH", "reports/model.joblib")
    original_exists = Path.exists
    # Even if a local model file exists, missing registry settings must not use it.
    monkeypatch.setattr(
        Path, "exists",
        lambda path: path == Path("reports/model.joblib") or original_exists(path),
    )
    local_load = Mock(side_effect=AssertionError("Local fallback must not run"))
    monkeypatch.setattr(joblib, "load", local_load)

    with pytest.raises(RuntimeError, match="MODEL_REGISTRY_NAME"):
        service._load_model(config.load_serving())

    local_load.assert_not_called()
    offline_registry.load.assert_not_called()


def test_registry_failure_stays_unready_without_local_fallback(
    monkeypatch, offline_registry, caplog, wait_for_startup,
):
    monkeypatch.setenv("MODEL_PATH", "reports/model.joblib")
    local_load = Mock(side_effect=AssertionError("Local fallback must not run"))
    monkeypatch.setattr(joblib, "load", local_load)
    offline_registry.load.side_effect = PermissionError("registry access denied")

    with TestClient(service.app) as client:
        wait_for_startup(client)
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert service.STATE["model"] is None
        assert service.STATE["version"] == "1"

    assert "model load failed" in caplog.text
    assert "registry access denied" in caplog.text
    offline_registry.load.assert_called_once_with("models:/test-model/1")
    local_load.assert_not_called()
