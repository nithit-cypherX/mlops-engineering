"""Lab 2 — reload a registered model by version and score five held-out rows.

Adapted from the instructor's scripts/reload_check.py at commit
30ba98b02e8f233c7d7a36d584738ea442e46bd1.

    python scripts/reload_check.py --name itcs355-<studentid> --version 1

Loading a local model file instead of the registry defeats the purpose of Task 5.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlflow
import mlflow.sklearn
import numpy as np

from src import config, data, seeds


def main() -> int:
    ap = argparse.ArgumentParser(description="Reload one model version and score five test rows")
    ap.add_argument("--name", required=True, help="Registered model name")
    ap.add_argument("--version", required=True, help="Positive version number, not a stage or alias")
    ap.add_argument("--rows", type=int, choices=[5], default=5)
    args = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,254}", args.name):
        ap.error("--name must be a model name without spaces or slashes")
    if not re.fullmatch(r"[1-9][0-9]*", args.version):
        ap.error("--version must be a positive version number, not latest or Staging")

    cfg = config.load(strict=False)
    df = data.load_raw(cfg.raw_path)
    # The Lab 2 study used this same fixed, group-aware split for every trial.
    _, _, test_df = data.split(df, seed=seeds.DEFAULT_SEED)
    sample = test_df.head(args.rows)
    if len(sample) != args.rows:
        raise ValueError("The test split must contain at least five held-out rows")
    features = sample[data.FEATURES]

    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    mlflow.set_registry_uri(cfg.mlflow_tracking_uri)
    uri = f"models:/{args.name}/{args.version}"
    print(f"loading {uri}")
    model = mlflow.sklearn.load_model(uri)
    if not np.array_equal(model.classes_, [0, 1]):
        raise ValueError("Expected binary classes [0, 1] so column 1 means failure")
    probabilities = np.asarray(model.predict_proba(features), dtype=float)
    if (probabilities.shape != (args.rows, 2)
            or not np.isfinite(probabilities).all()
            or not ((0 <= probabilities) & (probabilities <= 1)).all()
            or not np.allclose(probabilities.sum(axis=1), 1, rtol=0, atol=1e-8)):
        raise ValueError("Expected five rows of finite class probabilities in [0, 1] summing to 1")

    for rid, p in zip(sample[data.ID], probabilities[:, 1]):
        print(f"  reading {rid}: p(failure)={p:.4f}")
    print("\nPASS  model reloaded from the registry and scored five held-out rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
