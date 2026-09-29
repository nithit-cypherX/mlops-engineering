"""Behaviour checks for the registered model; never train or use a mock here.

Adapted from the instructor's tests/test_model_behaviour.py at
2e29faeb1fdaa6360947808e32b0e3c1444113f2. Run with make test-model.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from cloudlayer.factory import get_adapter
from src import config, data, seeds

# Use Lab 3's 200 ms API target as a loose ceiling for model inference alone.
# This is the mean of warm local calls, excluding HTTP, startup and concurrency.
# Passing here does not prove that the API meets its p95 target.
LATENCY_BUDGET_MS = 200.0


@pytest.fixture(scope="module")
def model():
    cfg = config.load_serving()
    loaded = get_adapter(cfg).load_model(cfg.model_registry_name, cfg.model_version)
    assert np.array_equal(loaded.classes_, [0, 1]), "Expected model classes [0, 1]"
    assert list(loaded.feature_names_in_) == data.FEATURES, "Model feature order changed"
    print(f"model_uri=models:/{cfg.model_registry_name}/{cfg.model_version}")
    return loaded


@pytest.fixture(scope="module")
def held_out_features():
    raw = config.REPO_ROOT / "data" / "raw" / "sensors.csv"
    if not raw.is_file():
        pytest.fail(
            "Missing data/raw/sensors.csv: restore the dataset recorded in data/raw.dvc.",
            pytrace=False,
        )
    frame = data.load_raw(raw)
    _, _, held_out = data.split(frame, seed=seeds.DEFAULT_SEED)
    print(f"data_fingerprint={data.data_fingerprint(raw)}; split_seed={seeds.DEFAULT_SEED}")
    return held_out[data.FEATURES]


def test_predictions_are_valid_probabilities(model, held_out_features):
    probabilities = np.asarray(model.predict_proba(held_out_features), dtype=float)
    assert probabilities.shape == (len(held_out_features), 2), "Wrong probability shape"
    assert np.isfinite(probabilities).all(), "Probabilities contain NaN or infinity"
    assert ((probabilities >= 0) & (probabilities <= 1)).all(), "Probability outside [0, 1]"
    assert np.allclose(probabilities.sum(axis=1), 1, rtol=0, atol=1e-8), (
        "Class probabilities do not sum to 1"
    )


def test_known_healthy_machine_scores_low(model):
    """Use the instructor's healthy input and risk threshold, chosen before running."""
    healthy = pd.DataFrame([{
        "temp_c": 66.0, "vibration_mm_s": 1.4, "pressure_kpa": 330.0,
        "hours_since_service": 50.0, "load_pct": 30.0, "ambient_humidity": 50.0,
    }])[data.FEATURES]
    risk = float(model.predict_proba(healthy)[0, 1])
    print(f"healthy_machine_risk={risk:.6f}; expected=0<=risk<0.5")
    assert np.isfinite(risk) and 0 <= risk < 0.5, (
        f"Healthy machine risk must be in [0, 0.5), got {risk}"
    )


def test_mean_prediction_latency_within_budget(model, held_out_features):
    """Time 20 warm calls on the same first held-out row; not a load test."""
    sample = held_out_features.head(1)
    model.predict_proba(sample)
    started = time.perf_counter()
    for _ in range(20):
        model.predict_proba(sample)
    mean_ms = (time.perf_counter() - started) * 1000 / 20
    print(f"mean_warm_prediction_ms={mean_ms:.3f}; budget_ms={LATENCY_BUDGET_MS}")
    assert mean_ms < LATENCY_BUDGET_MS, (
        f"Mean warm prediction took {mean_ms:.3f} ms; budget is <{LATENCY_BUDGET_MS} ms"
    )
