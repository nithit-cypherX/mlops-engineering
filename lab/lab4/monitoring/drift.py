"""Offline load_pct drift check for the pinned Lab 2 model, version 1.

PSI calculation reused from the instructor's monitoring/drift.py at
2e29faeb1fdaa6360947808e32b0e3c1444113f2 (quantile bins and +1 smoothing).
Calibration: python -m scripts.calibrate_drift (no training or cloud calls).
Check: python -m monitoring.drift --current-logs requests.json --as-of <UTC-time>

Input is a JSON list of completed requests from ONE endpoint, projected as:
timestamp (timezone-aware), path, status, model_version, load_pct_values.
The collector must scope the endpoint and use the log record's event timestamp.
This module does not fetch logs, emit metrics, schedule jobs or send alerts.
Exit codes: 0 below threshold, 1 invalid input, 2 drift, 3 insufficient data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.pull_data import DATA_SHA256
from src import config, data

FEATURE = "load_pct"
MODEL_VERSION = "1"
SPLIT_SEED = 20260101
BINS = 10
MIN_SAMPLES = 500
THRESHOLD = 0.60
WINDOW_MINUTES = 15
OFFSET_MINUTES = 2
# ponytail: one model/feature policy; ceiling: model 1, load_pct only;
# revisit when: the model/data changes or real traffic differs from calibration;
# upgrade: re-establish lineage and calibrate a policy for that population.


def load_reference(path: Path) -> pd.DataFrame:
    if hashlib.sha256(path.read_bytes()).hexdigest() != DATA_SHA256:
        raise ValueError("Reference data does not match the pinned training dataset.")
    train, _, _ = data.split(data.load_raw(path), seed=SPLIT_SEED)
    if len(train) != 3600 or train[data.GROUP].nunique() != 144:
        raise ValueError("Reference split does not match model 1 lineage.")
    return train


def _values(values) -> np.ndarray:
    if isinstance(values, (list, tuple)) and any(isinstance(v, (bool, np.bool_)) for v in values):
        raise ValueError("load_pct must not contain booleans.")
    array = np.asarray(values)
    if array.ndim != 1 or array.dtype.kind not in "iuf":
        raise ValueError("load_pct must contain numeric values, not strings or booleans.")
    array = array.astype(float)
    if not np.isfinite(array).all() or ((array < 0) | (array > 100)).any():
        raise ValueError("load_pct must be finite and within [0, 100].")
    return array


def psi(reference: np.ndarray, current: np.ndarray, bins: int = BINS) -> float:
    reference, current = _values(reference), _values(current)
    if not len(reference) or not len(current):
        raise ValueError("PSI needs nonempty reference and current samples.")
    if type(bins) is not int or bins < 2:
        raise ValueError("PSI needs at least two bins.")
    if np.ptp(reference) == 0:
        raise ValueError("A constant reference cannot define this PSI baseline.")
    edges = np.quantile(reference, np.linspace(0, 1, bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    edges = np.unique(edges)
    if len(edges) != bins + 1:
        raise ValueError("Reference cannot form the requested distinct quantile bins.")
    ref_counts, _ = np.histogram(reference, bins=edges)
    cur_counts, _ = np.histogram(current, bins=edges)
    ref_prop = (ref_counts + 1) / (ref_counts.sum() + len(ref_counts))
    cur_prop = (cur_counts + 1) / (cur_counts.sum() + len(cur_counts))
    return float(np.sum((cur_prop - ref_prop) * np.log(cur_prop / ref_prop)))


def utc_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Request timestamp must be an ISO-8601 time with a timezone.") from None
    if parsed.tzinfo is None:
        raise ValueError("Request timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def current_values(records: list[dict], *, start: datetime, end: datetime) -> np.ndarray:
    """Expand batch rows; invalid telemetry fails instead of hiding partial coverage."""
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("A nonempty timezone-aware window is required.")
    if not isinstance(records, list):
        raise ValueError("Current logs must be a JSON list of completed requests.")
    values = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Each request log must be an object.")
        if not isinstance(record.get("path"), str) or not record["path"]:
            raise ValueError("Request log is missing its path.")
        if record.get("path") not in ("/predict", "/predict/batch"):
            continue
        timestamp = utc_time(record.get("timestamp"))
        if not start <= timestamp < end:
            continue
        if not isinstance(record.get("model_version"), str) or not record["model_version"]:
            raise ValueError("Prediction log is missing its model version.")
        if record["model_version"] == "unknown":
            raise ValueError("Prediction log has unknown model lineage.")
        if record["model_version"] != MODEL_VERSION:
            continue
        status = record.get("status")
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError("Prediction log has an invalid HTTP status.")
        feature_values = record.get("load_pct_values")
        if feature_values is None and 400 <= status <= 499:
            continue  # Rejected input never reached the validated prediction handler.
        if not isinstance(feature_values, list):
            raise ValueError("Prediction log is missing feature telemetry.")
        if not 1 <= len(feature_values) <= 100:
            raise ValueError("Prediction log has an invalid batch size.")
        if record["path"] == "/predict" and len(feature_values) != 1:
            raise ValueError("A single prediction must contribute exactly one input row.")
        # A validated input still counts when scoring subsequently returns 5xx.
        values.extend(_values(feature_values).tolist())
    return _values(values)


def evaluate(reference: np.ndarray, current: np.ndarray) -> dict:
    reference, current = _values(reference), _values(current)
    # Validate reference even when no current data exists. Do not hide a bad baseline.
    psi(reference, reference)
    result = {
        "feature": FEATURE, "model_version": MODEL_VERSION,
        "reference_count": len(reference), "current_count": len(current),
        "bins": BINS, "min_samples": MIN_SAMPLES, "threshold": THRESHOLD,
        "psi": None, "status": "insufficient_data",
    }
    if len(current) >= MIN_SAMPLES:
        score = psi(reference, current)
        result.update(psi=score, status="drift_detected" if score >= THRESHOLD else "below_threshold")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=config.REPO_ROOT / "data/raw/sensors.csv")
    parser.add_argument("--current-logs", type=Path, required=True)
    parser.add_argument("--as-of", help="Timezone-aware run time; defaults to now in UTC.")
    args = parser.parse_args(argv)
    try:
        now = utc_time(args.as_of) if args.as_of else datetime.now(timezone.utc)
        end = now - timedelta(minutes=OFFSET_MINUTES)
        start = end - timedelta(minutes=WINDOW_MINUTES)
        reference = load_reference(args.reference)[FEATURE].to_numpy()
        records = json.loads(args.current_logs.read_text())
        current = current_values(records, start=start, end=end)
        result = evaluate(reference, current)
        result.update(window_start=start.isoformat(), window_end=end.isoformat(),
                      reference_sha256=DATA_SHA256, split_seed=SPLIT_SEED)
    except (OSError, ValueError):
        # Input/SDK details can contain sensitive data. Never echo raw records.
        print(json.dumps({"status": "invalid_input", "psi": None,
                          "reason": "Check reference integrity and the documented log schema."}))
        return 1
    print(json.dumps(result, allow_nan=False))
    return {"below_threshold": 0, "drift_detected": 2, "insufficient_data": 3}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
