"""Lab 2 budgeted study, adapted from the instructor's src/tune.py.

Supply an explicit study allocation with --budget-thb and the actual --instance.
The 150 THB lab budget also covers prior jobs and non-trial cloud costs.

The next-trial estimate is max(--trial-estimate-s, twice the longest observed
attempt). It is a planning guard, not a VM timeout or an Azure billing limit.
duration_s covers the trial through model logging; it excludes data setup, final
metric/checkpoint writes, VM startup/idle, and other services.

Managed jobs publish checkpoints during the study. A resumed job reads its
predecessor's checkpoint but writes to its own key. Real cloud interruption
verification still belongs to Task 2.3 Part 3.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import time
from pathlib import Path
from uuid import uuid4

import mlflow
import mlflow.sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from cloudlayer.factory import get_adapter
from src import config, costs, data, seeds
from src.train import dvc_hash, git_commit

# Instructor starter: 30ba98b02e8f233c7d7a36d584738ea442e46bd1.
# Keep its grid search; widen min_samples_leaf from [1, 5] to the agreed [1, 10].
SEARCH_SPACE: dict[str, list] = {
    "n_estimators": [100, 300],
    "max_depth": [4, 8, 12],
    "min_samples_leaf": [1, 10],
}


def grid(space: dict[str, list]) -> list[dict]:
    keys = list(space)
    return [dict(zip(keys, values)) for values in itertools.product(*(space[k] for k in keys))]


def positive_finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ITCS355 Lab 2 — budgeted study")
    p.add_argument("--trials", type=int, choices=range(1, 13), default=12,
                   help="Submission needs all 12; smaller values are partial studies.")
    p.add_argument("--budget-thb", type=positive_finite, required=True,
                   help="Allocation for measured trial work, not the whole Azure bill.")
    p.add_argument("--instance", required=True, help="Actual instance key in src/costs.py")
    p.add_argument("--trial-estimate-s", type=positive_finite, default=600.0,
                   help="Planning floor per trial, initially 10 minutes; not a timeout.")
    p.add_argument("--seed", type=int, default=seeds.DEFAULT_SEED)
    p.add_argument("--experiment", default="itcs355-lab2")
    p.add_argument("--raw-path", type=Path, default=None)
    p.add_argument("--dvc-metadata-path", type=Path, default=None)
    p.add_argument("--checkpoint", type=Path, default=Path("reports/tune_checkpoint.json"),
                   help="Local checkpoint file; managed jobs also publish it to Blob.")
    p.add_argument("--checkpoint-key", default=None, help="New job's relative Blob output key")
    p.add_argument("--resume-uri", default=None, help="Read a stopped job's cloud checkpoint")
    p.add_argument("--image-uri", default=None, help="Immutable image used for this study")
    p.add_argument("--test-interruption", action="store_true",
                   help="Test only: fail after four completed trials are checkpointed to cloud; "
                        "new studies only, omit when resuming.")
    args = p.parse_args()
    if args.budget_thb > 150:
        p.error("--budget-thb cannot exceed the whole-lab limit of 150 THB")
    if not 0 <= args.seed < 2**32:
        p.error("--seed must be between 0 and 2**32 - 1")
    if args.resume_uri and not args.checkpoint_key:
        p.error("--resume-uri requires a new --checkpoint-key")
    if args.test_interruption and (
        not args.checkpoint_key or args.resume_uri or args.trials <= 4
    ):
        p.error("--test-interruption requires a new cloud study with more than four trials; "
                "omit it when resuming")
    if args.checkpoint_key:
        if not re.fullmatch(r"studies/[A-Za-z0-9_-]+/checkpoint\.json", args.checkpoint_key):
            p.error("--checkpoint-key must be studies/<job-id>/checkpoint.json")
        if not args.image_uri or not re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", args.image_uri):
            p.error("Cloud checkpoints require a digest-pinned --image-uri")
    return args


def load_checkpoint(path: Path, context: dict) -> dict:
    if not path.exists():
        return {"schema_version": 1, "context": context, "study_id": uuid4().hex,
                "completed": [], "completed_runs": {}, "in_progress": None,
                "spent_thb": 0.0, "max_duration_s": 0.0}
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or state.get("context") != context:
        raise ValueError("Checkpoint does not match this code/data/seed/grid/price; use a new path.")
    completed = state.get("completed")
    valid_keys = {json.dumps(params, sort_keys=True) for params in grid(SEARCH_SPACE)}
    if (not isinstance(state.get("study_id"), str) or not state["study_id"]
            or not isinstance(completed, list)
            or any(not isinstance(key, str) or key not in valid_keys for key in completed)
            or len(completed) != len(set(completed))):
        raise ValueError("Invalid checkpoint study ID or completed trials.")
    for name in ("spent_thb", "max_duration_s"):
        value = state.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid checkpoint {name}; refusing to reset spending.")
    completed_runs = state.get("completed_runs")
    if (type(state.get("schema_version")) is not int or state["schema_version"] != 1
            or not isinstance(completed_runs, dict)
            or set(completed_runs) != set(completed)
            or any(not isinstance(run_id, str) or not run_id for run_id in completed_runs.values())
            or len(set(completed_runs.values())) != len(completed_runs)
            or "in_progress" not in state):
        raise ValueError("Invalid checkpoint schema or completed run references.")
    pending = state["in_progress"]
    if pending is not None and (
        not isinstance(pending, dict) or set(pending) != {"key", "run_id"}
        or not isinstance(pending["key"], str) or pending["key"] not in valid_keys
        or pending["key"] in completed or not isinstance(pending["run_id"], str)
        or not pending["run_id"] or pending["run_id"] in completed_runs.values()
    ):
        raise ValueError("Invalid checkpoint in-progress trial.")
    return state


def save_checkpoint(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, allow_nan=False))
    temporary.replace(path)


def recover_interrupted_cost(state: dict, client, rate: float) -> None:
    """Recover a measured attempt cost, or stop rather than treating lost time as free."""
    pending = state["in_progress"]
    if pending is None:
        return
    run = client.get_run(pending["run_id"])
    expected = {"study_id": state["study_id"], **{
        key: str(state["context"][key]) for key in ("git_commit", "dvc_hash", "data_fingerprint")
    }}
    if any(run.data.tags.get(key) != value for key, value in expected.items()):
        raise ValueError("Interrupted run lineage does not match the checkpoint")
    duration = run.data.metrics.get("duration_s")
    cost = run.data.metrics.get("cost_thb")
    if (run.info.status not in {"FINISHED", "FAILED", "KILLED"}
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in (duration, cost))
            or not math.isclose(cost, duration / 3600 * rate, rel_tol=1e-9, abs_tol=1e-12)):
        raise RuntimeError(f"Reconcile interrupted trial {pending['run_id']} before resuming: "
                           "final duration/cost is unavailable or inconsistent; no new trials started")
    state["spent_thb"] += cost
    state["max_duration_s"] = max(state["max_duration_s"], duration)
    state["in_progress"] = None
    # Completion was not durably recorded: retain its cost, but retry this configuration.
    print(f"Recovered measured cost for interrupted attempt {pending['run_id']}: {cost:.6f} THB")


def main() -> None:
    args = parse_args()
    hash_seed = seeds.require_hash_seed(args.seed)
    cfg = config.load(strict=False)
    seed = seeds.set_all(args.seed)
    rate = costs.hourly_rate(cfg.provider, args.instance, spot=False)
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("Hourly rate must be finite and non-negative.")
    raw_path = args.raw_path if args.raw_path is not None else cfg.raw_path
    metadata_path = (args.dvc_metadata_path if args.dvc_metadata_path is not None
                     else cfg.dvc_metadata_path)
    revision = git_commit()
    version = dvc_hash(metadata_path)
    fingerprint = data.data_fingerprint(raw_path)
    context = {
        "git_commit": revision, "dvc_hash": version, "data_fingerprint": fingerprint,
        "seed": seed, "search_space": SEARCH_SPACE, "provider": cfg.provider,
        "instance": args.instance, "hourly_rate_thb": rate,
    }
    adapter = None
    checkpoint_uri = None
    if args.checkpoint_key:
        if cfg.provider.lower() == "local":
            raise ValueError("Cloud checkpoints require a cloud provider, not local")
        checkpoint_uri = cfg.blob_uri.rstrip("/") + "/" + args.checkpoint_key
        context.update(image_uri=args.image_uri, tracking_uri=cfg.mlflow_tracking_uri,
                       budget_thb=args.budget_thb)
        adapter = get_adapter(cfg)
        if args.resume_uri:
            if args.resume_uri == checkpoint_uri:
                raise ValueError("Resume must write to a new job's checkpoint, not overwrite its source")
            adapter.download(args.resume_uri, str(args.checkpoint))
            if not args.checkpoint.is_file():
                raise FileNotFoundError("Resume checkpoint was not downloaded")
        elif args.checkpoint.exists():
            raise ValueError("New cloud study requires a fresh local checkpoint path")
    state = load_checkpoint(args.checkpoint, context)

    def persist() -> None:
        save_checkpoint(args.checkpoint, state)
        if adapter is not None:
            adapter.upload(str(args.checkpoint), args.checkpoint_key)
            print(f"Checkpoint saved: {checkpoint_uri}; completed={len(state['completed'])}", flush=True)

    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    had_pending = state["in_progress"] is not None
    if had_pending:
        recover_interrupted_cost(state, mlflow.MlflowClient(), rate)
    if adapter is not None or had_pending:
        persist()  # A failed initial upload stops before any model work.
    candidates = grid(SEARCH_SPACE)[:args.trials]

    df = data.load_raw(raw_path)
    # One fixed split for the whole study, not a new split for each configuration.
    train_df, val_df, test_df = data.split(df, seed=seed)
    tags = {
        "lab": "2", "study_id": state["study_id"], "git_commit": revision,
        "dvc_hash": version, "data_fingerprint": fingerprint,
        "split_strategy": "group_by_machine_id", "n_train_rows": str(len(train_df)),
        "n_val_rows": str(len(val_df)), "n_test_rows": str(len(test_df)),
        "cost_basis": "dedicated_trial_through_model_log_estimate",
    }
    if checkpoint_uri:
        tags.update(checkpoint_uri=checkpoint_uri, image_uri=args.image_uri)
    mlflow.set_experiment(args.experiment)

    skipped: list[dict] = []
    # The outer run also consumes a platform-provided MLFLOW_RUN_ID, if present.
    # Each nested run then owns one configuration instead of overwriting that run.
    with mlflow.start_run(run_name="budgeted-study", tags={**tags, "run_role": "study"}):
        mlflow.log_params({"budget_thb": args.budget_thb, "instance": args.instance,
                           "trial_estimate_s": args.trial_estimate_s})
        for i, params in enumerate(candidates):
            key = json.dumps(params, sort_keys=True)
            if key in state["completed"]:
                print(f"trial {i}: already done, skipping (checkpoint run {state['completed_runs'][key]})")
                continue

            estimate_s = max(args.trial_estimate_s, 2 * state["max_duration_s"])
            projected_cost = estimate_s / 3600.0 * rate
            if state["spent_thb"] + projected_cost > args.budget_thb:
                skipped.append(params)
                continue

            started = time.perf_counter()
            with mlflow.start_run(run_name=f"trial-{i:02d}", nested=True,
                                  tags={**tags, "run_role": "trial"}) as trial:
                state["in_progress"] = {"key": key, "run_id": trial.info.run_id}
                persist()  # Persist the attempt identity before training, not only on success.
                try:
                    model = RandomForestClassifier(random_state=seed, n_jobs=-1, **params)
                    mlflow.log_params({
                        **model.get_params(deep=False), "seed": seed,
                        "python_hash_seed": hash_seed, "n_features": len(data.FEATURES),
                        "instance": args.instance, "provider": cfg.provider,
                        "hourly_rate_thb": rate, "projected_cost_thb": projected_cost,
                    })
                    model.fit(train_df[data.FEATURES], train_df[data.TARGET])
                    metrics = {}
                    for name, part in (("val", val_df), ("test", test_df)):
                        proba = model.predict_proba(part[data.FEATURES])[:, 1]
                        metrics[f"{name}_roc_auc"] = float(
                            roc_auc_score(part[data.TARGET], proba))
                        metrics[f"{name}_pr_auc"] = float(
                            average_precision_score(part[data.TARGET], proba))
                    mlflow.sklearn.log_model(model, artifact_path="model")
                finally:
                    # A failed fit/model upload still consumes time. Do not mark it done.
                    elapsed_s = time.perf_counter() - started
                    trial_cost = elapsed_s / 3600.0 * rate
                    mlflow.log_metrics({"duration_s": elapsed_s, "cost_thb": trial_cost})
                    state["spent_thb"] += trial_cost
                    state["max_duration_s"] = max(state["max_duration_s"], elapsed_s)
                    state["in_progress"] = None
                    persist()

                mlflow.log_metrics(metrics)

            # Only a finished run with its model and metrics is eligible to be skipped.
            state["completed"].append(key)
            state["completed_runs"][key] = trial.info.run_id
            persist()
            print(f"trial {i}: {params} -> val_roc_auc={metrics['val_roc_auc']:.4f} "
                  f"cost={trial_cost:.4f} THB cumulative={state['spent_thb']:.4f}")
            if args.test_interruption and len(state["completed"]) == 4:
                # Deliberate failure between trials, not a Spot eviction or mid-fit kill.
                message = ("CONTROLLED INTERRUPTION TEST: four completed trials checkpointed "
                           "to cloud; stopping before trial 5. Resume without --test-interruption.")
                print(message, flush=True)
                raise RuntimeError(message)

        mlflow.log_metrics({"estimated_spent_thb": state["spent_thb"],
                            "completed_trials": len(state["completed"]),
                            "budget_skipped_trials": len(skipped)})

    print(f"Estimated measured-trial spend: {state['spent_thb']:.4f} "
          f"of allocated {args.budget_thb:.4f} THB (not the full Azure bill).")
    if skipped:
        print(f"BUDGET GUARD — {len(skipped)} configurations not run:")
        for params in skipped:
            print(params)
    if state["spent_thb"] > args.budget_thb:
        print("WARNING: observed duration exceeded the estimate and study allocation.")
    print(f"Checkpoint: {checkpoint_uri or args.checkpoint}. "
          "Real cloud interruption/resume verification is a separate check.")


if __name__ == "__main__":
    main()
