"""Real local model export through the gate into a mock registry; no cloud calls."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import mlflow.sklearn
import numpy as np
import pytest
import yaml

from cloudlayer import azure_registry as registry
from scripts import evaluation_gate as gate
from src import data, train


def test_default_parameters_export_reloadable_model_and_real_metrics(local_job, monkeypatch):
    job = local_job
    original = job.source.read_bytes()
    save = Mock(wraps=mlflow.sklearn.save_model)
    monkeypatch.setattr(mlflow.sklearn, "save_model", save)
    result = train.run_training(job.checked, job.dvc, job.output)
    params = result["params"]
    assert (params["n_estimators"], params["max_depth"], params["min_samples_leaf"]) == (100, 12, 10)
    assert params["random_state"] == result["seed"] == result["python_hash_seed"] == 20260101
    assert result["git_commit"] == "c" * 40 + "-dirty"
    assert result["git_commit_source"] == "local_git"
    assert result["dvc_hash"] == "a" * 32 + ".dir"
    assert result["data_fingerprint"] == hashlib.sha256(original).hexdigest()
    assert result["split_strategy"] == "group_by_machine_id"
    assert result["split_rows"] == {"train": 48, "val": 16, "test": 16}
    assert json.loads((job.output / "metrics.json").read_text()) == result
    model = mlflow.sklearn.load_model(str(job.output / "model"))
    trained = save.call_args.args[0]
    np.testing.assert_allclose(
        model.predict_proba(job.frame[data.FEATURES]),
        trained.predict_proba(job.frame[data.FEATURES]), rtol=0, atol=1e-12,
    )
    _, val_df, test_df = data.split(job.frame, seed=result["seed"])
    for name, part in (("val", val_df), ("test", test_df)):
        proba = model.predict_proba(part[data.FEATURES])[:, 1]
        assert result[f"{name}_roc_auc"] == pytest.approx(
            train.roc_auc_score(part[data.TARGET], proba), abs=1e-12,
        )
        assert result[f"{name}_pr_auc"] == pytest.approx(
            train.average_precision_score(part[data.TARGET], proba), abs=1e-12,
        )
    saved_metadata = yaml.safe_load((job.output / "model" / "MLmodel").read_text())["metadata"]
    assert saved_metadata["git_commit"] == result["git_commit"]
    assert saved_metadata["data_fingerprint"] == result["data_fingerprint"]
    assert saved_metadata == {
        key: value for key, value in result.items() if key != "model_files_sha256"
    }
    assert result["model_files_sha256"] == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (job.output / "model").iterdir()
    }
    assert save.call_args.kwargs["pip_requirements"] == [
        f"{name}=={value}" for name, value in result["package_versions"].items()
    ]
    assert job.source.read_bytes() == original
    assert (job.checked / "sensors.csv").read_bytes() == original
    # Connect this real, tiny training export to a mock registry without another fit.
    client = SimpleNamespace(
        subscription_id=registry.SUBSCRIPTION, resource_group_name=registry.GROUP,
        workspace_name=registry.WORKSPACE, models=Mock(spec=["create_or_update", "get"]),
        datastores=Mock(spec=["get"]),
    )
    from azure.ai.ml import load_datastore

    client.datastores.get.return_value = load_datastore(
        Path(__file__).resolve().parents[2] / "pipeline" / "datastore-artifacts.yaml",
    )
    client.models.create_or_update.return_value = SimpleNamespace(
        name=registry.MODEL_NAME, version="1",
    )
    client.models.get.side_effect = lambda **_: SimpleNamespace(
        name=registry.MODEL_NAME, version="1", type="mlflow_model",
        id=f"{registry.MODEL_ROOT}/versions/1",
        tags=client.models.create_or_update.call_args.args[0].tags.copy(),
    )
    gate_dir = job.output.parent / "gate"
    assert gate.main([
        "--metrics", str(job.output / "metrics.json"), "--output-dir", str(gate_dir),
    ]) == 0
    registered_dir = job.output.parent / "registered"
    assert registry.main([
        "--training-dir", str(job.output), "--gate-report", str(gate_dir / "gate.json"),
        "--output-dir", str(registered_dir),
    ], client=client) == 0
    registered = json.loads((registered_dir / "registration.json").read_text())
    assert registered["model_uri"] == f"azureml:{registry.MODEL_NAME}:1"
    client.models.create_or_update.assert_called_once()
    assert client.models.create_or_update.call_args.args[0].tags["val_roc_auc"] == str(
        result["val_roc_auc"],
    )
