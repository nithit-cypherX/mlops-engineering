"""Local path/export checks and MLflow integration; Azure is never contacted."""
import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import numpy as np
import pandas as pd
import pytest

from src import config, data, seeds, train


def test_path_arguments_are_optional(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["train"])
    args = train.parse_args()
    assert (args.raw_path, args.dvc_metadata_path, args.output_dir, args.metrics_out) == (
        None, None, None, None,
    )
    assert (args.n_estimators, args.max_depth, args.min_samples_leaf, args.seed) == (
        200, 8, 5, seeds.DEFAULT_SEED,
    )


def test_path_arguments_accept_paths_with_spaces(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("input data.csv", "input metadata.dvc", "job output")]
    monkeypatch.setattr(sys, "argv", [
        "train", "--raw-path", str(paths[0]), "--dvc-metadata-path", str(paths[1]),
        "--output-dir", str(paths[2]),
    ])
    args = train.parse_args()
    assert (args.raw_path, args.dvc_metadata_path, args.output_dir) == tuple(paths)


@pytest.fixture
def local_training(tmp_path, monkeypatch, request):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    monkeypatch.setattr(seeds, "_INITIAL_HASH_SEED", str(seeds.DEFAULT_SEED))
    monkeypatch.chdir(tmp_path)
    frame = pd.DataFrame({
        data.ID: np.arange(40), data.GROUP: np.repeat(np.arange(10), 4),
        data.TARGET: np.tile([0, 1], 20),
        **{name: np.tile([0.1, 0.9], 20) for name in data.FEATURES},
    })
    cfg = SimpleNamespace(
        raw_path=tmp_path / "default-data" / "raw" / "sensors.csv",
        dvc_metadata_path=tmp_path / "default-data" / "raw.dvc",
        mlflow_tracking_uri=f"sqlite:///{tmp_path / 'tracking.db'}",
    )
    custom_raw = tmp_path / "job inputs" / "sensors.csv"
    custom_dvc = tmp_path / "job inputs" / "raw.dvc"
    for raw, metadata, version in (
        (cfg.raw_path, cfg.dvc_metadata_path, "a"), (custom_raw, custom_dvc, "b"),
    ):
        raw.parent.mkdir(parents=True)
        frame.to_csv(raw, index=False)
        metadata.write_text("outs:\n- path: raw\n  hash: md5\n  md5: " + version * 32 + ".dir\n")
    monkeypatch.setattr(train.config, "load", lambda **kwargs: cfg)
    monkeypatch.setattr(config, "IMAGE_GIT_COMMIT", "c" * 40)
    monkeypatch.setattr(train.data, "load_raw", Mock(wraps=data.load_raw))
    monkeypatch.setattr(train.data, "data_fingerprint", Mock(wraps=data.data_fingerprint))
    monkeypatch.setattr(train.data, "split", Mock(wraps=data.split))
    monkeypatch.setattr(train, "RandomForestClassifier", Mock(wraps=train.RandomForestClassifier))
    # Most tests mock tracking; the integration case uses real local MLflow.
    real_tracking = getattr(request, "param", False)
    real_set_uri = train.mlflow.set_tracking_uri
    old_uri = train.mlflow.get_tracking_uri()
    real_set_uri(cfg.mlflow_tracking_uri)
    for name in ("set_tracking_uri", "set_experiment", "log_params", "set_tags", "log_metrics"):
        original = getattr(train.mlflow, name)
        monkeypatch.setattr(train.mlflow, name, Mock(wraps=original if real_tracking else None))
    if not real_tracking:
        monkeypatch.setattr(train.mlflow, "start_run", MagicMock())
    monkeypatch.setattr(
        train.mlflow.sklearn, "log_model",
        Mock(wraps=train.mlflow.sklearn.log_model if real_tracking else None),
    )
    monkeypatch.setattr(train.mlflow.sklearn, "save_model", Mock(wraps=train.mlflow.sklearn.save_model))
    argv = ["train", "--n-estimators", "2", "--max-depth", "2", "--min-samples-leaf", "1"]
    monkeypatch.setattr(sys, "argv", argv)
    try:
        yield SimpleNamespace(cfg=cfg, raw=custom_raw, dvc=custom_dvc, argv=argv, frame=frame)
    finally:
        real_set_uri(old_uri)


@pytest.mark.parametrize("local_training", [True], indirect=True, ids=["real-local-mlflow"])
def test_training_logs_reloadable_model_to_local_mlflow(local_training):
    setup = local_training
    experiment_name = "lab2-model-logging-check"
    setup.argv.extend(["--experiment", experiment_name, "--run-name", "local-api-check"])

    train.main()

    client = train.mlflow.MlflowClient(tracking_uri=setup.cfg.mlflow_tracking_uri)
    experiment = client.get_experiment_by_name(experiment_name)
    runs = client.search_runs([experiment.experiment_id])
    assert len(runs) == 1
    run = runs[0]
    assert run.info.status == "FINISHED"
    assert run.data.params["seed"] == str(seeds.DEFAULT_SEED)
    assert run.data.tags["git_commit"] == "c" * 40
    assert run.data.tags["dvc_hash"] == "a" * 32 + ".dir"
    assert run.data.tags["data_fingerprint"] == data.data_fingerprint(setup.cfg.raw_path)
    assert set(run.data.metrics) == {
        "val_roc_auc", "val_pr_auc", "test_roc_auc", "test_pr_auc",
    }
    assert "model/MLmodel" in {
        item.path for item in client.list_artifacts(run.info.run_id, "model")
    }
    restored = train.mlflow.sklearn.load_model(f"runs:/{run.info.run_id}/model")
    trained = train.mlflow.sklearn.log_model.call_args.args[0]
    train.mlflow.sklearn.log_model.assert_called_once_with(trained, artifact_path="model")
    np.testing.assert_array_equal(
        restored.predict_proba(setup.frame[data.FEATURES]),
        trained.predict_proba(setup.frame[data.FEATURES]),
    )


@pytest.mark.parametrize("also_legacy_metrics", [False, True])
def test_custom_inputs_export_reloadable_model_and_metrics(
    local_training, tmp_path, also_legacy_metrics,
):
    setup = local_training
    output = tmp_path / "job outputs" / "artifacts"
    legacy = tmp_path / "old reports" / "score.json"
    setup.argv.extend([
        "--raw-path", str(setup.raw), "--dvc-metadata-path", str(setup.dvc),
        "--output-dir", str(output),
    ])
    if also_legacy_metrics:
        setup.argv.extend(["--metrics-out", str(legacy)])
    train.main()
    train.data.load_raw.assert_called_once_with(setup.raw)
    train.data.data_fingerprint.assert_called_once_with(setup.raw)
    assert train.mlflow.set_tags.call_args.args[0]["dvc_hash"] == "b" * 32 + ".dir"
    assert train.mlflow.set_tags.call_args.args[0]["git_commit"] == "c" * 40
    assert train.data.split.call_args.kwargs == {"seed": seeds.DEFAULT_SEED}
    train.RandomForestClassifier.assert_called_once_with(
        n_estimators=2, max_depth=2, min_samples_leaf=1,
        random_state=seeds.DEFAULT_SEED, n_jobs=-1,
    )
    assert (output / "model" / "MLmodel").is_file()
    restored = train.mlflow.sklearn.load_model(str(output / "model"))
    trained = train.mlflow.sklearn.log_model.call_args.args[0]
    np.testing.assert_array_equal(
        restored.predict_proba(setup.frame[data.FEATURES]),
        trained.predict_proba(setup.frame[data.FEATURES]),
    )
    result = json.loads((output / "metrics.json").read_text())
    assert result == {
        "seed": seeds.DEFAULT_SEED,
        "data_fingerprint": train.mlflow.set_tags.call_args.args[0]["data_fingerprint"],
        **train.mlflow.log_metrics.call_args.args[0],
    }
    assert set(train.mlflow.log_metrics.call_args.args[0]) == {
        "val_roc_auc", "val_pr_auc", "test_roc_auc", "test_pr_auc",
    }
    if also_legacy_metrics:
        assert json.loads(legacy.read_text()) == result
    else:
        assert not legacy.exists()


def test_default_inputs_and_legacy_metrics_still_work(local_training, tmp_path):
    setup = local_training
    metrics = tmp_path / "reports" / "legacy.json"
    setup.argv.extend(["--metrics-out", str(metrics)])
    train.main()
    train.data.load_raw.assert_called_once_with(setup.cfg.raw_path)
    train.data.data_fingerprint.assert_called_once_with(setup.cfg.raw_path)
    assert train.mlflow.set_tags.call_args.args[0]["dvc_hash"] == "a" * 32 + ".dir"
    train.mlflow.sklearn.save_model.assert_not_called()
    train.mlflow.sklearn.log_model.assert_called_once()
    result = json.loads(metrics.read_text())
    assert result["seed"] == seeds.DEFAULT_SEED
    for name, value in train.mlflow.log_metrics.call_args.args[0].items():
        assert result[name] == value


@pytest.mark.parametrize("flag,error", [
    ("--raw-path", FileNotFoundError), ("--dvc-metadata-path", RuntimeError),
])
def test_missing_explicit_input_fails_without_falling_back(
    local_training, tmp_path, flag, error,
):
    local_training.argv.extend([flag, str(tmp_path / "missing-input")])
    with pytest.raises(error):
        train.main()
    train.RandomForestClassifier.assert_not_called()
    train.mlflow.start_run.assert_not_called()


def test_output_write_failure_is_not_reported_as_success(local_training, tmp_path):
    output = tmp_path / "occupied-output"
    output.write_text("existing file")
    local_training.argv.extend(["--output-dir", str(output)])
    with pytest.raises(FileExistsError):
        train.main()
    assert output.read_text() == "existing file"
    train.mlflow.sklearn.save_model.assert_not_called()
