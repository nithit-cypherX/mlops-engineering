"""Study wiring tests: simulated models/clock and temporary local MLflow only."""
from __future__ import annotations

import json
import socket
from argparse import Namespace
from unittest.mock import Mock, create_autospec

import mlflow
import numpy as np
import pandas as pd
import pytest
from mlflow.entities import RunTag
from sklearn.ensemble import RandomForestClassifier

from src import tune


@pytest.fixture
def study(tmp_path, monkeypatch):
    args = Namespace(
        trials=12, budget_thb=20.0, instance="Standard_F2s_v2", trial_estimate_s=600.0,
        seed=20260101, experiment="unit-test-study", checkpoint=tmp_path / "checkpoint.json",
        raw_path=None, dvc_metadata_path=None, checkpoint_key=None, resume_uri=None, image_uri=None,
        test_interruption=False,
    )
    metadata_path = tmp_path / "raw.dvc"
    metadata_path.write_text("outs:\n- path: raw\n  hash: md5\n  md5: " + "a" * 32 + ".dir\n")
    cfg = Namespace(
        provider="azure", raw_path=tmp_path / "unused.csv", dvc_metadata_path=metadata_path,
        mlflow_tracking_uri=f"sqlite:///{tmp_path / 'mlflow.db'}",
    )
    frames = []
    for offset in (100, 200, 300):
        frame = pd.DataFrame(
            {feature: [0.0, 1.0, 2.0, 3.0] for feature in tune.data.FEATURES},
            index=range(offset, offset + 4),
        )
        frame[tune.data.TARGET] = [0, 1, 0, 1]
        frames.append(frame)
    split = Mock(return_value=tuple(frames))
    load = Mock(return_value=frames[0])
    clock = {"seconds": 0.0, "fit_s": 2.0, "save_s": 3.0}
    models = []

    def make_model(**kwargs):
        model = Mock()
        model.get_params.return_value = RandomForestClassifier(**kwargs).get_params(deep=False)

        def fit(*_):
            clock["seconds"] += clock["fit_s"]

        def predict(frame):
            positives = ([0.9, 0.1, 0.2, 0.8] if frame.index[0] == 300
                         else [0.1, 0.9, 0.3, 0.7])
            return np.array([[1 - value, value] for value in positives])

        model.fit.side_effect = fit
        model.predict_proba.side_effect = predict
        models.append(model)
        return model

    def log_model(model, artifact_path):
        clock["seconds"] += clock["save_s"]
        mlflow.log_text("TEST DOUBLE — not a trained model", artifact_path + "/test-double.txt")

    model_factory = Mock(side_effect=make_model)
    model_log = create_autospec(mlflow.sklearn.log_model, side_effect=log_model)
    monkeypatch.chdir(tmp_path)
    for name in ("MLFLOW_RUN_ID", "MLFLOW_EXPERIMENT_ID", "MLFLOW_EXPERIMENT_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tune, "parse_args", lambda: args)
    monkeypatch.setattr(tune.config, "load", lambda **_: cfg)
    monkeypatch.setattr(tune.seeds, "_INITIAL_HASH_SEED", str(args.seed))
    monkeypatch.setattr(tune, "git_commit", lambda: "b" * 40)
    monkeypatch.setattr(tune.data, "data_fingerprint", Mock(return_value="test-data"))
    monkeypatch.setattr(tune.data, "load_raw", load)
    monkeypatch.setattr(tune.data, "split", split)
    monkeypatch.setattr(tune, "RandomForestClassifier", model_factory)
    monkeypatch.setattr(tune.mlflow.sklearn, "log_model", model_log)
    # Replace only tune's clock reference, not the global time module used by MLflow.
    monkeypatch.setattr(tune, "time", Namespace(perf_counter=lambda: clock["seconds"]))
    old_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    client = mlflow.MlflowClient(tracking_uri=cfg.mlflow_tracking_uri)
    try:
        yield Namespace(args=args, cfg=cfg, clock=clock, models=models, factory=model_factory,
                        log_model=model_log, split=split, load=load, client=client, frames=frames)
    finally:
        while mlflow.active_run() is not None:
            mlflow.end_run()
        mlflow.set_tracking_uri(old_uri)


def runs(study, role):
    experiment = study.client.get_experiment_by_name(study.args.experiment)
    return study.client.search_runs(
        [experiment.experiment_id], filter_string=f"tags.run_role = '{role}'",
    )


def test_grid_contains_the_twelve_agreed_configurations():
    candidates = tune.grid(tune.SEARCH_SPACE)
    assert len(candidates) == len({json.dumps(p, sort_keys=True) for p in candidates}) == 12
    assert {p["n_estimators"] for p in candidates} == {100, 300}
    assert {p["max_depth"] for p in candidates} == {4, 8, 12}
    assert {p["min_samples_leaf"] for p in candidates} == {1, 10}


@pytest.mark.parametrize("extra", [
    ["--budget-thb", "0", "--instance", "Standard_F2s_v2"],
    ["--budget-thb", "-1", "--instance", "Standard_F2s_v2"],
    ["--budget-thb", "nan", "--instance", "Standard_F2s_v2"],
    ["--budget-thb", "inf", "--instance", "Standard_F2s_v2"],
    ["--budget-thb", "151", "--instance", "Standard_F2s_v2"],
    ["--instance", "Standard_F2s_v2"],
    ["--budget-thb", "20"],
    ["--budget-thb", "20", "--instance", "Standard_F2s_v2", "--trials", "13"],
    ["--budget-thb", "20", "--instance", "Standard_F2s_v2", "--trial-estimate-s", "nan"],
    ["--budget-thb", "20", "--instance", "Standard_F2s_v2", "--seed", "-1"],
])
def test_invalid_or_missing_cli_input_is_rejected(monkeypatch, extra):
    monkeypatch.setattr("sys.argv", ["tune", *extra])
    with pytest.raises(SystemExit) as error:
        tune.parse_args()
    assert error.value.code == 2


def test_cli_has_no_implicit_budget_or_instance(monkeypatch):
    monkeypatch.setattr("sys.argv", ["tune", "--budget-thb", "7", "--instance", "Standard_F2s_v2"])
    args = tune.parse_args()
    assert args.budget_thb == 7
    assert args.instance == "Standard_F2s_v2"
    assert args.trials == 12
    assert args.seed == 20260101
    assert args.trial_estimate_s == 600
    assert args.test_interruption is False


@pytest.mark.parametrize("options,valid", [
    ([], False),
    (["--checkpoint-key", "studies/new-job/checkpoint.json",
      "--image-uri", "registry.example.invalid/lab2@sha256:" + "a" * 64], True),
    (["--checkpoint-key", "studies/new-job/checkpoint.json",
      "--image-uri", "registry.example.invalid/lab2@sha256:" + "a" * 64,
      "--trials", "4"], False),
    (["--checkpoint-key", "studies/new-job/checkpoint.json",
      "--image-uri", "registry.example.invalid/lab2@sha256:" + "a" * 64,
      "--resume-uri", "mock://old-job/checkpoint.json"], False),
])
def test_interruption_cli_requires_a_new_cloud_study_with_work_left(monkeypatch, options, valid):
    monkeypatch.setattr("sys.argv", ["tune", "--budget-thb", "20", "--instance", "Standard_F2s_v2",
                                    "--test-interruption", *options])
    if valid:
        assert tune.parse_args().test_interruption is True
    else:
        with pytest.raises(SystemExit) as error:
            tune.parse_args()
        assert error.value.code == 2


def test_twelve_trials_have_separate_complete_runs_and_one_split(study, monkeypatch):
    # Simulate the MLflow run ID supplied by a managed job, without contacting Azure.
    experiment = mlflow.set_experiment(study.args.experiment)
    parent = study.client.create_run(experiment.experiment_id)
    monkeypatch.setenv("MLFLOW_RUN_ID", parent.info.run_id)
    tune.main()

    parents = runs(study, "study")
    children = runs(study, "trial")
    assert [run.info.run_id for run in parents] == [parent.info.run_id]
    assert len(children) == len({run.info.run_id for run in children}) == 12
    assert {run.data.tags["mlflow.parentRunId"] for run in children} == {parent.info.run_id}
    assert all(run.info.status == "FINISHED" for run in children)
    assert len({run.data.tags["study_id"] for run in children}) == 1
    study.load.assert_called_once_with(study.cfg.raw_path)
    study.split.assert_called_once_with(study.frames[0], seed=20260101)
    assert study.factory.call_count == study.log_model.call_count == 12
    by_params = {
        (int(r.data.params["n_estimators"]), int(r.data.params["max_depth"]),
         int(r.data.params["min_samples_leaf"])): r for r in children
    }
    for model, params in zip(study.models, tune.grid(tune.SEARCH_SPACE)):
        run = by_params[(params["n_estimators"], params["max_depth"], params["min_samples_leaf"])]
        for name, value in model.get_params.return_value.items():
            assert run.data.params[name] == str(value)
        assert run.data.params["seed"] == run.data.params["python_hash_seed"] == "20260101"
        assert run.data.params["instance"] == "Standard_F2s_v2"
        assert float(run.data.params["hourly_rate_thb"]) == pytest.approx(2.900677)
        assert run.data.tags["git_commit"] == "b" * 40
        assert run.data.tags["dvc_hash"] == "a" * 32 + ".dir"
        assert run.data.tags["data_fingerprint"] == "test-data"
        assert run.data.tags["split_strategy"] == "group_by_machine_id"
        assert set(run.data.metrics) == {
            "val_roc_auc", "val_pr_auc", "test_roc_auc", "test_pr_auc", "duration_s", "cost_thb",
        }
        assert run.data.metrics["val_roc_auc"] == 1.0
        assert run.data.metrics["test_roc_auc"] == 0.25
        assert run.data.metrics["duration_s"] == 5.0  # 2s fit + 3s model log.
        assert run.data.metrics["cost_thb"] == pytest.approx(5 / 3600 * 2.900677)
        assert study.client.list_artifacts(run.info.run_id, "model")[0].path == "model/test-double.txt"
    for call in study.log_model.call_args_list:
        assert call.kwargs == {"artifact_path": "model"}
    state = json.loads(study.args.checkpoint.read_text())
    assert len(state["completed"]) == 12
    assert state["spent_thb"] == pytest.approx(60 / 3600 * 2.900677)


def test_nested_run_tags_are_rest_serializable(study, monkeypatch):
    study.args.trials = 1
    experiment = mlflow.set_experiment(study.args.experiment)
    parent = study.client.create_run(experiment.experiment_id)
    monkeypatch.setenv("MLFLOW_RUN_ID", parent.info.run_id)
    original_create_run = mlflow.MlflowClient.create_run
    serialized_tags = []

    def create_run_with_rest_tags(client, *args, **kwargs):
        # Exercise the same serializer as RestStore before SQLite can coerce values.
        serialized_tags.append({
            key: RunTag(key, value).to_proto().value for key, value in kwargs["tags"].items()
        })
        return original_create_run(client, *args, **kwargs)

    monkeypatch.setattr(mlflow.MlflowClient, "create_run", create_run_with_rest_tags)
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    tune.main()

    assert len(serialized_tags) == 1
    tags = serialized_tags[0]
    assert tags["mlflow.parentRunId"] == parent.info.run_id
    assert tags["run_role"] == "trial"
    expected_rows = dict(zip(
        ("n_train_rows", "n_val_rows", "n_test_rows"),
        (str(len(frame)) for frame in study.frames),
    ))
    for key, value in expected_rows.items():
        assert tags[key] == value
    children = runs(study, "trial")
    assert len(children) == 1 and children[0].info.status == "FINISHED"


def test_budget_guard_skips_before_first_trial(study, capsys):
    study.args.budget_thb = 0.1  # Less than the 600-second initial estimate.
    tune.main()
    study.factory.assert_not_called()
    assert runs(study, "trial") == []
    assert "12 configurations not run" in capsys.readouterr().out


def test_budget_guard_allows_exact_boundary_then_stops(study):
    study.args.budget_thb = 600 / 3600 * 2.900677
    tune.main()
    assert study.factory.call_count == 1
    assert len(runs(study, "trial")) == 1


def test_next_trial_estimate_increases_after_a_slow_trial(study):
    study.args.budget_thb = 0.9
    study.clock["fit_s"] = 397.0  # Total 400s: next estimate grows from 600s to 800s.
    # A stale 600s estimate would fit, but the updated 800s estimate must not.
    assert (400 + 600) / 3600 * 2.900677 < 0.9 < (400 + 800) / 3600 * 2.900677
    tune.main()
    assert study.factory.call_count == 1
    state = json.loads(study.args.checkpoint.read_text())
    assert state["max_duration_s"] == 400
    assert state["spent_thb"] == pytest.approx(400 / 3600 * 2.900677)


def test_overrun_is_reported_and_no_further_trial_starts(study, capsys):
    study.args.budget_thb = 0.5  # First estimate fits, but the actual attempt takes longer.
    study.clock["fit_s"] = 997.0
    tune.main()
    assert study.factory.call_count == 1
    assert "observed duration exceeded the estimate" in capsys.readouterr().out
    assert json.loads(study.args.checkpoint.read_text())["spent_thb"] > 0.5


def test_failed_model_logging_keeps_spend_but_does_not_mark_completed(study):
    def fail(model, artifact_path):
        study.clock["seconds"] += 3
        raise RuntimeError("simulated model upload failure")

    study.log_model.side_effect = fail
    with pytest.raises(RuntimeError, match="simulated model upload failure"):
        tune.main()
    state = json.loads(study.args.checkpoint.read_text())
    assert state["completed"] == []
    assert state["spent_thb"] == pytest.approx(5 / 3600 * 2.900677)
    failed = runs(study, "trial")[0]
    assert failed.info.status == "FAILED"
    assert failed.data.metrics["duration_s"] == 5
    assert failed.data.metrics["cost_thb"] == pytest.approx(5 / 3600 * 2.900677)


def test_unknown_instance_fails_before_data_or_model_work(study):
    study.args.instance = "misspelled-instance"
    with pytest.raises(KeyError, match="No rate for"):
        tune.main()
    study.load.assert_not_called()
    study.factory.assert_not_called()


def test_wrong_hash_seed_fails_before_data_or_model_work(study, monkeypatch):
    monkeypatch.setattr(tune.seeds, "_INITIAL_HASH_SEED", "42")
    with pytest.raises(RuntimeError, match="Set PYTHONHASHSEED"):
        tune.main()
    study.load.assert_not_called()
    study.factory.assert_not_called()


def test_local_checkpoint_skips_done_trials_and_rejects_changed_data(study, monkeypatch):
    study.args.trials = 1
    tune.main()
    saved = study.args.checkpoint.read_text()
    tune.main()
    assert study.factory.call_count == 1
    assert study.args.checkpoint.read_text() == saved
    monkeypatch.setattr(tune.data, "data_fingerprint", lambda _: "different-data")
    with pytest.raises(ValueError, match="Checkpoint does not match"):
        tune.main()
    assert study.factory.call_count == 1


@pytest.mark.parametrize("field,bad_value", [
    ("spent_thb", -1), ("spent_thb", float("nan")), ("max_duration_s", None),
    ("completed", ["not-a-configuration"]),
    ("schema_version", 0), ("schema_version", True), ("completed_runs", None),
    ("completed_runs", {"wrong-key": "run-id"}), ("in_progress", {"run_id": "missing-key"}),
])
def test_invalid_checkpoint_fails_closed(tmp_path, field, bad_value):
    path = tmp_path / "checkpoint.json"
    context = {"seed": 20260101}
    state = tune.load_checkpoint(path, context)
    state[field] = bad_value
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="Invalid checkpoint"):
        tune.load_checkpoint(path, context)


@pytest.fixture
def cloud_study(study, monkeypatch, tmp_path):
    """Real local MLflow, fake Blob bytes; no developer credentials or cloud calls."""
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    study.args.checkpoint_key = "studies/job-a/checkpoint.json"
    study.args.image_uri = "registry.example.invalid/lab2@sha256:" + "a" * 64
    study.cfg.blob_uri = "mock://lab2"
    blobs = {}
    snapshots = []

    def upload(path, key):
        content = tune.Path(path).read_bytes()
        uri = study.cfg.blob_uri + "/" + key
        blobs[uri] = content
        snapshots.append(json.loads(content))
        return uri

    def download(uri, path):
        if uri not in blobs:
            raise FileNotFoundError("Remote checkpoint missing")
        destination = tune.Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blobs[uri])

    adapter = Mock(upload=Mock(side_effect=upload), download=Mock(side_effect=download))
    factory = Mock(return_value=adapter)
    monkeypatch.setattr(tune, "get_adapter", factory)

    def resume():
        study.args.resume_uri = study.cfg.blob_uri + "/" + study.args.checkpoint_key
        study.args.checkpoint_key = "studies/job-b/checkpoint.json"
        study.args.checkpoint = tmp_path / "fresh-job" / "checkpoint.json"

    return Namespace(study=study, blobs=blobs, snapshots=snapshots, adapter=adapter,
                     factory=factory, upload=upload, resume=resume)


def test_cloud_checkpoints_are_written_before_work_and_after_finished_runs(cloud_study):
    cloud = cloud_study
    study = cloud.study
    study.args.trials = 1
    tune.main()
    initial, pending, measured, completed = cloud.snapshots
    run = runs(study, "trial")[0]
    assert initial["completed"] == [] and initial["in_progress"] is None
    assert pending["in_progress"]["run_id"] == run.info.run_id
    assert pending["spent_thb"] == 0
    assert measured["in_progress"] is None and measured["completed"] == []
    assert measured["spent_thb"] == pytest.approx(5 / 3600 * 2.900677)
    assert list(completed["completed_runs"].values()) == [run.info.run_id]
    assert run.info.status == "FINISHED"
    assert run.data.tags["checkpoint_uri"] == "mock://lab2/studies/job-a/checkpoint.json"
    assert run.data.tags["image_uri"] == study.args.image_uri
    assert completed["context"]["budget_thb"] == 20
    assert completed["context"]["image_uri"] == study.args.image_uri


@pytest.mark.parametrize("interruption", [False, True])
def test_fresh_job_resumes_four_completed_trials_without_refitting_them(
    cloud_study, capsys, interruption,
):
    cloud = cloud_study
    study = cloud.study
    study.args.trials = 12 if interruption else 4
    study.args.test_interruption = interruption
    if interruption:
        with pytest.raises(RuntimeError, match="CONTROLLED INTERRUPTION TEST"):
            tune.main()
    else:
        tune.main()
    assert study.factory.call_count == study.log_model.call_count == 4
    assert len(runs(study, "trial")) == 4
    assert all(run.info.status == "FINISHED" for run in runs(study, "trial"))
    assert runs(study, "study")[0].info.status == ("FAILED" if interruption else "FINISHED")
    source_uri = "mock://lab2/studies/job-a/checkpoint.json"
    source_bytes = cloud.blobs[source_uri]
    first_state = json.loads(source_bytes)
    assert len(first_state["completed"]) == 4
    assert first_state["in_progress"] is None
    assert first_state["spent_thb"] == pytest.approx(20 / 3600 * 2.900677)
    first_ids = set(first_state["completed_runs"].values())
    study.args.checkpoint.unlink()  # Simulate loss of the old job's local filesystem.
    cloud.resume()
    study.args.trials = 12
    study.args.test_interruption = False  # The deliberate failure is opt-in, never inherited.
    assert not study.args.checkpoint.exists()
    tune.main()
    cloud.adapter.download.assert_called_once_with(source_uri, str(study.args.checkpoint))
    assert study.factory.call_count == 12  # Four before interruption, eight in the new job.
    final = json.loads(cloud.blobs["mock://lab2/studies/job-b/checkpoint.json"])
    assert final["study_id"] == first_state["study_id"]
    assert len(final["completed"]) == len(final["completed_runs"]) == 12
    assert first_ids <= set(final["completed_runs"].values())
    assert final["spent_thb"] == pytest.approx(60 / 3600 * 2.900677)
    assert len(runs(study, "trial")) == 12
    assert cloud.blobs[source_uri] == source_bytes  # Never write to the predecessor's checkpoint.
    output = capsys.readouterr().out
    assert output.count("already done, skipping") == 4
    assert output.count("CONTROLLED INTERRUPTION TEST") == int(interruption)
    parents = runs(study, "study")
    assert sum(run.info.status == "FINISHED" for run in parents) == (1 if interruption else 2)


def test_interruption_marker_requires_a_successful_fourth_completion_upload(cloud_study, capsys):
    cloud = cloud_study
    cloud.study.args.test_interruption = True

    def fail_fourth_completion(path, key):
        state = json.loads(tune.Path(path).read_text())
        if len(state["completed"]) == 4:
            raise PermissionError("Fourth completion upload denied")
        return cloud.upload(path, key)

    cloud.adapter.upload.side_effect = fail_fourth_completion
    with pytest.raises(PermissionError, match="Fourth completion upload denied"):
        tune.main()
    assert cloud.study.factory.call_count == 4
    saved = json.loads(cloud.blobs["mock://lab2/studies/job-a/checkpoint.json"])
    assert len(saved["completed"]) == 3
    assert saved["spent_thb"] == pytest.approx(20 / 3600 * 2.900677)
    assert "CONTROLLED INTERRUPTION TEST" not in capsys.readouterr().out


def test_interruption_option_does_not_bypass_the_budget_guard(cloud_study, capsys):
    study = cloud_study.study
    study.args.test_interruption = True
    study.args.budget_thb = 600 / 3600 * 2.900677
    tune.main()
    assert study.factory.call_count == 1
    output = capsys.readouterr().out
    assert "11 configurations not run" in output
    assert "CONTROLLED INTERRUPTION TEST" not in output


@pytest.mark.parametrize("failure", ["missing", "corrupt", "permission"])
def test_resume_never_falls_back_to_stale_local_state(cloud_study, failure):
    cloud = cloud_study
    study = cloud.study
    study.args.trials = 1
    tune.main()
    cloud.resume()
    study.args.checkpoint.parent.mkdir()
    study.args.checkpoint.write_bytes(b"stale local checkpoint must not be used")
    if failure == "missing":
        cloud.blobs.clear()
        expected = FileNotFoundError
    elif failure == "corrupt":
        cloud.blobs[study.args.resume_uri] = b"{broken"
        expected = json.JSONDecodeError
    else:
        cloud.adapter.download.side_effect = PermissionError("Access denied")
        expected = PermissionError
    uploads = cloud.adapter.upload.call_count
    with pytest.raises(expected):
        tune.main()
    assert study.factory.call_count == 1
    assert cloud.adapter.upload.call_count == uploads


@pytest.mark.parametrize("changed", ["code", "data", "seed", "grid", "image", "budget", "price"])
def test_resume_context_must_match_before_any_new_training(cloud_study, monkeypatch, changed):
    cloud = cloud_study
    study = cloud.study
    study.args.trials = 1
    tune.main()
    cloud.resume()
    if changed == "code":
        monkeypatch.setattr(tune, "git_commit", lambda: "c" * 40)
    elif changed == "data":
        monkeypatch.setattr(tune.data, "data_fingerprint", lambda _: "changed-data")
    elif changed == "seed":
        study.args.seed = 42
        monkeypatch.setattr(tune.seeds, "_INITIAL_HASH_SEED", "42")
    elif changed == "grid":
        monkeypatch.setattr(tune, "SEARCH_SPACE", {**tune.SEARCH_SPACE, "max_depth": [4, 8]})
    elif changed == "image":
        study.args.image_uri = "registry.example.invalid/lab2@sha256:" + "b" * 64
    elif changed == "budget":
        study.args.budget_thb = 21
    else:
        monkeypatch.setattr(tune.costs, "hourly_rate", lambda *_args, **_kwargs: 3.0)
    uploads = cloud.adapter.upload.call_count
    with pytest.raises(ValueError, match="Checkpoint does not match"):
        tune.main()
    assert study.factory.call_count == 1
    assert cloud.adapter.upload.call_count == uploads


@pytest.mark.parametrize("failure_at,expected_models", [(1, 0), (2, 0), (3, 1), (4, 1)])
def test_upload_failure_stops_before_the_next_trial(cloud_study, failure_at, expected_models):
    cloud = cloud_study
    calls = 0

    def fail_selected_upload(path, key):
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise PermissionError("Checkpoint upload denied")
        return cloud.upload(path, key)

    cloud.adapter.upload.side_effect = fail_selected_upload
    with pytest.raises(PermissionError, match="Checkpoint upload denied"):
        tune.main()
    assert calls == failure_at
    assert cloud.study.factory.call_count == expected_models


@pytest.mark.parametrize("failure_at", [3, 4])
def test_resume_preserves_cost_when_final_checkpoint_upload_was_lost(cloud_study, failure_at):
    cloud = cloud_study
    study = cloud.study
    study.args.trials = 1
    calls = 0

    def fail_selected_upload(path, key):
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise PermissionError("Checkpoint upload denied")
        return cloud.upload(path, key)

    cloud.adapter.upload.side_effect = fail_selected_upload
    with pytest.raises(PermissionError):
        tune.main()
    cloud.adapter.upload.side_effect = cloud.upload
    cloud.resume()
    tune.main()
    state = json.loads(study.args.checkpoint.read_text())
    assert study.factory.call_count == 2  # No durable completion record, so repeat only this config.
    assert state["spent_thb"] == pytest.approx(10 / 3600 * 2.900677)
    assert len(state["completed"]) == 1
    assert state["in_progress"] is None


@pytest.mark.parametrize("status,metrics", [
    ("RUNNING", {}), ("KILLED", {}), ("FAILED", {"duration_s": 5.0, "cost_thb": 0.0}),
    ("FAILED", {"duration_s": float("nan"), "cost_thb": 0.1}),
])
def test_lost_interruption_cost_blocks_resume_instead_of_counting_zero(
    cloud_study, monkeypatch, status, metrics,
):
    cloud = cloud_study
    study = cloud.study
    study.args.trials = 1
    tune.main()
    pending = cloud.snapshots[1]  # Last durable state before the simulated hard interruption.
    run = study.client.get_run(pending["in_progress"]["run_id"])
    cloud.blobs["mock://lab2/studies/job-a/checkpoint.json"] = json.dumps(pending).encode()
    client = Mock()
    client.get_run.return_value = Namespace(info=Namespace(status=status),
                                           data=Namespace(tags=run.data.tags, metrics=metrics))
    monkeypatch.setattr(tune.mlflow, "MlflowClient", Mock(return_value=client))
    cloud.resume()
    uploads = cloud.adapter.upload.call_count
    with pytest.raises(RuntimeError, match="Reconcile interrupted trial"):
        tune.main()
    assert study.factory.call_count == 1
    assert cloud.adapter.upload.call_count == uploads
    client.get_run.assert_called_once_with(pending["in_progress"]["run_id"])


def test_new_cloud_study_does_not_silently_reuse_an_existing_local_checkpoint(cloud_study):
    cloud = cloud_study
    cloud.study.args.checkpoint.write_bytes(b"existing state")
    with pytest.raises(ValueError, match="fresh local checkpoint"):
        tune.main()
    cloud.study.factory.assert_not_called()
    cloud.adapter.upload.assert_not_called()


def test_resume_cannot_write_back_to_its_source_checkpoint(cloud_study):
    cloud = cloud_study
    cloud.study.args.resume_uri = "mock://lab2/" + cloud.study.args.checkpoint_key
    with pytest.raises(ValueError, match="not overwrite its source"):
        tune.main()
    cloud.adapter.download.assert_not_called()
    cloud.adapter.upload.assert_not_called()


@pytest.mark.parametrize("options", [
    ["--resume-uri", "mock://source"],
    ["--checkpoint-key", "../checkpoint.json"],
    ["--checkpoint-key", "studies/new-job/checkpoint.json"],
    ["--checkpoint-key", "studies/new-job/checkpoint.json", "--image-uri", "image:latest"],
])
def test_cloud_checkpoint_cli_contract_rejects_incomplete_or_unsafe_options(monkeypatch, options):
    monkeypatch.setattr("sys.argv", ["tune", "--budget-thb", "20", "--instance", "Standard_F2s_v2",
                                    *options])
    with pytest.raises(SystemExit) as caught:
        tune.parse_args()
    assert caught.value.code == 2


def test_interrupted_cost_recovery_rejects_a_run_from_different_lineage():
    state = {"study_id": "study-a", "context": {
        "git_commit": "a" * 40, "dvc_hash": "b" * 32 + ".dir", "data_fingerprint": "data-a",
    }, "in_progress": {"key": "unused", "run_id": "wrong-run"}, "spent_thb": 1.0}
    client = Mock()
    client.get_run.return_value = Namespace(data=Namespace(tags={"study_id": "other-study"}))
    with pytest.raises(ValueError, match="lineage does not match"):
        tune.recover_interrupted_cost(state, client, 2.900677)
    assert state["spent_thb"] == 1.0
    assert state["in_progress"]["run_id"] == "wrong-run"
