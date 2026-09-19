"""Registration and launcher checks with fake clients: never write to Azure."""
import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import PERMISSION_DENIED, RESOURCE_ALREADY_EXISTS

from cloudlayer import azure
from scripts import register_model


RUN_ID = "11111111-2222-3333-4444-555555555555"
JOB_ID = "lab2-" + "a" * 32
MODEL_URI = f"runs:/{RUN_ID}/model"
NAME = "course-model"
IMAGE = "example.azurecr.io/lab2@sha256:" + "b" * 64
LINEAGE = {
    "git_commit": "c" * 40, "data_version": "d" * 32 + ".dir",
    "mlflow_run_id": RUN_ID, "training_job_id": JOB_ID,
    "image_digest": "sha256:" + "b" * 64, "seed": "20260101",
    "metric_val": "0.84", "metric_test": "0.85",
}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    cfg = SimpleNamespace(mlflow_tracking_uri="azureml://example-tracking", model_registry_name=NAME)
    adapter = azure.AzureAdapter(cfg)
    managed = Mock()
    adapter._ml_client = managed
    run = SimpleNamespace(
        info=SimpleNamespace(run_id=RUN_ID, status="FINISHED", artifact_uri="azureml://saved/artifacts"),
        data=SimpleNamespace(
            tags={"git_commit": "c" * 40, "dvc_hash": "d" * 32 + ".dir",
                  "mlflow.parentRunId": JOB_ID, "image_uri": IMAGE},
            params={"seed": "20260101"}, metrics={"val_roc_auc": 0.84, "test_roc_auc": 0.85},
        ),
    )
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(azure, "MlflowClient", factory)
    client.get_run.return_value = run
    client.list_artifacts.return_value = [SimpleNamespace(path="model/" + p, is_dir=False)
                                          for p in ("MLmodel", "model.pkl", "conda.yaml",
                                                    "python_env.yaml", "requirements.txt")]
    job = SimpleNamespace(name=JOB_ID, tags=dict(run.data.tags), environment="saved-env:1")
    managed.jobs.get.return_value = job
    managed.environments.get.return_value = SimpleNamespace(image=IMAGE)
    client.create_model_version.return_value = SimpleNamespace(version="7")
    saved = SimpleNamespace(name=NAME, version="7", status="READY", run_id=RUN_ID,
                            current_stage="None", tags=dict(LINEAGE))
    client.get_model_version.return_value = saved
    return SimpleNamespace(adapter=adapter, client=client, managed=managed, factory=factory,
                           cfg=cfg, run=run, job=job, saved=saved)


def test_registers_original_artifact_with_eight_version_tags_and_reads_it_back(setup):
    assert setup.adapter.register_model(MODEL_URI, NAME) == "7"
    setup.factory.assert_called_once_with(
        tracking_uri=setup.cfg.mlflow_tracking_uri, registry_uri=setup.cfg.mlflow_tracking_uri,
    )
    setup.managed.jobs.get.assert_called_once_with(JOB_ID)
    setup.managed.environments.get.assert_called_once_with("saved-env", "1")
    setup.client.create_registered_model.assert_called_once_with(NAME)
    setup.client.create_model_version.assert_called_once_with(
        name=NAME, source="azureml://saved/artifacts/model", run_id=RUN_ID, tags=LINEAGE,
    )
    setup.client.get_model_version.assert_called_once_with(NAME, "7")
    assert {call[0] for call in setup.client.mock_calls} == {
        "get_run", "list_artifacts", "create_registered_model", "create_model_version",
        "get_model_version",
    }
    assert {call[0] for call in setup.managed.mock_calls} == {"jobs.get", "environments.get"}


@pytest.mark.parametrize("uri,name", [
    ("models:/other/1", NAME), (MODEL_URI + "?token=x", NAME),
    (f"runs:/{RUN_ID}/other", NAME), (MODEL_URI, ""), (MODEL_URI, "../other"),
])
def test_bad_input_is_rejected_before_client_creation(setup, uri, name):
    with pytest.raises(ValueError):
        setup.adapter.register_model(uri, name)
    setup.factory.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("git_commit", "unknown"), ("git_commit", "a" * 40 + "-dirty"),
    ("dvc_hash", ""), ("mlflow.parentRunId", ""), ("image_uri", "image:latest"),
])
def test_missing_or_invalid_lineage_does_not_create_a_model(setup, field, value):
    setup.run.data.tags[field] = value
    with pytest.raises(ValueError, match="lineage"):
        setup.adapter.register_model(MODEL_URI, NAME)
    setup.client.create_registered_model.assert_not_called()
    setup.client.create_model_version.assert_not_called()


@pytest.mark.parametrize("seed", ["", "-1", str(2**32), "1.5"])
def test_invalid_seed_stops_before_registration(setup, seed):
    setup.run.data.params["seed"] = seed
    with pytest.raises(ValueError, match="lineage"):
        setup.adapter.register_model(MODEL_URI, NAME)
    setup.client.create_registered_model.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("val_roc_auc", None), ("test_roc_auc", float("nan")),
    ("val_roc_auc", float("inf")), ("test_roc_auc", 1.1), ("val_roc_auc", -0.1),
])
def test_missing_or_invalid_score_stops_before_registration(setup, key, value):
    setup.run.data.metrics[key] = value
    with pytest.raises(ValueError, match="ROC-AUC"):
        setup.adapter.register_model(MODEL_URI, NAME)
    setup.client.create_registered_model.assert_not_called()


@pytest.mark.parametrize("fault", ["unfinished", "missing-model", "job-lineage", "image"])
def test_unverified_source_is_not_registered(setup, fault):
    if fault == "unfinished":
        setup.run.info.status = "FAILED"
    elif fault == "missing-model":
        setup.client.list_artifacts.return_value = []
    elif fault == "job-lineage":
        setup.job.tags["dvc_hash"] = "e" * 32 + ".dir"
    else:
        setup.managed.environments.get.return_value.image = IMAGE.replace("b" * 64, "e" * 64)
    with pytest.raises(ValueError):
        setup.adapter.register_model(MODEL_URI, NAME)
    setup.client.create_registered_model.assert_not_called()
    setup.client.create_model_version.assert_not_called()


def test_existing_model_name_allows_one_new_version(setup):
    setup.client.create_registered_model.side_effect = MlflowException(
        "Model name exists", error_code=RESOURCE_ALREADY_EXISTS,
    )
    assert setup.adapter.register_model(MODEL_URI, NAME) == "7"
    setup.client.create_model_version.assert_called_once()


@pytest.mark.parametrize("method", ["create_registered_model", "create_model_version"])
def test_write_errors_propagate_without_retry_or_promotion(setup, method):
    getattr(setup.client, method).side_effect = MlflowException(
        "Not allowed", error_code=PERMISSION_DENIED,
    )
    with pytest.raises(MlflowException, match="Not allowed"):
        setup.adapter.register_model(MODEL_URI, NAME)
    getattr(setup.client, method).assert_called_once()
    setup.client.get_model_version.assert_not_called()
    setup.client.transition_model_version_stage.assert_not_called()


@pytest.mark.parametrize("fault", ["tag", "run", "status", "stage", "read-error"])
def test_readback_failure_reports_created_version_without_creating_another(setup, fault):
    if fault == "tag":
        setup.saved.tags.pop("data_version")
    elif fault == "run":
        setup.saved.run_id = "other-run"
    elif fault == "status":
        setup.saved.status = "FAILED_REGISTRATION"
    elif fault == "stage":
        setup.saved.current_stage = "Staging"
    else:
        setup.client.get_model_version.side_effect = MlflowException("Read failed")
    with pytest.raises(RuntimeError, match="version 7"):
        setup.adapter.register_model(MODEL_URI, NAME)
    setup.client.create_model_version.assert_called_once()
    setup.client.transition_model_version_stage.assert_not_called()


def test_launcher_uses_configured_name_and_prints_versioned_reference(setup, monkeypatch, capsys):
    monkeypatch.setattr(register_model.config, "load", Mock(return_value=setup.cfg))
    monkeypatch.setattr(register_model, "get_adapter", Mock(return_value=setup.adapter))
    monkeypatch.setattr(sys, "argv", ["register_model", "--model-uri", MODEL_URI])
    register_model.main()
    assert json.loads(capsys.readouterr().out) == {
        "name": NAME, "version": "7", "model_uri": f"models:/{NAME}/7",
    }
    setup.client.create_model_version.assert_called_once()


def test_launcher_requires_explicit_source_before_loading_config(monkeypatch):
    load = Mock()
    monkeypatch.setattr(register_model.config, "load", load)
    monkeypatch.setattr(sys, "argv", ["register_model"])
    with pytest.raises(SystemExit) as caught:
        register_model.main()
    assert caught.value.code == 2
    load.assert_not_called()
