"""One-shot cloud drift check: python -m monitoring.run_drift.

Run from the Lab 4 code root using the existing Lab 2 monitoring runtime.
Override that image's training ENTRYPOINT; this module never imports training.
No scheduler, resource creation, retry loop or alert configuration lives here.
Exit 0 means the check ran (including insufficient_data); inspect JSON status.
Exit 1 means invalid input, query failure or export failure. No fake zero score.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cloudlayer.factory import get_adapter
from monitoring import drift
from scripts.pull_data import DATA_SHA256
from src import config

METRIC_NAME = "lab4.drift.load_pct.psi"


def run(cfg, adapter, *, reference_path: Path, as_of: datetime) -> dict:
    if cfg.model_version != drift.MODEL_VERSION:
        raise ValueError("This policy is calibrated only for model version 1.")
    if as_of.tzinfo is None:
        raise ValueError("The evaluation time must have a timezone.")
    end = as_of.astimezone(timezone.utc) - timedelta(minutes=drift.OFFSET_MINUTES)
    start = end - timedelta(minutes=drift.WINDOW_MINUTES)
    reference = drift.load_reference(reference_path)[drift.FEATURE].to_numpy()
    records = adapter.read_prediction_logs(start=start, end=end, model_version=drift.MODEL_VERSION)
    values = drift.current_values(records, start=start, end=end)
    result = drift.evaluate(reference, values)
    result.update(window_start=start.isoformat(), window_end=end.isoformat(),
                  reference_sha256=DATA_SHA256, split_seed=drift.SPLIT_SEED,
                  metric_name=METRIC_NAME, metric_export="not_sent")
    if result["psi"] is not None:
        adapter.emit_metric(METRIC_NAME, result["psi"])
        result["metric_export"] = "accepted"  # Dashboard read-back is a separate check.
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=config.REPO_ROOT / "data/raw/sensors.csv")
    parser.add_argument("--as-of", help="Timezone-aware evaluation time; defaults to now in UTC.")
    args = parser.parse_args(argv)
    try:
        cfg = config.load_monitoring()
        now = drift.utc_time(args.as_of) if args.as_of else datetime.now(timezone.utc)
        result = run(cfg, get_adapter(cfg), reference_path=args.reference, as_of=now)
    except Exception:
        print(json.dumps({"status": "error", "psi": None, "metric_export": "unconfirmed",
                          "reason": "Check monitoring configuration, reference, logs and export."}))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
