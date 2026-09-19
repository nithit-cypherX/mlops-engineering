"""Seed-check wiring and failure checks; no actual model training or Azure calls."""
import json
import socket
import sys
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from scripts import check_seed_variance as check


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))


@pytest.mark.parametrize("values", [[], [42], [20260101, 20260101], [20260101, -1],
                                    [20260101, 2**32], [20260101, 1.5], [20260101, True]])
def test_invalid_seed_lists_are_rejected(values):
    with pytest.raises(ValueError):
        check.validate_model_seeds(values)


def test_baseline_uses_full_precision_and_stops_on_mismatch():
    assert check.check_baseline(check.REFERENCE_METRICS) == {
        "val_roc_auc": 0, "val_pr_auc": 0,
    }
    rounded = {name: round(value, 4) for name, value in check.REFERENCE_METRICS.items()}
    with pytest.raises(ValueError, match="Baseline metrics differ"):
        check.check_baseline(rounded)


def test_one_seed_does_not_claim_zero_variance():
    summary = check.summarize([check.REFERENCE_METRICS])
    assert all(row["sample_variance"] is None and row["sample_stddev"] is None
               and row["n"] == 1 for row in summary.values())


def test_summary_uses_sample_variance():
    rows = [{"val_roc_auc": v, "val_pr_auc": v} for v in [0.7, 0.8, 0.9]]
    for row in check.summarize(rows).values():
        assert row["mean"] == pytest.approx(0.8)
        assert row["sample_variance"] == pytest.approx(0.01)
        assert row["sample_stddev"] == pytest.approx(0.1)


@pytest.mark.parametrize("failure", ["code", "data", "dvc"])
def test_changed_inputs_are_rejected_before_training(tmp_path, monkeypatch, failure):
    raw = tmp_path / "sensors.csv"
    raw.write_bytes(b"fixture")
    monkeypatch.setattr(check, "RAW_SHA256", check.hashlib.sha256(b"fixture").hexdigest())
    monkeypatch.setattr(check.train, "git_commit",
                        lambda: "different" if failure == "code" else check.REFERENCE_COMMIT)
    monkeypatch.setattr(check.train, "dvc_hash",
                        lambda path: "different" if failure == "dvc" else check.DVC_HASH)
    if failure == "data":
        raw.write_bytes(b"changed")
    with pytest.raises(ValueError):
        check.check_inputs(raw, tmp_path / "raw.dvc")


@pytest.fixture
def mocked_inputs(monkeypatch, tmp_path):
    parts = []
    for i, count in enumerate((3600, 1200, 1200)):
        parts.append(pd.DataFrame({
            check.data.ID: np.arange(count) + i * 10000,
            check.data.GROUP: [i] * count,
        }))
    split = Mock(return_value=tuple(parts))
    fit = Mock(side_effect=lambda train, val, seed:
               {"model_seed": seed, **check.REFERENCE_METRICS})
    monkeypatch.setattr(check, "check_inputs", Mock())
    monkeypatch.setattr(check.seeds, "_INITIAL_HASH_SEED", str(check.SPLIT_SEED))
    monkeypatch.setattr(check.data, "load_raw", Mock(return_value="fixture"))
    monkeypatch.setattr(check.data, "split", split)
    monkeypatch.setattr(check, "fit_and_score", fit)
    return parts, split, fit, tmp_path / "sensors.csv", tmp_path / "raw.dvc"


def test_split_is_fixed_and_test_rows_are_never_scored(mocked_inputs):
    parts, split, fit, raw, metadata = mocked_inputs
    result = check.run_check(raw, metadata, [20260101, 20260102])
    split.assert_called_once_with("fixture", seed=20260101)
    assert [call.args[2] for call in fit.call_args_list] == [20260101, 20260102]
    assert all(call.args[0] is parts[0] and call.args[1] is parts[1]
               for call in fit.call_args_list)
    assert result["global_seed"] == result["split_seed"] == result["python_hash_seed"] == 20260101


@pytest.mark.parametrize("score", [0.5, float("nan"), float("inf"), 1.1])
def test_bad_baseline_stops_before_other_seeds(mocked_inputs, score):
    _, _, fit, raw, metadata = mocked_inputs
    fit.side_effect = None
    fit.return_value = {"model_seed": 20260101, **check.REFERENCE_METRICS, "val_roc_auc": score}
    with pytest.raises(ValueError):
        check.run_check(raw, metadata, [20260101, 20260102])
    assert fit.call_count == 1


def test_group_overlap_fails_before_training(mocked_inputs):
    parts, _, fit, raw, metadata = mocked_inputs
    parts[1][check.data.GROUP] = 0
    with pytest.raises(ValueError, match="more than one"):
        check.run_check(raw, metadata, [20260101])
    fit.assert_not_called()


def test_rf_receives_only_training_rows_and_requested_model_seed(monkeypatch):
    frame = pd.DataFrame({name: [0.0, 1.0] for name in check.data.FEATURES})
    frame[check.data.TARGET] = [0, 1]
    validation = frame.copy()
    validation[check.data.FEATURES[0]] += 5
    model = Mock()
    model.predict_proba.return_value = np.array([[0.9, 0.1], [0.1, 0.9]])
    factory = Mock(return_value=model)
    monkeypatch.setattr(check, "RandomForestClassifier", factory)
    result = check.fit_and_score(frame, validation, 20260102)
    factory.assert_called_once_with(random_state=20260102, **check.MODEL_PARAMS)
    pd.testing.assert_frame_equal(model.fit.call_args.args[0], frame[check.data.FEATURES])
    pd.testing.assert_series_equal(model.fit.call_args.args[1], frame[check.data.TARGET])
    pd.testing.assert_frame_equal(model.predict_proba.call_args.args[0],
                                  validation[check.data.FEATURES])
    assert result == {"model_seed": 20260102, "val_roc_auc": 1.0, "val_pr_auc": 1.0}


def test_cli_defaults_to_baseline_only_and_records_provenance(mocked_inputs, monkeypatch, tmp_path):
    _, _, fit, raw, metadata = mocked_inputs
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["check", "--raw-path", str(raw),
                       "--dvc-metadata-path", str(metadata), "--out", str(out)])
    assert check.main() == 0
    assert fit.call_count == 1
    result = json.loads(out.read_text())
    assert result["status"] == "baseline_only"
    assert result["reference"]["mlflow_run_id"] == check.REFERENCE_RUN
    assert result["baseline_check"]["passed"] is True
    assert len(result["execution"]["checker_sha256"]) == 64
    assert all(row["sample_variance"] is None for row in result["summary"].values())


def test_failure_does_not_overwrite_existing_report(mocked_inputs, monkeypatch, tmp_path):
    _, _, fit, raw, metadata = mocked_inputs
    fit.side_effect = ValueError("fixture failure")
    out = tmp_path / "report.json"
    out.write_text("existing report")
    monkeypatch.setattr(sys, "argv", ["check", "--raw-path", str(raw),
                       "--dvc-metadata-path", str(metadata), "--out", str(out)])
    with pytest.raises(ValueError, match="fixture failure"):
        check.main()
    assert out.read_text() == "existing report"

