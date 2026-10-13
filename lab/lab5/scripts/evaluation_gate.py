"""Local Lab 5 evaluation gate, adapted from the instructor's evaluation_gate.py.

Use an absolute validation ROC-AUC floor, one of the Task 1 gate options.
Exit 0 for pass and 1 for a failed check; this script does not register a model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

# Our lab quality floor, not an instructor requirement or an incumbent comparison.
# The held-out test score is reported separately, not used for this decision.
MIN_ABSOLUTE = 0.80
METRIC = "val_roc_auc"


def checked_auc(value: object, label: str) -> float:
    if (type(value) not in (int, float) or not 0 <= value <= 1
            or not math.isfinite(value)):
        raise ValueError(f"{label} must be a finite number between 0 and 1")
    return float(value)


def evaluate(payload: object, minimum: float = MIN_ABSOLUTE) -> float:
    """Return the accepted score, or raise before any registration can happen."""
    minimum = checked_auc(minimum, "--min-auc")
    if not isinstance(payload, dict) or METRIC not in payload:
        raise ValueError(f"Metrics JSON must be an object containing {METRIC}")
    candidate = checked_auc(payload[METRIC], METRIC)
    if candidate < minimum:
        raise ValueError(f"{METRIC}={candidate!r} < minimum {minimum!r}")
    return candidate


def decision(content: bytes, minimum: float = MIN_ABSOLUTE) -> dict:
    """Bind a passing decision to the exact metrics bytes used by the gate."""
    score = evaluate(json.loads(content), minimum)
    return {
        "stage": "evaluate", "status": "pass", "metric": METRIC,
        "minimum": float(minimum), "score": score,
        "training_record_sha256": hashlib.sha256(content).hexdigest(),
    }


def read_pass_report(path: Path, training_content: bytes) -> dict:
    """Reject stale/mixed decisions and a floor below the normal registration policy."""
    report = json.loads(path.read_bytes())
    if not isinstance(report, dict):
        raise ValueError("Gate report must be an object")
    minimum = checked_auc(report.get("minimum"), "gate minimum")
    checked_auc(report.get("score"), "gate score")
    if minimum < MIN_ABSOLUTE:
        raise ValueError("Gate minimum cannot be below the normal 0.80 floor")
    expected = decision(training_content, minimum)
    if report != expected:
        raise ValueError("Gate report does not match this training result")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, help="empty folder for gate.json")
    parser.add_argument(
        "--min-auc", type=float, default=MIN_ABSOLUTE,
        help="validation ROC-AUC floor (default: 0.80); 0.90 is used for the negative check",
    )
    args = parser.parse_args(argv)
    try:
        if args.output_dir is not None:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            if any(args.output_dir.iterdir()):
                raise FileExistsError("Gate output directory must be empty; use a new run directory")
        report = decision(args.metrics.read_bytes(), args.min_auc)
        candidate = report["score"]
        if args.output_dir is not None:
            with (args.output_dir / "gate.json").open("x", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2, allow_nan=False)
                stream.write("\n")
    except (OSError, ValueError) as exc:
        print(f"GATE FAIL: {exc}", file=sys.stderr)
        return 1

    print(
        f"GATE PASS: {METRIC}={candidate!r} >= minimum {args.min_auc!r} "
        "(absolute threshold only)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
