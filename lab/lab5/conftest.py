"""Shared tiny-data fixtures for core tests and the adapter integration test."""
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import mlflow.sklearn
import numpy as np
import pandas as pd
import pytest

from src import seeds, train
from src.data_steps import ingest, validate


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
