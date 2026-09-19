"""Compare one completed Lab 2 study without creating or updating MLflow runs.

Adapted from the instructor's scripts/compare_runs.py at
30ba98b02e8f233c7d7a36d584738ea442e46bd1. Keep its Markdown report and cost-per-point
comparison; scope the input to our 12 finished trials and calculate before rounding.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlflow
import pandas as pd

from src import config

PARAMETERS = ["n_estimators", "max_depth", "min_samples_leaf"]
METRICS = ["val_roc_auc", "test_roc_auc", "duration_s", "cost_thb"]
COMMON_FIELDS = {
    "seed": "params.seed",
    "instance": "params.instance",
    "git_commit": "tags.git_commit",
    "dvc_hash": "tags.dvc_hash",
    "data_fingerprint": "tags.data_fingerprint",
    "image_uri": "tags.image_uri",
}


def build_comparison(runs: pd.DataFrame, study_id: str) -> pd.DataFrame:
    """Reject incomplete or mixed evidence instead of inventing missing costs."""
    identity = {"run_id", "status", "tags.study_id", "tags.run_role"}
    if missing := identity - set(runs.columns):
        raise ValueError(f"Missing run fields: {sorted(missing)}")
    selected = runs.loc[
        (runs["tags.study_id"] == study_id)
        & (runs["tags.run_role"] == "trial")
        & (runs["status"] == "FINISHED")
    ].copy()
    if len(selected) != 12:
        raise ValueError(f"Expected 12 finished trials; found {len(selected)}")

    columns = {"run_id": "run_id", **{p: f"params.{p}" for p in PARAMETERS},
               **{m: f"metrics.{m}" for m in METRICS}, **COMMON_FIELDS}
    if missing := set(columns.values()) - set(selected.columns):
        raise ValueError(f"Missing comparison fields: {sorted(missing)}")
    table = selected[list(columns.values())].rename(columns={v: k for k, v in columns.items()})
    for name in ["run_id", *COMMON_FIELDS]:
        if table[name].isna().any() or table[name].astype(str).str.strip().eq("").any():
            raise ValueError(f"Missing {name}")
        if name != "run_id" and table[name].nunique() != 1:
            raise ValueError(f"Mixed {name} values in this study")
    if table["run_id"].duplicated().any():
        raise ValueError("Duplicate run IDs")

    for name in PARAMETERS + METRICS:
        table[name] = pd.to_numeric(table[name], errors="raise")
        if not table[name].map(math.isfinite).all():
            raise ValueError(f"Invalid {name}: values must be finite")
    for name in PARAMETERS:
        if ((table[name] <= 0) | (table[name] % 1 != 0)).any():
            raise ValueError(f"Invalid positive integer parameter: {name}")
        table[name] = table[name].astype(int)
        if table[name].nunique() < 2:
            raise ValueError(f"Parameter did not vary: {name}")
    if table.duplicated(PARAMETERS).any():
        raise ValueError("Duplicate configurations")
    for name in ["val_roc_auc", "test_roc_auc"]:
        if not table[name].between(0, 1).all():
            raise ValueError(f"Invalid {name}: expected a score between 0 and 1")
    if (table["duration_s"] <= 0).any() or (table["cost_thb"] < 0).any():
        raise ValueError("Invalid duration or cost")

    gain_points = (table["val_roc_auc"] - table["val_roc_auc"].min()) * 100
    # The worst trial has zero gain over itself: leave the ratio undefined, not huge.
    table["thb_per_point"] = table["cost_thb"] / gain_points.where(gain_points > 0)
    return table.sort_values(["val_roc_auc", "run_id"], ascending=[False, True]).reset_index(drop=True)


def render_report(table: pd.DataFrame, study_id: str, experiment: str) -> str:
    display = table[["run_id", *PARAMETERS, *METRICS, "thb_per_point"]].copy()
    display["thb_per_point"] = display["thb_per_point"].map(
        lambda value: "N/A" if pd.isna(value) else f"{value:.4f}",
    )
    first = table.iloc[0]
    lines = [
        "# Lab 2 — Run comparison", "",
        f"Experiment `{experiment}` · Study `{study_id}` · {len(table)} finished trials", "",
        "Adapted from the instructor's "
        "[compare_runs.py](https://github.com/pasdptt/public_teaching_mlaiops/blob/"
        "30ba98b02e8f233c7d7a36d584738ea442e46bd1/scripts/compare_runs.py).", "",
        f"Seed: `{first['seed']}`. Instance: `{first['instance']}`.",
        f"Code commit: `{first['git_commit']}`.",
        f"DVC hash: `{first['dvc_hash']}`. Data fingerprint: `{first['data_fingerprint']}`.",
        "", "Image:", "", "```text", str(first["image_uri"]), "```", "",
        f"Estimated measured-trial cost: **{math.fsum(table['cost_thb']):.4f} THB**.",
        "This is not the Azure bill. It excludes data setup, VM startup/idle time,",
        "final metric/checkpoint writes, and other services. Trial duration includes model logging.",
        "",
        "Price checked on 18 September 2026: Linux `Standard_F2s_v2` in `malaysiawest`",
        "costs USD 0.0882/hour for Dedicated/on-demand compute. We used Microsoft's",
        "THB reference price of 2.900677/hour for trial estimates. This is not the actual Azure bill.",
        "Source: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices).",
        "", "## Comparison", "",
        "Sorted by validation ROC-AUC. Test ROC-AUC is shown for reporting, not model selection.",
        "`thb_per_point` is the estimated trial cost divided by the validation ROC-AUC gain",
        "over the worst trial, in percentage points. Lower means less cost per added point",
        "against this baseline; it is not a final model-selection rule.",
        "`N/A` means zero gain over the baseline. Calculations use unrounded values;",
        "the table rounds only for display.", "",
        display.to_markdown(index=False, floatfmt=".4f", disable_numparse=[0]), "",
        "## Related evidence", "",
        "This table compares the original fixed-seed study. See the",
        "[model selection justification](../README.md#model-selection-justification)",
        "and the [five-seed results](lab2-seed-check.json) for the final choice and",
        "seed-variance check.", "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare the 12 finished trials in one Lab 2 study")
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--experiment", default="itcs355-lab2")
    parser.add_argument("--out", type=Path, default=Path("reports/lab2-comparison.md"))
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.study_id):
        parser.error("--study-id must be the 32-character hex ID from the study checkpoint")

    cfg = config.load(strict=False)
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    experiment = mlflow.get_experiment_by_name(args.experiment)
    if experiment is None:
        raise ValueError(f"Experiment not found: {args.experiment}")
    runs = mlflow.search_runs(
        experiment_ids=[experiment.experiment_id],
        filter_string=f"tags.study_id = '{args.study_id}'",
    )
    table = build_comparison(runs, args.study_id)
    report = render_report(table, args.study_id, args.experiment)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(f"Wrote {args.out}: {len(table)} finished trials; "
          f"estimated measured-trial cost {math.fsum(table['cost_thb']):.4f} THB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
