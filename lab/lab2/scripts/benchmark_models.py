"""Compare two existing Lab 2 model artifacts locally; never train or contact Azure.

Download model/ from the two MLflow run IDs below into MODELS_DIR/100/model and
MODELS_DIR/300/model. Run in the original study image, with these directories and
data mounted read-only, networking disabled, two CPUs and native thread pools at 1.
Mount this script and check_seed_variance.py into /app/scripts; no image rebuild.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import joblib
import mlflow.sklearn
import numpy as np
import sklearn
import yaml
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from scripts import check_seed_variance as reference
from src import data, seeds

CASES = {
    "100": {"run_id": reference.REFERENCE_RUN,
            "metrics": reference.REFERENCE_METRICS},
    "300": {"run_id": "4a785cc0-6aef-41d3-a009-9f07bae57dba",
            "metrics": {"val_roc_auc": 0.8450585559019294,
                        "val_pr_auc": 0.40444499941701934}},
}
WARMUPS = 5
REPEATS = 50


def file_inventory(directory: Path) -> dict:
    return {
        str(path.relative_to(directory)): {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def load_verified_model(directory: Path, label: str):
    metadata = yaml.safe_load((directory / "MLmodel").read_text())
    if metadata.get("run_id") != CASES[label]["run_id"]:
        raise ValueError("Artifact run ID does not match the selected study trial.")
    if metadata["flavors"]["sklearn"]["sklearn_version"] != sklearn.__version__:
        raise ValueError("Use the original image's scikit-learn version.")
    files = file_inventory(directory)
    if files["model.pkl"]["bytes"] != metadata["model_size_bytes"]:
        raise ValueError("Model file size differs from MLflow metadata.")
    # Only the user's own retrieved artifacts are loaded, in an offline container.
    model = mlflow.sklearn.load_model(str(directory))
    params = {**reference.MODEL_PARAMS, "n_estimators": int(label),
              "random_state": reference.SPLIT_SEED}
    expected = RandomForestClassifier(**params).get_params(deep=False)
    if (not isinstance(model, RandomForestClassifier)
            or model.get_params(deep=False) != expected
            or len(model.estimators_) != int(label)
            or list(model.feature_names_in_) != data.FEATURES
            or list(model.classes_) != [0, 1]):
        raise ValueError("Saved model configuration or feature/class order differs.")
    return model, files


def verify_predictions(model, features, labels, expected):
    predictions = model.predict_proba(features)
    scores = {
        "val_roc_auc": float(roc_auc_score(labels, predictions[:, 1])),
        "val_pr_auc": float(average_precision_score(labels, predictions[:, 1])),
    }
    if any(not math.isclose(scores[k], v, rel_tol=0, abs_tol=1e-12)
           for k, v in expected.items()):
        raise ValueError("Reloaded model does not reproduce its Azure validation metrics.")
    return predictions, scores


def measure(models, features, expected_predictions, warmups=WARMUPS, repeats=REPEATS):
    """Time predict_proba only; consistency checks are outside the timed section."""
    if warmups < 1 or repeats < 2:
        raise ValueError("Use at least one warm-up and two measured rounds.")
    labels = list(models)
    for _ in range(warmups):
        for label in labels:
            models[label].predict_proba(features)
    samples = {label: [] for label in labels}
    for iteration in range(repeats):
        for label in (labels if iteration % 2 == 0 else labels[::-1]):
            started = time.perf_counter_ns()
            prediction = models[label].predict_proba(features)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            np.testing.assert_allclose(prediction, expected_predictions[label],
                                       rtol=0, atol=1e-12)
            if not math.isfinite(elapsed_ms) or elapsed_ms <= 0:
                raise ValueError("Invalid measured latency.")
            samples[label].append(elapsed_ms)
    return samples


def latency_summary(samples):
    return {
        "n": len(samples), "median_ms": float(np.median(samples)),
        "p95_ms": float(np.percentile(samples, 95, method="linear")),
        "min_ms": min(samples), "max_ms": max(samples),
    }


def benchmark(models_dir: Path, raw_path: Path, metadata_path: Path) -> dict:
    reference.check_inputs(raw_path, metadata_path)
    seeds.require_hash_seed(reference.SPLIT_SEED)
    seeds.set_all(reference.SPLIT_SEED)
    _, validation, _ = data.split(data.load_raw(raw_path), seed=reference.SPLIT_SEED)
    if len(validation) != 1200:
        raise ValueError("Expected the original 1,200-row validation split.")
    features, labels = validation[data.FEATURES], validation[data.TARGET]
    models, inventories, predictions, scores = {}, {}, {}, {}
    for label, case in CASES.items():
        models[label], inventories[label] = load_verified_model(
            models_dir / label / "model", label)
        predictions[label], scores[label] = verify_predictions(
            models[label], features, labels, case["metrics"])
    samples = measure(models, features, predictions)
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "reference": {
            "study_id": reference.REFERENCE_STUDY, "image": reference.IMAGE,
            "training_code_commit": reference.REFERENCE_COMMIT,
            "raw_sha256": reference.RAW_SHA256, "dvc_hash": reference.DVC_HASH,
            "split_seed": reference.SPLIT_SEED,
            "validation_reading_ids_sha256": hashlib.sha256(json.dumps(
                validation[data.ID].tolist(), separators=(",", ":")).encode()).hexdigest(),
        },
        "method": {
            "operation": "predict_proba", "rows_per_call": len(features),
            "features": data.FEATURES, "warmups_per_model_after_verification": WARMUPS,
            "measured_calls_per_model": REPEATS, "order": "100,300 then 300,100; alternating",
            "percentile_method": "NumPy linear interpolation",
            "timing_excludes": ["download", "load_model", "data loading/splitting",
                               "feature selection", "metric calculation", "output verification"],
        },
        "execution": {
            "location": "local Docker, not an Azure endpoint",
            "python": platform.python_version(), "scikit_learn": sklearn.__version__,
            "joblib": joblib.__version__, "numpy": np.__version__,
            "machine": platform.machine(), "effective_cpus": joblib.cpu_count(),
            "cpu_quota": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
            "native_thread_limits": {k: os.environ.get(k) for k in
                                     ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"]},
            "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "seed_checker_sha256": hashlib.sha256(Path(reference.__file__).read_bytes()).hexdigest(),
        },
        "models": {
            label: {
                "run_id": CASES[label]["run_id"],
                "params": models[label].get_params(deep=False),
                "validation_metrics": scores[label],
                "files": inventories[label],
                "model_pkl_bytes": inventories[label]["model.pkl"]["bytes"],
                "artifact_directory_bytes": sum(f["bytes"] for f in inventories[label].values()),
                "latency": latency_summary(samples[label]), "latency_samples_ms": samples[label],
            }
            for label in CASES
        },
        "limits": [
            "Latency is for a warm batch of 1,200 rows, not a single request or an endpoint.",
            "This measures these two saved models on one local machine in one benchmark session.",
            "Serialized file size is not RAM usage; no memory or load-time benchmark was done.",
            "No retraining, new seeds, cloud jobs, or MLflow writes were performed.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--raw-path", type=Path, required=True)
    parser.add_argument("--dvc-metadata-path", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("reports/lab2-model-benchmark.json"))
    args = parser.parse_args()
    report = benchmark(args.models_dir, args.raw_path, args.dvc_metadata_path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    for name, model in report["models"].items():
        print(name, "trees:", model["model_pkl_bytes"], "bytes;", model["latency"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

