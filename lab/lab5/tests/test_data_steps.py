"""Offline data-contract and CLI checks. Only temporary copies are modified."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.data import PLAUSIBLE_RANGES, validate_contract
from src.data_steps import ingest, validate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def frame():
    # Explicit fixture independent of the production schema/range constants.
    return pd.DataFrame({
        "reading_id": [1, 2, 3, 4],
        "machine_id": [10, 10, 20, 20],
        "temp_c": [20.0, 25.0, 30.0, 35.0],
        "vibration_mm_s": [1.0, 2.0, 3.0, 4.0],
        "pressure_kpa": [100.0, 110.0, 120.0, 130.0],
        "hours_since_service": [10.0, 20.0, 30.0, 40.0],
        "load_pct": [30.0, 40.0, 50.0, 60.0],
        "ambient_humidity": [40.0, 50.0, 60.0, 70.0],
        "failed_within_7d": [0, 1, 0, 1],
    })


@pytest.fixture
def csv_path(frame, tmp_path):
    path = tmp_path / "selected.csv"
    frame.to_csv(path, index=False)
    return path


def test_contract_accepts_valid_data_without_mutation(frame):
    before = frame.copy(deep=True)
    validate_contract(frame)
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize("corrupt, message", [
    pytest.param(lambda f: f.drop(columns=["temp_c"]), "missing columns", id="missing"),
    pytest.param(lambda f: f.assign(extra=0), "unexpected columns", id="extra"),
    pytest.param(lambda f: f.assign(temp_c="hot"), "expected float64", id="wrong-type"),
    pytest.param(lambda f: f.assign(temp_c=20), "expected float64", id="integer-feature"),
    pytest.param(lambda f: f.assign(temp_c=float("nan")), "null values", id="null-feature"),
    pytest.param(lambda f: f.assign(reading_id=float("nan")), "null values", id="null-id"),
    pytest.param(lambda f: f.assign(temp_c=float("inf")), "plausible range", id="infinity"),
    pytest.param(lambda f: f.assign(temp_c=float("-inf")), "plausible range", id="neg-infinity"),
    pytest.param(lambda f: f.assign(failed_within_7d=2), "outside 0/1", id="bad-label"),
    pytest.param(lambda f: f.assign(failed_within_7d=0), "degenerate", id="all-zero"),
    pytest.param(lambda f: f.assign(failed_within_7d=1), "degenerate", id="all-one"),
    pytest.param(lambda f: f.assign(reading_id=1), "not unique", id="duplicate-id"),
    pytest.param(lambda f: f.iloc[:0], "no rows", id="empty"),
    pytest.param(
        lambda f: f.rename(columns={"machine_id": "reading_id"}),
        "duplicate column", id="duplicate-column",
    ),
])
def test_contract_rejects_invalid_data(frame, corrupt, message):
    with pytest.raises(ValueError, match=message):
        validate_contract(corrupt(frame.copy(deep=True)))


@pytest.mark.parametrize("column", list(PLAUSIBLE_RANGES))
@pytest.mark.parametrize("boundary", ("lower", "upper"))
def test_range_boundary_is_inclusive_but_outside_fails(frame, column, boundary):
    low, high = PLAUSIBLE_RANGES[column]
    value = low if boundary == "lower" else high
    frame[column] = float(value)
    validate_contract(frame)
    frame[column] = value + (-0.01 if boundary == "lower" else 0.01)
    with pytest.raises(ValueError, match=f"{column} outside plausible range"):
        validate_contract(frame)


@pytest.mark.parametrize("positives", [1, 99])
def test_degenerate_rate_boundaries_fail(frame, positives):
    data = pd.concat([frame] * 25, ignore_index=True)
    data["reading_id"] = range(100)
    data["failed_within_7d"] = [1] * positives + [0] * (100 - positives)
    with pytest.raises(ValueError, match="degenerate"):
        validate_contract(data)


def test_ingest_records_known_sha256_without_parsing_or_modifying_source(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(b"abc")
    report = ingest(source, tmp_path / "raw")
    assert report == {
        "stage": "ingest", "source_file": "source.csv", "bytes": 3,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    }
    assert (tmp_path / "raw" / "sensors.csv").read_bytes() == b"abc"
    assert json.loads((tmp_path / "raw" / "ingest.json").read_text()) == report
    assert source.read_bytes() == b"abc"


def test_validate_publishes_exact_checked_bytes_and_report(csv_path, tmp_path):
    original = csv_path.read_bytes()
    output = tmp_path / "validated"
    report = validate(csv_path, output)
    assert report == {
        "stage": "validate", "status": "pass", "bytes": len(original), "rows": 4,
        "sha256": hashlib.sha256(original).hexdigest(),
    }
    assert (output / "sensors.csv").read_bytes() == original
    assert json.loads((output / "validation.json").read_text()) == report
    assert csv_path.read_bytes() == original


@pytest.mark.parametrize("operation", [ingest, validate])
def test_missing_input_fails_without_outputs(operation, tmp_path):
    output = tmp_path / "out"
    with pytest.raises(FileNotFoundError):
        operation(tmp_path / "missing.csv", output)
    assert not output.exists()


@pytest.mark.parametrize("operation", [ingest, validate])
def test_nonempty_output_is_never_overwritten(operation, csv_path, tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("existing work")
    with pytest.raises(FileExistsError, match="must be empty"):
        operation(csv_path, output)
    assert list(output.iterdir()) == [marker]
    assert marker.read_text() == "existing work"


@pytest.mark.parametrize("operation", [ingest, validate])
def test_input_directory_cannot_be_used_as_output(operation, csv_path):
    before = csv_path.read_bytes()
    with pytest.raises(FileExistsError, match="must be empty"):
        operation(csv_path, csv_path.parent)
    assert csv_path.read_bytes() == before


@pytest.mark.parametrize("operation", [ingest, validate])
def test_empty_existing_output_directory_is_supported(operation, csv_path, tmp_path):
    output = tmp_path / "mounted-output"
    output.mkdir()
    operation(csv_path, output)
    assert (output / "sensors.csv").read_bytes() == csv_path.read_bytes()


@pytest.mark.parametrize("content", [b"", b"a,b\n1,2,3\n4,5,6,7\n"])
def test_empty_or_malformed_csv_cannot_publish_pass(content, tmp_path):
    source = tmp_path / "bad.csv"
    source.write_bytes(content)
    output = tmp_path / "out"
    with pytest.raises(ValueError):
        validate(source, output)
    assert not output.exists()


def run_step(step, source, output, *, optimized=False):
    args = [sys.executable]
    if optimized:
        args.append("-O")
    return subprocess.run(
        [*args, "-m", "src.data_steps", step, "--input", str(source), "--output", str(output)],
        cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30,
    )


def test_cli_ingest_then_validate_uses_selected_file(csv_path, tmp_path):
    original = csv_path.read_bytes()
    raw = tmp_path / "run" / "raw"
    checked = tmp_path / "run" / "checked"
    first = run_step("ingest", csv_path, raw)
    assert first.returncode == 0, first.stderr
    second = run_step("validate", raw / "sensors.csv", checked)
    assert second.returncode == 0, second.stderr
    ingest_report = json.loads(first.stdout)
    validation_report = json.loads(second.stdout)
    assert ingest_report["sha256"] == validation_report["sha256"]
    assert validation_report["status"] == "pass"
    assert validation_report["rows"] == 4
    assert (checked / "sensors.csv").read_bytes() == original
    assert csv_path.read_bytes() == original


@pytest.mark.parametrize("corrupt, message", [
    pytest.param(lambda f: f.drop(columns=["temp_c"]), "schema mismatch", id="missing-column"),
    pytest.param(lambda f: f.assign(load_pct=250.0), "load_pct outside", id="out-of-range"),
    pytest.param(lambda f: f.assign(reading_id=1), "not unique", id="duplicate-id"),
    pytest.param(lambda f: f.assign(temp_c="hot"), "expected float64", id="wrong-type"),
])
def test_cli_rejects_the_actual_ingested_file(frame, csv_path, tmp_path, corrupt, message):
    original = csv_path.read_bytes()
    source = tmp_path / "bad.csv"
    corrupt(frame).to_csv(source, index=False)
    raw = tmp_path / "raw"
    first = run_step("ingest", source, raw)
    assert first.returncode == 0, first.stderr
    output = tmp_path / "checked"
    result = run_step("validate", raw / "sensors.csv", output)
    assert result.returncode == 1
    assert "VALIDATE FAIL" in result.stderr
    assert message in result.stderr
    assert not result.stdout
    assert not output.exists()
    assert csv_path.read_bytes() == original  # A separate valid file cannot hide the bad input.


@pytest.mark.parametrize("step", ["ingest", "validate"])
def test_cli_missing_input_fails_not_skips(step, tmp_path):
    output = tmp_path / "out"
    result = run_step(step, tmp_path / "missing.csv", output)
    assert result.returncode == 1
    assert f"{step.upper()} FAIL" in result.stderr
    assert not output.exists()


def test_cli_requires_explicit_paths():
    result = subprocess.run(
        [sys.executable, "-m", "src.data_steps", "validate"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 2
    assert "--input" in result.stderr
    assert "--output" in result.stderr


def test_optimized_python_cannot_disable_contract(frame, tmp_path):
    source = tmp_path / "bad.csv"
    frame.assign(load_pct=250.0).to_csv(source, index=False)
    output = tmp_path / "out"
    result = run_step("validate", source, output, optimized=True)
    assert result.returncode == 1
    assert "load_pct outside" in result.stderr
    assert not output.exists()
