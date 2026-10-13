"""Lab 5 local training step, adapted from Lab 2.

Requires the validated CSV and its pass report. Exports an MLflow-format model
and metrics with lineage, without tracking-server, registry, or cloud calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

import mlflow.sklearn
import pandas as pd
import yaml
from mlflow.exceptions import MlflowException
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from src import data, seeds

ROOT = Path(__file__).resolve().parents[1]
MODEL_PACKAGES = ("scikit-learn", "numpy", "pandas", "cloudpickle", "mlflow")


def git_commit(source_commit: str | None = None) -> str:
    """Use local Git when available; accept declared lineage for a Git-free image."""
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}(?:-dirty)?", source_commit):
        raise ValueError("--git-commit must be a full lowercase SHA, optionally ending in -dirty")
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        if source_commit is not None:
            return source_commit
        raise RuntimeError("Git revision unavailable; supply --git-commit for a Git-free image") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Invalid Git revision")
    try:
        status = subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Cannot check local Git dirty state") from exc
    actual = revision + ("-dirty" if status.strip() else "")
    if source_commit is not None and source_commit != actual:
        raise ValueError("--git-commit does not match local HEAD and dirty state")
    return actual


def dvc_hash(path: Path) -> str:
    """Read the declared DVC directory version, distinct from the verified CSV SHA-256."""
    try:
        metadata = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Cannot read DVC metadata: {path}") from exc
    outs = metadata.get("outs") if isinstance(metadata, dict) else None
    if not isinstance(outs, list) or len(outs) != 1 or not isinstance(outs[0], dict):
        raise RuntimeError("Expected one raw directory in DVC metadata")
    out = outs[0]
    value = out.get("md5")
    if (out.get("path") != "raw" or out.get("hash") != "md5"
            or not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{32}\.dir", value)):
        raise RuntimeError("Invalid raw directory hash in DVC metadata")
    return value


def read_validated(directory: Path) -> tuple[pd.DataFrame, str]:
    report = json.loads((directory / "validation.json").read_text())
    if not isinstance(report, dict) or (
        report.get("stage") != "validate" or report.get("status") != "pass"
    ):
        raise ValueError("A passing validation report is required")
    content = (directory / "sensors.csv").read_bytes()
    fingerprint = hashlib.sha256(content).hexdigest()
    if report.get("sha256") != fingerprint:
        raise ValueError("Validated CSV SHA-256 does not match the validation report")
    frame = pd.read_csv(BytesIO(content))
    if (type(report.get("bytes")) is not int or report["bytes"] != len(content)
            or type(report.get("rows")) is not int or report["rows"] != len(frame)):
        raise ValueError("Validated CSV size/row count does not match the validation report")
    # A pass label alone is not evidence: check the same snapshot used for training.
    data.validate_contract(frame)
    return frame, fingerprint


def run_training(
    validated_dir: Path,
    dvc_metadata_path: Path,
    output_dir: Path,
    *,
    seed: int = seeds.DEFAULT_SEED,
    n_estimators: int = 100,
    max_depth: int = 12,
    min_samples_leaf: int = 10,
    source_commit: str | None = None,
) -> dict:
    hash_seed = seeds.require_hash_seed(seed)
    seeds.set_all(seed)
    frame, fingerprint = read_validated(validated_dir)
    declared_dvc_hash = dvc_hash(dvc_metadata_path)
    revision = git_commit(source_commit)
    train_df, val_df, test_df = data.split(frame, seed=seed)
    parts = {"train": train_df, "val": val_df, "test": test_df}
    for name, part in parts.items():
        if part[data.TARGET].nunique() != 2:
            raise ValueError(f"{name} split must contain both target classes")

    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise FileExistsError("Output directory must be empty; use a new run directory")
    model = RandomForestClassifier(
        n_estimators=n_estimators, max_depth=max_depth,
        min_samples_leaf=min_samples_leaf, random_state=seed, n_jobs=-1,
    )
    model.fit(train_df[data.FEATURES], train_df[data.TARGET])
    metrics = {}
    for name, part in (("val", val_df), ("test", test_df)):
        proba = model.predict_proba(part[data.FEATURES])[:, 1]
        metrics[f"{name}_roc_auc"] = float(roc_auc_score(part[data.TARGET], proba))
        metrics[f"{name}_pr_auc"] = float(average_precision_score(part[data.TARGET], proba))
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in metrics.values()):
        raise ValueError("Training produced invalid metrics")

    packages = {name: version(name) for name in MODEL_PACKAGES}
    result = {
        "seed": seed,
        "python_hash_seed": hash_seed,
        "params": model.get_params(deep=False),
        "features": data.FEATURES,
        "data_fingerprint": fingerprint,
        "dvc_hash": declared_dvc_hash,
        "git_commit": revision,
        "git_commit_source": "supplied" if source_commit is not None else "local_git",
        "split_strategy": "group_by_machine_id",
        "split_rows": {name: len(part) for name, part in parts.items()},
        "package_versions": packages,
        **metrics,
    }
    # Explicit installed versions avoid dependency-inference subprocesses.
    # save_model is a local export, not log_model or register_model.
    mlflow.sklearn.save_model(
        model, path=str(output_dir / "model"),
        pip_requirements=[f"{name}=={value}" for name, value in packages.items()],
        metadata=result.copy(),
    )
    # Bind the scores/lineage to this exact exported bundle, including its metadata.
    result["model_files_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((output_dir / "model").iterdir()) if path.is_file()
    }
    serialized = json.dumps(result, indent=2, allow_nan=False)
    with (output_dir / "metrics.json").open("x", encoding="utf-8") as stream:
        stream.write(serialized + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validated-dir", type=Path, required=True)
    parser.add_argument("--dvc-metadata-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=seeds.DEFAULT_SEED)
    parser.add_argument("--n-estimators", type=int, default=100)
    parser.add_argument("--max-depth", type=int, default=12)
    parser.add_argument("--min-samples-leaf", type=int, default=10)
    parser.add_argument(
        "--git-commit", dest="source_commit",
        help="source revision for an image without .git; must match local Git if available",
    )
    args = parser.parse_args(argv)
    try:
        result = run_training(**vars(args))
    except (OSError, ValueError, RuntimeError, MlflowException) as exc:
        print(f"TRAIN FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
