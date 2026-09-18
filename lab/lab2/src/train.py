"""Training entry point.

Run locally:      PYTHONHASHSEED=20260101 python -m src.train --seed 20260101
Managed jobs pass --raw-path, --dvc-metadata-path, and --output-dir.

Every run logs: all hyperparameters, the seed, validation AND test metrics separately,
the data fingerprint, and the Git commit. A metric that cannot be traced to code and
data is not evidence of anything.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import mlflow
import mlflow.sklearn
import yaml
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from src import config, data, seeds


def git_commit() -> str:
    revision = config.IMAGE_GIT_COMMIT
    if revision:
        if not re.fullmatch(r"[0-9a-f]{40}(?:-dirty)?", revision):
            raise RuntimeError("Invalid image Git revision. Build with make image.")
        return revision
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, cwd=config.REPO_ROOT,
        )
        revision = out.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True, text=True, check=True, cwd=config.REPO_ROOT,
        )
        return revision + ("-dirty" if status.stdout.strip() else "")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Git revision unavailable. Build with make image.") from exc


def dvc_hash(path: Path) -> str:
    """Read the tracked directory version, not the CSV's SHA-256 fingerprint."""
    try:
        metadata = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Cannot read DVC metadata: {path}") from exc
    outs = metadata.get("outs") if isinstance(metadata, dict) else None
    if not isinstance(outs, list) or len(outs) != 1 or not isinstance(outs[0], dict):
        raise RuntimeError("Expected one raw directory in DVC metadata.")
    out = outs[0]
    value = out.get("md5")
    if (out.get("path") != "raw" or out.get("hash") != "md5"
            or not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{32}\.dir", value)):
        raise RuntimeError("Invalid raw directory hash in DVC metadata.")
    return value


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ITCS355 Lab 2 — reproducible training")
    p.add_argument("--n-estimators", type=int, default=200)
    p.add_argument("--max-depth", type=int, default=8)
    p.add_argument("--min-samples-leaf", type=int, default=5)
    p.add_argument("--seed", type=int, default=seeds.DEFAULT_SEED)
    p.add_argument("--experiment", default="itcs355-lab1")
    p.add_argument("--run-name", default=None)
    p.add_argument("--raw-path", type=Path, default=None,
                   help="Input CSV path. Defaults to the configured local dataset.")
    p.add_argument("--dvc-metadata-path", type=Path, default=None,
                   help="Input raw.dvc path. Defaults to the configured local metadata.")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Export model/ and metrics.json here for the job's output upload.")
    p.add_argument("--metrics-out", type=Path, default=None,
                   help="Write final metrics as JSON. Used by `make verify`.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    hash_seed = seeds.require_hash_seed(args.seed)
    cfg = config.load(strict=False)
    raw_path = args.raw_path if args.raw_path is not None else cfg.raw_path
    metadata_path = (args.dvc_metadata_path if args.dvc_metadata_path is not None
                     else cfg.dvc_metadata_path)
    seed = seeds.set_all(args.seed)
    version = dvc_hash(metadata_path)
    revision = git_commit()

    df = data.load_raw(raw_path)
    fingerprint = data.data_fingerprint(raw_path)
    train_df, val_df, test_df = data.split(df, seed=seed)

    model = RandomForestClassifier(
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        min_samples_leaf=args.min_samples_leaf,
        random_state=seed,
        n_jobs=-1,
    )

    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    mlflow.set_experiment(args.experiment)

    with mlflow.start_run(run_name=args.run_name):
        mlflow.log_params({
            **model.get_params(deep=False),
            "seed": seed,
            "python_hash_seed": hash_seed,
            "n_features": len(data.FEATURES),
        })
        # Provenance. This is what makes the metric traceable.
        mlflow.set_tags({
            "git_commit": revision,
            "dvc_hash": version,
            "data_fingerprint": fingerprint,
            "split_strategy": "group_by_machine_id",
            "n_train_rows": len(train_df),
            "n_val_rows": len(val_df),
            "n_test_rows": len(test_df),
        })

        model.fit(train_df[data.FEATURES], train_df[data.TARGET])

        metrics: dict[str, float] = {}
        for name, part in (("val", val_df), ("test", test_df)):
            proba = model.predict_proba(part[data.FEATURES])[:, 1]
            metrics[f"{name}_roc_auc"] = float(roc_auc_score(part[data.TARGET], proba))
            metrics[f"{name}_pr_auc"] = float(average_precision_score(part[data.TARGET], proba))
        mlflow.log_metrics(metrics)
        mlflow.sklearn.log_model(model, artifact_path="model")

        output_metrics = None
        if args.output_dir is not None:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            mlflow.sklearn.save_model(model, path=str(args.output_dir / "model"))
            output_metrics = args.output_dir / "metrics.json"

        result = json.dumps({"seed": seed, "data_fingerprint": fingerprint, **metrics}, indent=2)
        print(result)
        for path in (args.metrics_out, output_metrics):
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(result)


if __name__ == "__main__":
    main()
