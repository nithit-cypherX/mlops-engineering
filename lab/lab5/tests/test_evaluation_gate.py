"""Offline evaluation gate checks; no training, registration, or cloud access."""
from __future__ import annotations

import hashlib
import json
import math
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import evaluation_gate as gate

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluation_gate.py"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(
            socket.socket, method, Mock(side_effect=AssertionError("No network")),
        )


@pytest.fixture
def metrics_file(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({"val_roc_auc": 0.8444994217173845, "test_roc_auc": 0.0}))
    return path


@pytest.mark.parametrize("score, expected", [
    (0.8444994217173845, 0),
    (0.80, 0),
    (math.nextafter(0.80, 0), 1),
    (0.79, 1),
    (0, 1),
    (1, 0),
])
def test_default_floor_and_boundary(metrics_file, capsys, score, expected):
    metrics_file.write_text(json.dumps({"val_roc_auc": score}))
    assert gate.main(["--metrics", str(metrics_file)]) == expected
    captured = capsys.readouterr()
    if expected == 0:
        assert "GATE PASS" in captured.out
        assert "absolute threshold only" in captured.out
        assert not captured.err
    else:
        assert "GATE FAIL" in captured.err
        assert not captured.out


@pytest.mark.parametrize("value", [
    None, True, False, "0.95", [], {}, -0.01, 1.01,
    float("nan"), float("inf"), -float("inf"), 10**400,
])
def test_invalid_metric_fails_closed(metrics_file, capsys, value):
    metrics_file.write_text(json.dumps({"val_roc_auc": value, "test_roc_auc": 1.0}))
    assert gate.main(["--metrics", str(metrics_file)]) == 1
    captured = capsys.readouterr()
    assert "GATE FAIL" in captured.err
    assert "finite number between 0 and 1" in captured.err
    assert not captured.out


@pytest.mark.parametrize("payload", [
    "{", "null", "[]", "true", "{}", '{"test_roc_auc": 1.0}',
])
def test_malformed_or_missing_metric_does_not_fall_back(metrics_file, capsys, payload):
    metrics_file.write_text(payload)
    assert gate.main(["--metrics", str(metrics_file)]) == 1
    captured = capsys.readouterr()
    assert "GATE FAIL" in captured.err
    assert not captured.out


def test_missing_file_fails_closed(tmp_path, capsys):
    assert gate.main(["--metrics", str(tmp_path / "missing.json")]) == 1
    assert "GATE FAIL" in capsys.readouterr().err


def test_invalid_utf8_fails_closed(metrics_file, capsys):
    metrics_file.write_bytes(b"\xff")
    assert gate.main(["--metrics", str(metrics_file)]) == 1
    assert "GATE FAIL" in capsys.readouterr().err


@pytest.mark.parametrize("minimum", ["nan", "inf", "-inf", "-0.1", "1.1"])
def test_invalid_floor_fails_closed(metrics_file, capsys, minimum):
    assert gate.main(["--metrics", str(metrics_file), f"--min-auc={minimum}"]) == 1
    captured = capsys.readouterr()
    assert "--min-auc must be a finite number" in captured.err
    assert not captured.out


@pytest.mark.parametrize("val_score, test_score, expected", [
    (0.85, 0.0, 0), (0.75, 1.0, 1),
])
def test_test_score_cannot_override_validation(metrics_file, val_score, test_score, expected):
    metrics_file.write_text(json.dumps({
        "val_roc_auc": val_score, "test_roc_auc": test_score,
    }))
    assert gate.main(["--metrics", str(metrics_file)]) == expected


@pytest.mark.parametrize("extra, expected, message", [
    ([], 0, "GATE PASS"),
    (["--min-auc", "0.90"], 1, "GATE FAIL"),
    (["--min-auc", "nan"], 1, "GATE FAIL"),
    (["--min-auc", "invalid"], 2, "invalid float value"),
])
def test_cli_returns_real_exit_status_without_changing_inputs(metrics_file, extra, expected, message):
    before = metrics_file.read_bytes()
    result = subprocess.run(
        [sys.executable, "-B", "-S", str(SCRIPT), "--metrics", str(metrics_file), *extra],
        cwd=metrics_file.parent, text=True, capture_output=True, check=False,
    )
    assert result.returncode == expected
    assert message in result.stdout + result.stderr
    assert metrics_file.read_bytes() == before
    assert list(metrics_file.parent.iterdir()) == [metrics_file]


def test_cli_requires_metrics_path():
    result = subprocess.run(
        [sys.executable, "-B", "-S", str(SCRIPT)], text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert "--metrics" in result.stderr


def test_gate_writes_decision_bound_to_exact_input(metrics_file, tmp_path):
    output = tmp_path / "gate"
    original = metrics_file.read_bytes()
    assert gate.main(["--metrics", str(metrics_file), "--output-dir", str(output)]) == 0
    report = json.loads((output / "gate.json").read_text())
    assert report == {
        "stage": "evaluate", "status": "pass", "metric": "val_roc_auc",
        "minimum": 0.80, "score": 0.8444994217173845,
        "training_record_sha256": hashlib.sha256(original).hexdigest(),
    }
    assert gate.read_pass_report(output / "gate.json", original) == report
    assert metrics_file.read_bytes() == original


@pytest.mark.parametrize("payload", ['{"val_roc_auc": 0.79}', "{}", "{"])
def test_failed_gate_never_writes_pass_output(metrics_file, tmp_path, payload):
    metrics_file.write_text(payload)
    output = tmp_path / "gate"
    assert gate.main(["--metrics", str(metrics_file), "--output-dir", str(output)]) == 1
    assert not (output / "gate.json").exists()


def test_gate_rejects_old_output_without_overwriting(metrics_file, tmp_path):
    output = tmp_path / "gate"
    args = ["--metrics", str(metrics_file), "--output-dir", str(output)]
    assert gate.main(args) == 0
    original = (output / "gate.json").read_bytes()
    assert gate.main(args) == 1
    assert (output / "gate.json").read_bytes() == original


@pytest.mark.parametrize("key, value", [
    ("stage", "train"), ("status", "fail"), ("metric", "test_roc_auc"),
    ("minimum", 0.7), ("minimum", 0.90), ("minimum", True),
    ("score", 0.99), ("score", True), ("training_record_sha256", "0" * 64),
])
def test_changed_or_weaker_gate_report_is_rejected(metrics_file, tmp_path, key, value):
    content = metrics_file.read_bytes()
    report = gate.decision(content)
    report[key] = value
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        gate.read_pass_report(path, content)


def test_gate_from_another_training_result_is_rejected(metrics_file, tmp_path):
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(gate.decision(metrics_file.read_bytes())))
    with pytest.raises(ValueError, match="does not match"):
        gate.read_pass_report(path, b'{"val_roc_auc": 0.8444994217173845, "test_roc_auc": 0.5}')


@pytest.mark.parametrize("payload", ["{", "null", "[]", "{}"])
def test_missing_or_malformed_gate_report_is_rejected(tmp_path, payload):
    path = tmp_path / "gate.json"
    with pytest.raises(FileNotFoundError):
        gate.read_pass_report(path, b'{"val_roc_auc": 0.85}')
    path.write_text(payload)
    with pytest.raises(ValueError):
        gate.read_pass_report(path, b'{"val_roc_auc": 0.85}')


def test_stricter_gate_pass_keeps_its_actual_floor(tmp_path):
    content = b'{"val_roc_auc": 0.95}'
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(gate.decision(content, 0.90)))
    assert gate.read_pass_report(path, content)["minimum"] == 0.90
