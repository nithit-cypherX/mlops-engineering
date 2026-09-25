"""One bounded Lab 3 canary drill. --help is offline; --execute permits cloud writes.

Requires a separately registered candidate. No training, registration or image build.
The labeled replay measures quality, not the separate Task 3 latency requirement.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from sklearn.metrics import roc_auc_score

from cloudlayer.factory import get_adapter
from service.schemas import PredictRequest
from src import config, data

SEED = 20260101
FINGERPRINT = "422cccb9136e8140"
BASELINE_RUN = "8087ae5c-d06f-411e-b6b0-7f7891ffe7ed"
CANDIDATE_RUN = "1730032f-4352-4781-99dc-687b35f1778c"
MAX_REQUESTS = 600
MEASURE_SECONDS = 300
AUC_GAP = 0.01  # Approved drill threshold, not a significance test or course-mandated value.


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validation_rows(path):
    if data.data_fingerprint(path) != FINGERPRINT:
        raise ValueError("Use the reviewed Lab 2 raw dataset; do not generate a replacement")
    _, frame, _ = data.split(data.load_raw(path), SEED)
    if (len(frame) != 1200 or not frame[data.ID].is_unique
            or frame[data.TARGET].value_counts().to_dict() != {0: 1079, 1: 121}):
        raise ValueError("Validation cohort differs from the reviewed 1,200 rows")
    rows = []
    for record in frame.to_dict("records"):
        features = {key: record[key] for key in data.FEATURES}
        PredictRequest.model_validate(features)
        rows.append({"id": int(record[data.ID]), "label": int(record[data.TARGET]),
                     "features": features})
    random.Random(SEED).shuffle(rows)
    return rows


class BlindScores:
    """No model version/identity enters this decision: only A/B scores and labels."""

    def __init__(self, rows):
        self.labels = {row["id"]: row["label"] for row in rows}
        if len(self.labels) != len(rows) or set(self.labels.values()) != {0, 1}:
            raise ValueError("Expected unique IDs and both target classes")
        self.scores = {"A": {}, "B": {}}

    def add(self, group, ids, probabilities):
        if group not in self.scores or len(ids) != len(probabilities) or len(set(ids)) != len(ids):
            raise ValueError("Invalid response group or batch length/IDs")
        for row_id, probability in zip(ids, probabilities):
            if (row_id not in self.labels or isinstance(probability, bool)
                    or not isinstance(probability, (int, float))
                    or not math.isfinite(probability) or not 0 <= probability <= 1):
                raise ValueError("Expected a known row and a finite probability in [0,1]")
            previous = self.scores[group].get(row_id)
            if previous is not None and not math.isclose(previous, probability, abs_tol=1e-12, rel_tol=0):
                raise ValueError("The same model/row returned different predictions")
        self.scores[group].update(zip(ids, probabilities))

    def decision(self):
        counts = {group: len(scores) for group, scores in self.scores.items()}
        if any(set(scores) != set(self.labels) for scores in self.scores.values()):
            return {"status": "incomplete", "unique_rows": counts}
        ids = sorted(self.labels)
        labels = [self.labels[i] for i in ids]
        auc = {group: float(roc_auc_score(labels, [scores[i] for i in ids]))
               for group, scores in self.scores.items()}
        lower = min(auc, key=auc.get)
        gap = abs(auc["A"] - auc["B"])
        return {"status": "degradation" if gap >= AUC_GAP else "no_alarm",
                "lower_group": lower, "roc_auc": auc, "gap": gap, "threshold": AUC_GAP,
                "unique_rows": counts}


def run_drill(adapter, plan, rows, output):
    """One attempt. Always try cleanup after the first possible cloud mutation."""
    if (plan["models"][plan["baseline_version"]]["run_id"] != BASELINE_RUN
            or plan["models"][plan["candidate_version"]]["run_id"] != CANDIDATE_RUN):
        raise ValueError("Registered versions do not match the two reviewed Lab 2 runs")
    scores = BlindScores(rows)
    batches = [rows[i:i + 100] for i in range(0, len(rows), 100)]
    versions = [plan["baseline_version"], plan["candidate_version"]]
    random.SystemRandom().shuffle(versions)
    group_for_version = dict(zip(versions, ("A", "B")))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite another attempt's evidence.
    summary = {"status": "in_progress", "started_at": utc_now(), "data_fingerprint": FINGERPRINT,
               "seed": SEED, "validation_rows": len(rows), "labels": scores.labels,
               "limits": {"requests": MAX_REQUESTS, "measurement_seconds": MEASURE_SECONDS},
               "predictions": {"A": 0, "B": 0}, "requests": {"A": 0, "B": 0}}
    # Receipt exists before the first write; it identifies both revisions for manual recovery.
    (output / "preflight.json").write_text(json.dumps(plan, indent=2) + "\n")
    events = (output / "events.jsonl").open("x")

    def event(kind, **fields):
        record = {"at": utc_now(), "event": kind, **fields}
        events.write(json.dumps(record, allow_nan=False) + "\n")
        events.flush()
        return record["at"]

    mutation_started = False
    phase = "preflight"
    try:
        phase = "prepare"
        event("prepare_requested", baseline=plan["baseline"], candidate=plan["candidate"])
        deployment_clock = time.monotonic()
        mutation_started = True
        adapter.canary_prepare(plan)
        event("both_ready")
        phase = "split"
        event("split_requested", weights=[90, 10])
        traffic = adapter.canary_route(plan, 10)
        summary["split_confirmed_at"] = event("split_confirmed", traffic=traffic)
        start = time.monotonic()
        deadline = start + MEASURE_SECONDS
        phase = "measurement"
        decision = scores.decision()
        for index in range(MAX_REQUESTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            batch = batches[index % len(batches)]
            ids = [row["id"] for row in batch]
            sent_at = utc_now()
            result, request_id = adapter.invoke_batch(
                plan["endpoint"], [row["features"] for row in batch], timeout=min(10, remaining))
            group = group_for_version.get(result.get("model_version"))
            probabilities = result.get("probabilities", [])
            scores.add(group, ids, probabilities)
            summary["requests"][group] += 1
            summary["predictions"][group] += len(ids)
            event("batch", sent_at=sent_at, request_id=request_id, group=group,
                  ids=ids, probabilities=probabilities)
            decision = scores.decision()
            if time.monotonic() >= deadline:
                decision = {**decision, "status": "time_cap"}
                break
            if decision["status"] != "incomplete":
                break
        summary["decision"] = decision
        summary["measurement_seconds"] = time.monotonic() - start
        decision_clock = time.monotonic()
        summary["decision_at"] = event("blind_decision", **decision)
        # Unblind only AFTER the metric-only decision has been saved.
        summary["group_for_version"] = group_for_version
        event("unblind", group_for_version=group_for_version)
        if decision["status"] == "degradation":
            summary["deploy_to_detection_seconds"] = time.monotonic() - deployment_clock
        phase = "rollback"
        rollback_clock = time.monotonic()
        summary["rollback_requested_at"] = event("rollback_requested")
        traffic = adapter.canary_route(plan, 0)
        summary["rollback_config_confirmed_at"] = event("rollback_config_confirmed", traffic=traffic)
        summary["decision_to_rollback_config_seconds"] = time.monotonic() - decision_clock
        phase = "rollback_observation"
        observe_deadline = time.monotonic() + 60
        # Finite observation, not a claim that every future request must be baseline.
        for index in range(50):
            remaining = observe_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Post-rollback observation exceeded one minute")
            row = rows[index % len(rows)]
            result, request_id = adapter.invoke_batch(plan["endpoint"], [row["features"]],
                                                      timeout=min(10, remaining))
            version = result.get("model_version")
            event("rollback_probe", index=index + 1, model_version=version, request_id=request_id)
            if version != plan["baseline_version"]:
                raise RuntimeError("A post-rollback request reached a non-baseline model")
            scores.add(group_for_version[version], [row["id"]], result.get("probabilities", []))
        summary["rollback_observed_at"] = event("rollback_observed", baseline_requests=50)
        summary["decision_to_rollback_observed_seconds"] = time.monotonic() - decision_clock
        summary["rollback_observation_seconds"] = time.monotonic() - rollback_clock
        candidate_group = group_for_version[plan["candidate_version"]]
        summary["candidate_predictions_before_rollback"] = summary["predictions"][candidate_group]
        summary["status"] = (
            "detected_and_rolled_back" if decision["status"] == "degradation"
            and decision["lower_group"] == candidate_group else "inconclusive")
    except (Exception, KeyboardInterrupt) as exc:
        summary.update(status="failed", failed_phase=phase, error_type=type(exc).__name__)
        # Do not dump CLI stderr/headers or arbitrary endpoint error bodies into evidence.
        event("failed", phase=phase, error_type=type(exc).__name__)
    finally:
        if mutation_started:
            try:
                cleanup = adapter.canary_cleanup(plan)
            except (Exception, KeyboardInterrupt) as exc:
                cleanup = {"ok": False, "errors": [type(exc).__name__]}
            summary["cleanup"] = cleanup
            if not cleanup["ok"]:
                summary["status"] = "cleanup_unconfirmed"
        # Preserve partial-attempt identity/counts as evidence, never as a blind decision input.
        summary.setdefault("group_for_version", group_for_version)
        summary.setdefault("decision", scores.decision())
        summary["finished_at"] = utc_now()
        events.close()
        (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Allow one LIVE drill after separate approval")
    parser.add_argument("--baseline-revision", required=True)
    parser.add_argument("--candidate-version", required=True)
    parser.add_argument("--suffix", required=True, help="Fresh cny- suffix, e.g. cny-2026092401")
    parser.add_argument("--data", type=Path, required=True, help="Existing Lab 2 raw sensors.csv")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("No cloud call made: --execute is required for a separately approved live run")
    rows = validation_rows(args.data)  # Validate locally BEFORE contacting the cloud.
    if args.output.exists():
        parser.error("Evidence directory already exists")
    cfg = config.load_deployment()
    adapter = get_adapter(cfg)
    plan = adapter.canary_plan(args.baseline_revision, args.candidate_version, args.suffix)
    summary = run_drill(adapter, plan, rows, args.output)
    print(json.dumps({"status": summary["status"], "evidence": str(args.output),
                      "cleanup": summary.get("cleanup")}, allow_nan=False))
    return 0 if summary["status"] == "detected_and_rolled_back" else 1


if __name__ == "__main__":
    raise SystemExit(main())
