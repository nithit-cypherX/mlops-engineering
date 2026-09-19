"""Local seed check for the selected Lab 2 candidate; no MLflow or Azure writes.

Run this mounted script in the existing study image, with the dataset read-only.
The default checks only seed 20260101. Extra model seeds require an explicit list.
The split, NumPy/Python seed, and Python hash seed stay fixed at 20260101.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from src import data, seeds, train

REFERENCE_RUN = "8087ae5c-d06f-411e-b6b0-7f7891ffe7ed"
REFERENCE_STUDY = "34f2f83bfb9641e38347f8ddb04c1caf"
REFERENCE_COMMIT = "610e7cfefbfe080d711a7f621e1d6992fa803ad3"
IMAGE = (
    "itcs355u6688124.azurecr.io/itcs355-lab2@sha256:"
    "df878435dd990605f1c63dc4538b9cb86dacd31384d0a730c6b2d1c0399b282d"
)
RAW_SHA256 = "422cccb9136e814061b83e4f041cff21ba7372c6f7fb0feb2c321555c6a6341f"
DVC_HASH = "1c886b512c8a5c9bf723da1cd119fc80.dir"
SPLIT_SEED = 20260101
MODEL_PARAMS = {"n_estimators": 100, "max_depth": 12, "min_samples_leaf": 10, "n_jobs": -1}
# Unrounded validation metrics read from REFERENCE_RUN, not the rounded comparison table.
REFERENCE_METRICS = {
    "val_roc_auc": 0.8444994217173845,
    "val_pr_auc": 0.4044125077384774,
}
# Numerical reproduction check, not a threshold for model quality or equivalence.
BASELINE_ATOL = 1e-12


def validate_model_seeds(model_seeds: list[int]) -> None:
    if not model_seeds or model_seeds[0] != SPLIT_SEED:
        raise ValueError("Run baseline seed 20260101 first.")
    if len(set(model_seeds)) != len(model_seeds):
        raise ValueError("Duplicate model seeds would bias the variance.")
    if any(type(seed) is not int or not 0 <= seed < 2**32 for seed in model_seeds):
        raise ValueError("Model seeds must be integers between 0 and 2**32 - 1.")


def check_inputs(raw_path: Path, metadata_path: Path) -> None:
    if train.git_commit() != REFERENCE_COMMIT:
        raise ValueError("Code revision differs from the reference training image.")
    if hashlib.sha256(raw_path.read_bytes()).hexdigest() != RAW_SHA256:
        raise ValueError("Raw data SHA-256 differs from the Azure study.")
    if train.dvc_hash(metadata_path) != DVC_HASH:
        raise ValueError("DVC version differs from the Azure study.")


def fit_and_score(train_df, val_df, model_seed: int) -> dict:
    model = RandomForestClassifier(random_state=model_seed, **MODEL_PARAMS)
    model.fit(train_df[data.FEATURES], train_df[data.TARGET])
    proba = model.predict_proba(val_df[data.FEATURES])[:, 1]
    return {
        "model_seed": model_seed,
        "val_roc_auc": float(roc_auc_score(val_df[data.TARGET], proba)),
        # Same average-precision definition called PR-AUC by the original training code.
        "val_pr_auc": float(average_precision_score(val_df[data.TARGET], proba)),
    }


def check_baseline(row: dict) -> dict[str, float]:
    deltas = {name: row[name] - expected for name, expected in REFERENCE_METRICS.items()}
    if any(not math.isclose(row[name], expected, rel_tol=0, abs_tol=BASELINE_ATOL)
           for name, expected in REFERENCE_METRICS.items()):
        raise ValueError(f"Baseline metrics differ from Azure; stop before other seeds: {deltas}")
    return deltas


def summarize(rows: list[dict]) -> dict:
    result = {}
    for metric in REFERENCE_METRICS:
        values = [row[metric] for row in rows]
        result[metric] = {
            "n": len(values), "mean": statistics.mean(values),
            "sample_variance": statistics.variance(values) if len(values) > 1 else None,
            "sample_stddev": statistics.stdev(values) if len(values) > 1 else None,
            "min": min(values), "max": max(values),
        }
    return result


def run_check(raw_path: Path, metadata_path: Path, model_seeds: list[int]) -> dict:
    validate_model_seeds(model_seeds)
    seeds.require_hash_seed(SPLIT_SEED)
    check_inputs(raw_path, metadata_path)
    seeds.set_all(SPLIT_SEED)
    parts = data.split(data.load_raw(raw_path), seed=SPLIT_SEED)
    names = ("train", "validation", "test")
    if tuple(map(len, parts)) != (3600, 1200, 1200):
        raise ValueError("Split sizes differ from the reference study.")
    groups = [set(part[data.GROUP]) for part in parts]
    if any(groups[i] & groups[j] for i, j in ((0, 1), (0, 2), (1, 2))):
        raise ValueError("A machine occurs in more than one data partition.")

    # Split once. Never train on or score the held-out test set in this check.
    rows = []
    for model_seed in model_seeds:
        row = fit_and_score(parts[0], parts[1], model_seed)
        if any(not math.isfinite(row[name]) or not 0 <= row[name] <= 1
               for name in REFERENCE_METRICS):
            raise ValueError("Validation metrics must be finite scores between 0 and 1.")
        if not rows:
            baseline_deltas = check_baseline(row)
        rows.append(row)

    return {
        "status": "baseline_only" if len(rows) == 1 else "seed_check_complete",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "reference": {
            "mlflow_run_id": REFERENCE_RUN, "study_id": REFERENCE_STUDY,
            "training_code_commit": REFERENCE_COMMIT, "required_image": IMAGE,
            "raw_sha256": RAW_SHA256, "dvc_hash": DVC_HASH,
            "validation_metrics": REFERENCE_METRICS,
        },
        "execution": {
            "location": "local", "python": platform.python_version(),
            "machine": platform.machine(), "scikit_learn": sklearn.__version__,
            "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_sha256": {
                Path(module.__file__).name: hashlib.sha256(
                    Path(module.__file__).read_bytes()).hexdigest()
                for module in (data, seeds, train)
            },
            "scope": "Fixed split and global seeds; vary only RandomForest random_state.",
            "cost_note": "No Azure training job. Local runtime is not priced as Azure compute.",
        },
        "split_seed": SPLIT_SEED,
        "global_seed": SPLIT_SEED,
        "python_hash_seed": SPLIT_SEED,
        "splits": {
            name: {
                "rows": len(part), "machines": len(group),
                "reading_ids_sha256": hashlib.sha256(
                    json.dumps(part[data.ID].tolist(), separators=(",", ":")).encode()
                ).hexdigest(),
            }
            for name, part, group in zip(names, parts, groups)
        },
        "baseline_model_params": RandomForestClassifier(
            random_state=SPLIT_SEED, **MODEL_PARAMS).get_params(deep=False),
        "baseline_check": {"passed": True, "absolute_tolerance": BASELINE_ATOL,
                           "deltas": baseline_deltas},
        "results": rows,
        "summary": summarize(rows),
        "limits": [
            "One seed cannot estimate variance; null does not mean zero variance.",
            "Results are conditional on one fixed group split, not different datasets.",
            "Only this configuration is checked; this does not prove equivalence to 300 trees.",
            "Do not pick a lucky seed or replace the original candidate artifact from this check.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-path", type=Path, required=True)
    parser.add_argument("--dvc-metadata-path", type=Path, required=True)
    parser.add_argument("--model-seeds", type=int, nargs="+", default=[SPLIT_SEED])
    parser.add_argument("--out", type=Path, default=Path("reports/lab2-seed-check.json"))
    args = parser.parse_args()
    result = run_check(args.raw_path, args.dvc_metadata_path, args.model_seeds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Baseline PASS; {len(result['results'])} seed(s) recorded in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

