"""Offline checks for the small staging-scope configuration."""
import os
from pathlib import Path

import pytest

from src.config import STAGING_SLOTS, load_staging

EXAMPLE = Path(__file__).resolve().parents[1] / "cloud.env.example"


@pytest.fixture(autouse=True)
def clear_config(monkeypatch):
    for key in (*STAGING_SLOTS, "SERVING_INSTANCE", "SERVING_IMAGE", "MLFLOW_TRACKING_URI", "SERVING_ALLOWED_IP"):
        monkeypatch.delenv(key, raising=False)


def test_example_loads_without_creating_cloud_env():
    cfg = load_staging(EXAMPLE)
    assert cfg.provider == "azure"
    assert cfg.endpoint_name == "lab5-staging"
    assert cfg.azure_containerapps_environment == "feedbackpulse-env"
    assert cfg.serving_instance == "0.5cpu-1Gi"
    assert cfg.serving_image == cfg.serving_allowed_ip == cfg.mlflow_tracking_uri == ""


def test_deployment_values_can_be_supplied_without_a_model_version_setting(monkeypatch):
    monkeypatch.setenv("SERVING_IMAGE", " digest ")
    monkeypatch.setenv("SERVING_ALLOWED_IP", " 8.8.8.8/32 ")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", " tracking ")
    cfg = load_staging(EXAMPLE)
    assert cfg.serving_image == "digest"
    assert cfg.serving_allowed_ip == "8.8.8.8/32"
    assert cfg.mlflow_tracking_uri == "tracking"
    assert not hasattr(cfg, "model_version")


def test_exported_value_wins_without_changing_process_environment(monkeypatch):
    monkeypatch.setenv("ENDPOINT_NAME", "another-app")
    cfg = load_staging(EXAMPLE)
    assert cfg.endpoint_name == "another-app"
    assert "PROJECT_ID" not in os.environ


@pytest.mark.parametrize("key", STAGING_SLOTS)
def test_blank_exported_required_value_fails(monkeypatch, key):
    monkeypatch.setenv(key, "   ")
    with pytest.raises(RuntimeError, match=key):
        load_staging(EXAMPLE)


def test_missing_file_and_missing_values_fail(tmp_path):
    with pytest.raises(RuntimeError, match="Missing staging configuration"):
        load_staging(tmp_path / "missing.env")


def test_missing_file_can_use_explicit_environment(monkeypatch, tmp_path):
    for line in EXAMPLE.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            monkeypatch.setenv(key, value)
    assert load_staging(tmp_path / "missing.env").endpoint_name == "lab5-staging"


def test_env_file_is_data_not_a_shell_script(tmp_path):
    path = tmp_path / "scope.env"
    path.write_text(EXAMPLE.read_text() + "\nENDPOINT_NAME=$(do-not-execute)\n")
    assert load_staging(path).endpoint_name == "$(do-not-execute)"
