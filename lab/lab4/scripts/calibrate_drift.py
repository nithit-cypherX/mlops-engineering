"""Reproduce the offline PSI policy check; stdout only, no model/cloud operations.

Bootstrap complete 25-reading machine blocks from the pinned TRAIN partition.
Select the smallest tested sample size with <=1% clean exceedances and >=95%
detection for BOTH +/-20 percentage-point shifts in 500 calibration windows.
Threshold = clean 99th percentile rounded UP to the next 0.05.
Freeze that policy, then check 500 fresh Monte Carlo windows (a different seed).
Confirmation goal: <=5% clean exceedances, >=95% detection in both directions.

These are artificial windows from the SAME training population, not independent
production observations. Clipping shifted values to [0,100] is deliberate.
Twenty points is a large (one-fifth of the allowed range) input shift, not a
claim about model accuracy or sensitivity to every smaller/other kind of drift.
"""
from __future__ import annotations

import json
import math

import numpy as np

from monitoring import drift
from src import config, data

CALIBRATION_SEED = 20261002
CONFIRMATION_SEED = 20261003
WINDOWS = 500
SAMPLE_SIZES = (100, 200, 500, 1000)


def window_scores(reference: np.ndarray, blocks: np.ndarray, n: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    scores = {key: [] for key in ("clean", "minus20", "plus20")}
    for _ in range(WINDOWS):
        current = blocks[rng.integers(len(blocks), size=n // blocks.shape[1])].reshape(-1)
        for name, values in (
            ("clean", current),
            ("minus20", np.clip(current - 20, 0, 100)),
            ("plus20", np.clip(current + 20, 0, 100)),
        ):
            scores[name].append(drift.psi(reference, values))
    return {key: np.asarray(values) for key, values in scores.items()}


def summarize(scores: dict, threshold: float) -> dict:
    return {
        key: {
            "breaches": int(np.count_nonzero(values >= threshold)),
            "windows": len(values),
            "rate": float(np.mean(values >= threshold)),
            "p50": float(np.quantile(values, .50)),
            "p95": float(np.quantile(values, .95)),
            "p99": float(np.quantile(values, .99)),
        }
        for key, values in scores.items()
    }


def calibrate() -> dict:
    train = drift.load_reference(config.REPO_ROOT / "data/raw/sensors.csv")
    reference = train[drift.FEATURE].to_numpy()
    groups = [part[drift.FEATURE].to_numpy() for _, part in train.groupby(data.GROUP, sort=True)]
    if any(len(group) != 25 for group in groups):
        raise ValueError("This calibration assumes 25 readings per machine; review it if data changes.")
    blocks = np.stack(groups)
    candidates = []
    for n in SAMPLE_SIZES:
        scores = window_scores(reference, blocks, n, CALIBRATION_SEED)
        threshold = math.ceil(float(np.quantile(scores["clean"], .99)) * 20) / 20
        results = summarize(scores, threshold)
        candidates.append({
            "samples": n, "machine_blocks": n // 25, "threshold": threshold,
            "results": results,
            "qualifies": results["clean"]["rate"] <= .01
            and min(results["minus20"]["rate"], results["plus20"]["rate"]) >= .95,
        })
    selected = next((candidate for candidate in candidates if candidate["qualifies"]), None)
    if selected is None:
        raise ValueError("No tested policy met the calibration goals.")
    confirmation = summarize(
        window_scores(reference, blocks, selected["samples"], CONFIRMATION_SEED),
        selected["threshold"],
    )
    confirmation_passed = (
        confirmation["clean"]["rate"] <= .05
        and min(confirmation["minus20"]["rate"], confirmation["plus20"]["rate"]) >= .95
    )
    return {
        "scope": "offline synthetic windows, not a live alert or production false-alert guarantee",
        "reference_sha256": drift.DATA_SHA256, "model_version": drift.MODEL_VERSION,
        "split_seed": drift.SPLIT_SEED, "reference_rows": len(reference),
        "reference_machines": len(blocks), "bins": drift.BINS, "smoothing": "add one per bin",
        "sampling": "complete machine blocks with replacement; 25 readings per block",
        "calibration_seed": CALIBRATION_SEED, "confirmation_seed": CONFIRMATION_SEED,
        "windows_per_scenario": WINDOWS,
        "selection_rule": "smallest tested N with clean <=1%, both shifts >=95%; threshold=ceil(q99*20)/20",
        "confirmation_rule": "fixed policy: clean <=5%, both shifts >=95%",
        "shift": "load_pct +/-20 percentage points, clipped to [0,100]",
        "candidates": candidates,
        "selected": {"min_samples": selected["samples"], "threshold": selected["threshold"]},
        "confirmation": confirmation, "confirmation_passed": confirmation_passed,
        "policy_matches_detector": (
            selected["samples"] == drift.MIN_SAMPLES and selected["threshold"] == drift.THRESHOLD
        ),
        "limitations": [
            "Resampling the training population is not independent production validation.",
            "500 rows may still come from fewer machines than simulated; logs do not include machine_id.",
            "Only load_pct and large +/-20-point shifts are calibrated, not all drift or model accuracy.",
            "A larger real dataset or different traffic mix requires recalibration.",
            "15-minute windows and 2-minute offset need live scheduled-job verification.",
        ],
    }


def main() -> int:
    result = calibrate()
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["confirmation_passed"] and result["policy_matches_detector"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
