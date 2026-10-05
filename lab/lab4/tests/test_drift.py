"""Offline drift checks: no registry, inference, metric emission or cloud calls."""
import json
import math
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from monitoring import drift
from scripts import calibrate_drift

RAW = Path(__file__).resolve().parents[1] / "data/raw/sensors.csv"
START = drift.utc_time("2026-10-02T06:00:00Z")
END = START + timedelta(minutes=15)


def request(values=None, **changes):
    record = {
        "timestamp": "2026-10-02T06:05:00Z",
        "path": "/predict", "model_version": "1", "status": 200,
        "load_pct_values": [68] if values is None else values,
    }
    record.update(changes)
    return record


@pytest.fixture(scope="module")
def reference():
    return drift.load_reference(RAW)


def test_reference_reconstructs_actual_training_partition(reference):
    assert len(reference) == 3600
    assert reference.machine_id.nunique() == 144
    assert drift.SPLIT_SEED == 20260101
    assert drift.DATA_SHA256 == "422cccb9136e814061b83e4f041cff21ba7372c6f7fb0feb2c321555c6a6341f"


def test_wrong_reference_bytes_are_rejected(tmp_path):
    path = tmp_path / "other.csv"
    path.write_text("load_pct\n68\n")
    with pytest.raises(ValueError, match="pinned"):
        drift.load_reference(path)


def test_changed_split_is_rejected(monkeypatch, reference):
    monkeypatch.setattr(drift.data, "split", lambda *a, **k: (reference.iloc[:-1], None, None))
    with pytest.raises(ValueError, match="lineage"):
        drift.load_reference(RAW)


def test_psi_matches_hand_calculation_including_smoothing():
    # Reference counts [2,2] -> [1/2,1/2]; current [4,0] -> [5/6,1/6].
    score = drift.psi(np.array([0, 25, 75, 100]), np.zeros(4), bins=2)
    assert score == pytest.approx(math.log(5) / 3)


def test_identical_distributions_have_zero_score(reference):
    values = reference.load_pct.to_numpy()
    assert drift.psi(values, values) == pytest.approx(0)


@pytest.mark.parametrize("bad", [[], [np.nan], [np.inf], [-1], [101], ["68"], [True], [True, 1], [[68]]])
def test_invalid_psi_inputs_are_rejected(bad):
    good = np.linspace(0, 100, 100)
    with pytest.raises(ValueError):
        drift.psi(good, bad)
    with pytest.raises(ValueError):
        drift.psi(bad, good)


@pytest.mark.parametrize("reference", [[50] * 100, [0] * 99 + [100]])
def test_degenerate_reference_is_not_reported_as_stable(reference):
    with pytest.raises(ValueError):
        drift.psi(reference, np.arange(100))


@pytest.mark.parametrize("bins", [0, 1, True, 2.5])
def test_invalid_bin_counts_are_rejected(bins):
    with pytest.raises(ValueError):
        drift.psi(np.arange(100), np.arange(100), bins=bins)


def test_single_batch_and_validated_scoring_failure_each_count_all_rows():
    records = [
        request([68], request_id="same-id"),
        request([68, 90], path="/predict/batch", request_id="same-id"),
        request([42], status=500),
    ]
    actual = drift.current_values(records, start=START, end=END)
    assert actual.tolist() == [68, 68, 90, 42]  # Never deduplicate by a client-supplied request ID.


def test_window_is_start_inclusive_end_exclusive_and_accepts_timezones():
    records = [
        request([1], timestamp=START.isoformat()),
        request([2], timestamp=END.isoformat()),
        request([3], timestamp=(START - timedelta(seconds=1)).isoformat()),
        request([4], timestamp="2026-10-02T13:05:00+07:00"),
    ]
    assert drift.current_values(records, start=START, end=END).tolist() == [1, 4]


def test_probes_other_versions_and_rejected_inputs_are_not_samples():
    records = [
        request(path="/ready"),
        request(model_version="2"),
        {k: v for k, v in request(status=422).items() if k != "load_pct_values"},
    ]
    assert drift.current_values(records, start=START, end=END).size == 0


@pytest.mark.parametrize("changes", [
    {"timestamp": "2026-10-02T06:05:00"}, {"timestamp": "bad"},
    {"load_pct_values": None}, {"load_pct_values": []}, {"load_pct_values": "68"},
    {"load_pct_values": [68, 90]}, {"load_pct_values": [None]},
    {"load_pct_values": [False, 1], "path": "/predict/batch"},
    {"load_pct_values": [np.nan]}, {"load_pct_values": [101]},
    {"load_pct_values": [68] * 101, "path": "/predict/batch"},
    {"status": "200"}, {"status": True}, {"model_version": None},
    {"model_version": "unknown"}, {"path": None},
])
def test_malformed_in_window_prediction_fails_not_partial_success(changes):
    with pytest.raises(ValueError):
        drift.current_values([request(**changes)], start=START, end=END)


@pytest.mark.parametrize("records", [{}, [None]])
def test_bad_log_container_fails(records):
    with pytest.raises(ValueError):
        drift.current_values(records, start=START, end=END)


@pytest.mark.parametrize("start,end", [
    (START, START), (END, START), (START.replace(tzinfo=None), END),
])
def test_invalid_window_is_rejected(start, end):
    with pytest.raises(ValueError):
        drift.current_values([], start=start, end=end)


@pytest.mark.parametrize("count", [0, 6, 100, 499])
def test_too_few_samples_do_not_emit_zero_or_claim_no_drift(reference, count):
    result = drift.evaluate(reference.load_pct.to_numpy(), [68] * count)
    assert result["status"] == "insufficient_data"
    assert result["psi"] is None
    assert result["current_count"] == count


def test_invalid_reference_is_not_hidden_by_no_current_data():
    with pytest.raises(ValueError):
        drift.evaluate([50] * 100, [])


def test_exact_threshold_breaches_without_rounding(monkeypatch):
    monkeypatch.setattr(drift, "psi", lambda *args: drift.THRESHOLD)
    assert drift.evaluate(np.arange(100), [68] * 500)["status"] == "drift_detected"
    monkeypatch.setattr(drift, "psi", lambda *args: drift.THRESHOLD - 1e-8)
    assert drift.evaluate(np.arange(100), [68] * 500)["status"] == "below_threshold"


@pytest.mark.parametrize("kind,exit_code,status", [
    ("empty", 3, "insufficient_data"),
    ("baseline", 0, "below_threshold"),
    ("shifted", 2, "drift_detected"),
])
def test_cli_states_and_pinned_reference_provenance(tmp_path, capsys, reference, kind, exit_code, status):
    values = [] if kind == "empty" else (
        reference.load_pct.tolist() if kind == "baseline" else [100] * 500
    )
    records = [request(values[i:i + 100], path="/predict/batch") for i in range(0, len(values), 100)]
    path = tmp_path / "current.json"
    path.write_text(json.dumps(records))
    code = drift.main(["--reference", str(RAW), "--current-logs", str(path),
                       "--as-of", "2026-10-02T06:17:00Z"])
    result = json.loads(capsys.readouterr().out)
    assert code == exit_code
    assert result["status"] == status
    assert result["reference_sha256"] == drift.DATA_SHA256
    assert result["split_seed"] == 20260101
    assert result["window_start"] == START.isoformat() and result["window_end"] == END.isoformat()


def test_cli_invalid_input_does_not_echo_raw_payload(tmp_path, capsys):
    path = tmp_path / "current.json"
    path.write_text("sensitive-payload-not-json")
    assert drift.main(["--reference", str(RAW), "--current-logs", str(path)]) == 1
    output = capsys.readouterr().out
    assert "sensitive-payload" not in output
    assert json.loads(output)["status"] == "invalid_input"


def test_fixed_seed_calibration_reproduces_selected_policy():
    result = calibrate_drift.calibrate()
    assert result["selected"] == {"min_samples": 500, "threshold": 0.6}
    assert result["policy_matches_detector"] and result["confirmation_passed"]
    assert result["confirmation"]["clean"]["breaches"] == 2
    assert result["confirmation"]["minus20"]["breaches"] == 500
    assert result["confirmation"]["plus20"]["breaches"] == 500
