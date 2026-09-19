"""Benchmark guards and timing method; fake models only, no training or Azure."""
from unittest.mock import Mock
import socket

import numpy as np
import pytest
import yaml

from scripts import benchmark_models as bench


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))


def test_file_sizes_are_actual_bytes_and_hashes(tmp_path):
    (tmp_path / "model.pkl").write_bytes(b"12345")
    (tmp_path / "MLmodel").write_bytes(b"metadata")
    result = bench.file_inventory(tmp_path)
    assert result["model.pkl"]["bytes"] == 5
    assert sum(row["bytes"] for row in result.values()) == 13
    assert result["model.pkl"]["sha256"] == bench.hashlib.sha256(b"12345").hexdigest()


@pytest.mark.parametrize("fault", ["run", "version", "size"])
def test_wrong_artifacts_fail_before_deserialization(tmp_path, monkeypatch, fault):
    (tmp_path / "model.pkl").write_bytes(b"fake")
    metadata = {
        "run_id": bench.CASES["100"]["run_id"], "model_size_bytes": 4,
        "flavors": {"sklearn": {"sklearn_version": bench.sklearn.__version__}},
    }
    if fault == "run":
        metadata["run_id"] = "wrong"
    elif fault == "version":
        metadata["flavors"]["sklearn"]["sklearn_version"] = "wrong"
    else:
        metadata["model_size_bytes"] = 8
    (tmp_path / "MLmodel").write_text(yaml.safe_dump(metadata))
    loader = Mock()
    monkeypatch.setattr(bench.mlflow.sklearn, "load_model", loader)
    with pytest.raises(ValueError):
        bench.load_verified_model(tmp_path, "100")
    loader.assert_not_called()


def test_saved_model_validation_must_match_reference():
    model = Mock()
    model.predict_proba.return_value = np.array([[0.9, 0.1], [0.1, 0.9]])
    _, scores = bench.verify_predictions(
        model, "features", [0, 1], {"val_roc_auc": 1, "val_pr_auc": 1})
    assert scores == {"val_roc_auc": 1, "val_pr_auc": 1}
    with pytest.raises(ValueError, match="reproduce"):
        bench.verify_predictions(model, "features", [0, 1], {"val_roc_auc": 0.5})
    model.fit.assert_not_called()


def test_warmups_excluded_and_order_alternates(monkeypatch):
    seen = []
    features = object()
    expected = np.array([[0.4, 0.6]])
    def predictor(label):
        def predict(frame):
            assert frame is features
            seen.append(label)
            return expected.copy()
        return predict
    models = {k: Mock(predict_proba=Mock(side_effect=predictor(k))) for k in ["100", "300"]}
    # Four measured calls, with elapsed times of 1, 2, 3, 4 milliseconds.
    ticks = iter([0, 1_000_000, 0, 2_000_000, 0, 3_000_000, 0, 4_000_000])
    clock = Mock(side_effect=lambda: next(ticks))
    monkeypatch.setattr(bench.time, "perf_counter_ns", clock)
    result = bench.measure(models, features, {k: expected for k in models}, warmups=1, repeats=2)
    assert seen == ["100", "300", "100", "300", "300", "100"]
    assert result == {"100": [1.0, 4.0], "300": [2.0, 3.0]}
    assert clock.call_count == 8
    for model in models.values():
        model.fit.assert_not_called()


def test_changed_predictions_fail_instead_of_reporting_fast_wrong_model(monkeypatch):
    model = Mock()
    model.predict_proba.return_value = np.array([[0.5, 0.5]])
    with pytest.raises(AssertionError):
        bench.measure({"100": model}, "features",
                      {"100": np.array([[0.4, 0.6]])}, warmups=1, repeats=2)


@pytest.mark.parametrize("warmups,repeats", [(0, 2), (1, 1)])
def test_insufficient_measurements_rejected(warmups, repeats):
    with pytest.raises(ValueError):
        bench.measure({}, None, {}, warmups=warmups, repeats=repeats)


def test_percentile_definition_and_units():
    result = bench.latency_summary([1.0, 2.0, 3.0, 4.0])
    assert result == {"n": 4, "median_ms": 2.5,
                      "p95_ms": pytest.approx(3.85, rel=0, abs=1e-12),
                      "min_ms": 1, "max_ms": 4}
