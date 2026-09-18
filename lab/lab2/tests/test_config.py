"""Configuration checks do not contact Azure or start training."""
import pytest

from src import config


BASE_VALUES = {
    "CLOUD_PROVIDER": "azure",
    "PROJECT_ID": "course-resource-group",
    "REGION": "test-region",
    "BLOB_URI": "https://storage.example.invalid/container/lab2",
    "CONTAINER_REGISTRY": "registry.example.invalid/training",
    "MLFLOW_TRACKING_URI": "sqlite:///reports/mlflow.db",
    "MODEL_REGISTRY_NAME": "course-model",
    "IDENTITY_REF": "compute-principal-id",
}
TRAINING_VALUES = {
    "AZURE_SUBSCRIPTION_ID": "subscription-id",
    "AZURE_ML_WORKSPACE": "workspace-name",
    "AZURE_ML_COMPUTE": "cpu-training",
}


@pytest.fixture(autouse=True)
def clean_cloud_environment(monkeypatch):
    for slot in config.CAPABILITY_SLOTS + config.AZURE_TRAINING_SLOTS:
        monkeypatch.delenv(slot, raising=False)


def configure(monkeypatch, values):
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_load_azure_training_configuration(monkeypatch):
    configure(monkeypatch, BASE_VALUES | TRAINING_VALUES)
    cfg = config.load()
    assert cfg.provider == "azure"
    assert cfg.project_id == BASE_VALUES["PROJECT_ID"]
    assert cfg.azure_subscription_id == TRAINING_VALUES["AZURE_SUBSCRIPTION_ID"]
    assert cfg.azure_ml_workspace == TRAINING_VALUES["AZURE_ML_WORKSPACE"]
    assert cfg.azure_ml_compute == TRAINING_VALUES["AZURE_ML_COMPUTE"]
    assert cfg.blob_uri == BASE_VALUES["BLOB_URI"]
    assert cfg.identity_ref == BASE_VALUES["IDENTITY_REF"]
    assert cfg.raw_path == config.REPO_ROOT / "data" / "raw" / "sensors.csv"
    assert cfg.dvc_metadata_path == config.REPO_ROOT / "data" / "raw.dvc"


@pytest.mark.parametrize("slot", config.AZURE_TRAINING_SLOTS)
def test_missing_azure_training_setting_fails(monkeypatch, slot):
    configure(monkeypatch, BASE_VALUES | TRAINING_VALUES)
    monkeypatch.delenv(slot)
    with pytest.raises(RuntimeError, match=slot):
        config.load()


@pytest.mark.parametrize("slot", config.CAPABILITY_SLOTS)
def test_base_capability_slots_are_still_required(monkeypatch, slot):
    configure(monkeypatch, BASE_VALUES | TRAINING_VALUES)
    monkeypatch.delenv(slot)
    with pytest.raises(RuntimeError, match=slot):
        config.load()


def test_local_provider_does_not_require_azure_settings(monkeypatch):
    configure(monkeypatch, BASE_VALUES | {"CLOUD_PROVIDER": "local"})
    cfg = config.load()
    assert cfg.provider == "local"
    assert cfg.azure_subscription_id == ""
    assert cfg.azure_ml_workspace == ""
    assert cfg.azure_ml_compute == ""


def test_non_strict_load_preserves_local_defaults():
    cfg = config.load(strict=False)
    assert cfg.provider == "local"
    assert cfg.mlflow_tracking_uri == "sqlite:///mlflow.db"
    assert cfg.data_dir == config.REPO_ROOT / "data"
    assert cfg.reports_dir == config.REPO_ROOT / "reports"
    assert cfg.azure_ml_compute == ""


def test_exported_values_override_env_file(monkeypatch, tmp_path):
    path = tmp_path / "cloud.env"
    path.write_text(
        "# Test configuration\n"
        "AZURE_ML_COMPUTE=cpu-from-file\n"
        "AZURE_ML_WORKSPACE=workspace-from-file\n"
    )
    monkeypatch.setenv("AZURE_ML_COMPUTE", "cpu-exported")
    config._load_env_file(path)
    cfg = config.load(strict=False)
    assert cfg.azure_ml_compute == "cpu-exported"
    assert cfg.azure_ml_workspace == "workspace-from-file"
