"""Local training checks; temporary data/models only, with network calls blocked."""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import mlflow.sklearn
import numpy as np
import pandas as pd
import pytest
import yaml

from src import data, seeds, train
from src.data_steps import ingest, validate
from cloudlayer import azure_registry as registry
from scripts import evaluation_gate as gate


@pytest.fixture
def frame():
    rows = 80
    return pd.DataFrame({
        "reading_id": np.arange(rows),
        "machine_id": np.repeat(np.arange(20), 4),
        "temp_c": np.tile([20.0, 50.0, 30.0, 60.0], 20),
        "vibration_mm_s": np.tile([1.0, 4.0, 2.0, 5.0], 20),
        "pressure_kpa": np.full(rows, 100.0),
        "hours_since_service": np.arange(rows, dtype=float),
        "load_pct": np.tile([30.0, 80.0, 40.0, 90.0], 20),
        "ambient_humidity": np.full(rows, 50.0),
        "failed_within_7d": np.tile([0, 1, 0, 1], 20),
    })


@pytest.fixture
def local_job(frame, tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "https://tracking.invalid")
    monkeypatch.setattr(seeds, "_INITIAL_HASH_SEED", str(seeds.DEFAULT_SEED))
    monkeypatch.setattr(train, "git_commit", lambda supplied=None: supplied or "c" * 40 + "-dirty")
    for target, name in (
        (mlflow, "start_run"), (mlflow, "register_model"), (mlflow.sklearn, "log_model"),
    ):
        monkeypatch.setattr(target, name, Mock(side_effect=AssertionError("Local export only")))
    source = tmp_path / "source.csv"
    frame.to_csv(source, index=False)
    checked = tmp_path / "validated"
    raw = tmp_path / "ingested"
    ingest(source, raw)
    validate(raw / "sensors.csv", checked)
    metadata = tmp_path / "raw.dvc"
    metadata.write_text("outs:\n- path: raw\n  hash: md5\n  md5: " + "a" * 32 + ".dir\n")
    return SimpleNamespace(
        source=source, checked=checked, dvc=metadata, output=tmp_path / "training",
        frame=frame, report=checked / "validation.json",
    )


def run_small(job):
    return train.run_training(
        job.checked, job.dvc, job.output, n_estimators=2, max_depth=2, min_samples_leaf=1,
    )


def assert_blocked_before_fit(job, monkeypatch, expected, match=None):
    constructor = Mock(side_effect=AssertionError("Training must not start"))
    monkeypatch.setattr(train, "RandomForestClassifier", constructor)
    with pytest.raises(expected, match=match):
        run_small(job)
    constructor.assert_not_called()
    assert not (job.output / "metrics.json").exists()
    assert not (job.output / "model").exists()


def test_group_split_is_disjoint_complete_and_deterministic(frame):
    first = data.split(frame, seed=20260101)
    again = data.split(frame, seed=20260101)
    groups = [set(part[data.GROUP]) for part in first]
    assert not groups[0] & groups[1]
    assert not groups[0] & groups[2]
    assert not groups[1] & groups[2]
    assert [len(part) for part in first] == [48, 16, 16]
    assert sorted(pd.concat(first)[data.ID].tolist()) == frame[data.ID].tolist()
    for left, right in zip(first, again):
        pd.testing.assert_frame_equal(left, right)
    changed, _, _ = data.split(frame, seed=7)
    assert changed[data.ID].tolist() != first[0][data.ID].tolist()
    shuffled, _, _ = data.split(frame.sample(frac=1, random_state=3), seed=20260101)
    pd.testing.assert_frame_equal(shuffled, first[0])


def test_split_rejects_too_few_machines(frame):
    with pytest.raises(ValueError, match="empty"):
        data.split(frame[frame[data.GROUP] == 0], seed=20260101)


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


@pytest.mark.parametrize("filename", ["validation.json", "sensors.csv"])
def test_missing_validated_file_blocks_training(local_job, monkeypatch, filename):
    (local_job.checked / filename).unlink()
    assert_blocked_before_fit(local_job, monkeypatch, FileNotFoundError)


@pytest.mark.parametrize("payload", ["{", "[]", "null", '"pass"', "true"])
def test_malformed_report_blocks_training(local_job, monkeypatch, payload):
    local_job.report.write_text(payload)
    assert_blocked_before_fit(local_job, monkeypatch, ValueError)


@pytest.mark.parametrize("changes, message", [
    ({"status": "fail"}, "passing validation report"),
    ({"stage": "ingest"}, "passing validation report"),
    ({"sha256": "0" * 64}, "SHA-256"),
    ({"rows": 81}, "size/row count"),
    ({"rows": "80"}, "size/row count"),
    ({"rows": True}, "size/row count"),
    ({"bytes": 0}, "size/row count"),
])
def test_incorrect_report_blocks_training(local_job, monkeypatch, changes, message):
    report = json.loads(local_job.report.read_text())
    report.update(changes)
    local_job.report.write_text(json.dumps(report))
    assert_blocked_before_fit(local_job, monkeypatch, ValueError, message)


def test_changed_csv_after_validation_blocks_training(local_job, monkeypatch):
    with (local_job.checked / "sensors.csv").open("ab") as stream:
        stream.write(b"\n")
    assert_blocked_before_fit(local_job, monkeypatch, ValueError, "SHA-256")


def test_pass_label_and_matching_hash_do_not_override_the_contract(local_job, monkeypatch):
    csv = local_job.checked / "sensors.csv"
    local_job.frame.assign(load_pct=250.0).to_csv(csv, index=False)
    content = csv.read_bytes()
    report = json.loads(local_job.report.read_text())
    report.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
    local_job.report.write_text(json.dumps(report))
    assert_blocked_before_fit(local_job, monkeypatch, ValueError, "load_pct outside")


def test_single_class_partition_blocks_training(local_job, monkeypatch):
    train_df, _, _ = data.split(local_job.frame, seed=20260101)
    frame = local_job.frame.copy()
    frame[data.TARGET] = (~frame[data.GROUP].isin(train_df[data.GROUP])).astype("int64")
    frame.to_csv(local_job.source, index=False)
    fresh = local_job.checked.parent / "one-class-checked"
    validate(local_job.source, fresh)
    local_job.checked = fresh
    assert_blocked_before_fit(local_job, monkeypatch, ValueError, "train split")


def test_no_reuse_of_old_output(local_job, monkeypatch):
    local_job.output.mkdir()
    marker = local_job.output / "keep.txt"
    marker.write_text("earlier result")
    assert_blocked_before_fit(local_job, monkeypatch, FileExistsError, "must be empty")
    assert marker.read_text() == "earlier result"


def test_input_directory_cannot_be_output(local_job, monkeypatch):
    original = (local_job.checked / "sensors.csv").read_bytes()
    local_job.output = local_job.checked
    assert_blocked_before_fit(local_job, monkeypatch, FileExistsError)
    assert (local_job.checked / "sensors.csv").read_bytes() == original


@pytest.mark.parametrize("content", [
    "", "[]", "outs: [", "outs: []", "outs: [null]",
    "outs:\n- path: raw\n  hash: md5\n  md5: bad\n",
    "outs:\n- path: other\n  hash: md5\n  md5: " + "a" * 32 + ".dir\n",
])
def test_invalid_dvc_metadata_blocks_training(local_job, monkeypatch, content):
    local_job.dvc.write_text(content)
    assert_blocked_before_fit(local_job, monkeypatch, RuntimeError)


def test_missing_dvc_metadata_blocks_training(local_job, monkeypatch):
    local_job.dvc.unlink()
    assert_blocked_before_fit(local_job, monkeypatch, RuntimeError, "Cannot read DVC")


@pytest.mark.parametrize("hash_seed", [None, "42"])
def test_missing_or_mismatched_launcher_seed_blocks_training(local_job, monkeypatch, hash_seed):
    monkeypatch.setattr(seeds, "_INITIAL_HASH_SEED", hash_seed)
    assert_blocked_before_fit(local_job, monkeypatch, RuntimeError, "PYTHONHASHSEED")


def test_seed_control_is_repeatable():
    import random
    seeds.set_all(42)
    first = random.random(), np.random.random()
    seeds.set_all(42)
    assert (random.random(), np.random.random()) == first


@pytest.mark.parametrize("seed", [-1, 2**32])
def test_invalid_seed_fails(seed):
    with pytest.raises(ValueError):
        seeds.set_all(seed)


@pytest.mark.parametrize("dirty, suffix", [("", ""), ("?? src/train.py\n", "-dirty")])
def test_git_revision_records_dirty_state(monkeypatch, dirty, suffix):
    run = Mock(side_effect=[
        SimpleNamespace(stdout="b" * 40 + "\n"), SimpleNamespace(stdout=dirty),
    ])
    monkeypatch.setattr(train.subprocess, "run", run)
    assert train.git_commit() == "b" * 40 + suffix
    assert "--untracked-files=normal" in run.call_args.args[0]


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.CalledProcessError(1, "git")])
def test_missing_git_is_not_reported_as_known_revision(monkeypatch, error):
    monkeypatch.setattr(train.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(RuntimeError, match="Git revision unavailable"):
        train.git_commit()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.5])
def test_invalid_metrics_cannot_publish_success(local_job, monkeypatch, value):
    monkeypatch.setattr(train, "roc_auc_score", lambda *_: value)
    save = Mock(side_effect=AssertionError("Must not export invalid metrics"))
    monkeypatch.setattr(train.mlflow.sklearn, "save_model", save)
    with pytest.raises(ValueError, match="invalid metrics"):
        run_small(local_job)
    save.assert_not_called()
    assert not (local_job.output / "metrics.json").exists()


def test_cli_requires_explicit_inputs():
    with pytest.raises(SystemExit) as exc:
        train.main([])
    assert exc.value.code == 2


def test_cli_defaults_are_the_selected_lab2_parameters(local_job, monkeypatch):
    run = Mock(return_value={})
    monkeypatch.setattr(train, "run_training", run)
    assert train.main([
        "--validated-dir", str(local_job.checked),
        "--dvc-metadata-path", str(local_job.dvc),
        "--output-dir", str(local_job.output),
    ]) == 0
    assert run.call_args.kwargs == {
        "validated_dir": local_job.checked, "dvc_metadata_path": local_job.dvc,
        "output_dir": local_job.output, "seed": 20260101, "n_estimators": 100,
        "max_depth": 12, "min_samples_leaf": 10, "source_commit": None,
    }


def test_cli_rejects_mismatched_fingerprint(local_job, capsys):
    report = json.loads(local_job.report.read_text())
    report["sha256"] = "0" * 64
    local_job.report.write_text(json.dumps(report))
    assert train.main([
        "--validated-dir", str(local_job.checked),
        "--dvc-metadata-path", str(local_job.dvc),
        "--output-dir", str(local_job.output),
    ]) == 1
    captured = capsys.readouterr()
    assert "TRAIN FAIL" in captured.err
    assert "SHA-256" in captured.err
    assert not captured.out
    assert not local_job.output.exists()


def test_model_export_failure_is_not_success(local_job, monkeypatch):
    monkeypatch.setattr(
        train.mlflow.sklearn, "save_model", Mock(side_effect=OSError("disk write failed")),
    )
    with pytest.raises(OSError, match="disk write"):
        run_small(local_job)
    assert not (local_job.output / "metrics.json").exists()


@pytest.mark.parametrize("supplied", ["b" * 40, "b" * 40 + "-dirty"])
def test_supplied_revision_works_without_git(monkeypatch, supplied):
    monkeypatch.setattr(train.subprocess, "run", Mock(side_effect=FileNotFoundError()))
    assert train.git_commit(supplied) == supplied


@pytest.mark.parametrize("supplied", ["", "unknown", "abc1234", "B" * 40, "b" * 39, "b" * 40 + "-clean"])
def test_invalid_supplied_revision_is_rejected_before_git(monkeypatch, supplied):
    run = Mock(side_effect=AssertionError("Invalid lineage must fail first"))
    monkeypatch.setattr(train.subprocess, "run", run)
    with pytest.raises(ValueError, match="full lowercase SHA"):
        train.git_commit(supplied)
    run.assert_not_called()


@pytest.mark.parametrize("dirty, supplied, expected", [
    ("", "b" * 40, "b" * 40),
    (" M train.py", "b" * 40 + "-dirty", "b" * 40 + "-dirty"),
    (" M train.py", "b" * 40, None),
    ("", "c" * 40, None),
])
def test_supplied_revision_cannot_hide_local_sha_or_dirty_state(monkeypatch, dirty, supplied, expected):
    monkeypatch.setattr(train.subprocess, "run", Mock(side_effect=[
        SimpleNamespace(stdout="b" * 40), SimpleNamespace(stdout=dirty),
    ]))
    if expected is None:
        with pytest.raises(ValueError, match="does not match"):
            train.git_commit(supplied)
    else:
        assert train.git_commit(supplied) == expected


def test_supplied_revision_does_not_hide_failed_dirty_check(monkeypatch):
    monkeypatch.setattr(train.subprocess, "run", Mock(side_effect=[
        SimpleNamespace(stdout="b" * 40), subprocess.CalledProcessError(1, "git status"),
    ]))
    with pytest.raises(RuntimeError, match="dirty state"):
        train.git_commit("b" * 40)


def test_cli_records_supplied_revision_in_metrics_and_model(local_job):
    supplied = "b" * 40
    assert train.main([
        "--validated-dir", str(local_job.checked), "--dvc-metadata-path", str(local_job.dvc),
        "--output-dir", str(local_job.output), "--git-commit", supplied,
        "--n-estimators", "2", "--max-depth", "2", "--min-samples-leaf", "1",
    ]) == 0
    result = json.loads((local_job.output / "metrics.json").read_text())
    metadata = yaml.safe_load((local_job.output / "model" / "MLmodel").read_text())["metadata"]
    for record in (result, metadata):
        assert record["git_commit"] == supplied
        assert record["git_commit_source"] == "supplied"
