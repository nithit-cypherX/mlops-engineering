"""Task 4 scoring/control-flow tests. No registry, HTTP or cloud calls."""
import json
from unittest.mock import Mock

import pytest

from scripts import canary


@pytest.fixture
def rows():
    return [{"id": i, "label": i % 2, "features": {"test_id": i}} for i in range(20)]


@pytest.fixture
def plan():
    return {"baseline": "old", "candidate": "new", "baseline_version": "1",
            "candidate_version": "2", "endpoint": "fake-endpoint", "models": {
                "1": {"run_id": canary.BASELINE_RUN}, "2": {"run_id": canary.CANDIDATE_RUN}}}


@pytest.fixture
def adapter():
    fake = Mock()
    fake.canary_cleanup.return_value = {"ok": True, "stopped_zero_replicas": True,
                                        "candidate_inactive": True, "errors": []}
    fake.canary_route.side_effect = lambda plan, weight: [{"weight": 100 - weight}]
    count = 0

    def batch(endpoint, features, timeout):
        nonlocal count
        count += 1
        assert endpoint == "fake-endpoint" and 0 < timeout <= 10
        assert all(set(row) == {"test_id"} for row in features)  # No ground-truth labels.
        version = "2" if count == 2 else "1"
        return {"model_version": version, "probabilities": [
            0.5 if version == "2" else (0.9 if r["test_id"] % 2 else 0.1) for r in features
        ]}, f"request-{count}"

    fake.invoke_batch.side_effect = batch
    return fake


def test_blind_decision_needs_identical_complete_unique_cohorts(rows):
    scores = canary.BlindScores(rows)
    ids = [r["id"] for r in rows]
    good = [float(r["label"]) for r in rows]
    scores.add("A", ids, good)
    scores.add("A", ids, good)
    scores.add("B", ids[:-1], [0.5] * 19)
    assert scores.decision() == {"status": "incomplete", "unique_rows": {"A": 20, "B": 19}}
    scores.add("B", ids[-1:], [0.5])
    decision = scores.decision()
    assert decision["status"] == "degradation" and decision["lower_group"] == "B"
    assert decision["roc_auc"] == {"A": 1.0, "B": 0.5}


def test_no_alarm_for_same_scores(rows):
    scores = canary.BlindScores(rows)
    for group in ("A", "B"):
        scores.add(group, [r["id"] for r in rows], [0.5] * 20)
    assert scores.decision()["status"] == "no_alarm"


@pytest.mark.parametrize("ids,probs", [([0], []), ([0], [float("nan")]), ([0], [1.1]),
                                       ([0], [True]), ([100], [0.5]), ([0, 0], [0.5, 0.5])])
def test_invalid_predictions_rejected(rows, ids, probs):
    with pytest.raises(ValueError):
        canary.BlindScores(rows).add("A", ids, probs)


def test_duplicate_score_disagreement_rejected(rows):
    scores = canary.BlindScores(rows)
    scores.add("A", [0], [0.1])
    with pytest.raises(ValueError, match="different predictions"):
        scores.add("A", [0], [0.2])


def test_complete_drill_saves_blind_decision_then_unmasks_and_cleans(rows, plan, adapter, tmp_path):
    output = tmp_path / "run"
    summary = canary.run_drill(adapter, plan, rows, output)
    assert summary["status"] == "detected_and_rolled_back"
    assert summary["cleanup"]["ok"] and summary["candidate_predictions_before_rollback"] == 20
    assert adapter.invoke_batch.call_count == 52
    assert [c.args[1] for c in adapter.canary_route.call_args_list] == [10, 0]
    adapter.canary_cleanup.assert_called_once_with(plan)
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    names = [e["event"] for e in events]
    assert names.index("blind_decision") < names.index("unblind") < names.index("rollback_requested")
    assert len([e for e in events if e["event"] == "rollback_probe"]) == 50
    assert json.loads((output / "summary.json").read_text())["status"] == summary["status"]


def test_no_overwrite_or_wrong_model_mutation(rows, plan, adapter, tmp_path):
    with pytest.raises(FileExistsError):
        canary.run_drill(adapter, plan, rows, tmp_path)
    plan["models"]["2"]["run_id"] = "unreviewed"
    with pytest.raises(ValueError, match="reviewed"):
        canary.run_drill(adapter, plan, rows, tmp_path / "run")
    adapter.canary_prepare.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_partial_prepare_still_cleans(rows, plan, adapter, tmp_path, failure):
    adapter.canary_prepare.side_effect = failure("private error body")
    summary = canary.run_drill(adapter, plan, rows, tmp_path / "run")
    assert summary["status"] == "failed" and summary["failed_phase"] == "prepare"
    adapter.canary_cleanup.assert_called_once()
    assert "private error body" not in (tmp_path / "run" / "events.jsonl").read_text()


def test_request_cap_is_inconclusive_not_a_pass(rows, plan, adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "MAX_REQUESTS", 1)
    # The first request is baseline; rollback requests must all be baseline as well.
    adapter.invoke_batch.side_effect = lambda ep, features, timeout: ({"model_version": "1",
        "probabilities": [0.9 if r["test_id"] % 2 else 0.1 for r in features]}, "id")
    summary = canary.run_drill(adapter, plan, rows, tmp_path / "run")
    assert summary["status"] == "inconclusive"
    assert summary["decision"]["status"] == "incomplete"
    assert sum(summary["requests"].values()) == 1
    adapter.canary_cleanup.assert_called_once()


def test_cleanup_failure_overrides_success(rows, plan, adapter, tmp_path):
    adapter.canary_cleanup.return_value = {"ok": False, "errors": ["stop:TimeoutError"]}
    assert canary.run_drill(adapter, plan, rows, tmp_path / "run")["status"] == "cleanup_unconfirmed"


def test_bad_post_rollback_response_is_failure(rows, plan, adapter, tmp_path):
    original = adapter.invoke_batch.side_effect
    count = 0

    def batch(*args, **kwargs):
        nonlocal count
        count += 1
        result, request_id = original(*args, **kwargs)
        if count == 3:
            result["model_version"] = "2"
        return result, request_id

    adapter.invoke_batch.side_effect = batch
    summary = canary.run_drill(adapter, plan, rows, tmp_path / "run")
    assert summary["status"] == "failed" and summary["failed_phase"] == "rollback_observation"


def test_cli_requires_execute_before_reading_config_or_network(monkeypatch):
    factory = Mock(side_effect=AssertionError("No adapter allowed"))
    monkeypatch.setattr(canary, "get_adapter", factory)
    monkeypatch.setattr(canary.config, "load_deployment", factory)
    monkeypatch.setattr(canary, "validation_rows", factory)
    with pytest.raises(SystemExit) as error:
        canary.main(["--baseline-revision", "old", "--candidate-version", "2",
                     "--suffix", "cny-123456", "--data", "unused", "--output", "unused"])
    assert error.value.code == 2
    factory.assert_not_called()


def test_changed_dataset_is_rejected_before_split(tmp_path):
    path = tmp_path / "sensors.csv"
    path.write_text("not the reviewed data")
    with pytest.raises(ValueError, match="reviewed Lab 2"):
        canary.validation_rows(path)


def test_time_cap_prevents_further_measurement(rows, plan, adapter, tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(canary.time, "monotonic", lambda: clock[0])
    count = 0

    def batch(ep, features, timeout):
        nonlocal count
        count += 1
        if count == 1:
            clock[0] += canary.MEASURE_SECONDS
        return {"model_version": "1", "probabilities": [
            0.9 if r["test_id"] % 2 else 0.1 for r in features]}, "id"

    adapter.invoke_batch.side_effect = batch
    summary = canary.run_drill(adapter, plan, rows, tmp_path / "run")
    assert summary["status"] == "inconclusive" and summary["decision"]["status"] == "time_cap"
    assert sum(summary["requests"].values()) == 1
    assert count == 51  # One measurement plus the separate rollback observation window.


def test_metric_decision_does_not_assume_candidate_is_worse(rows, plan, adapter, tmp_path):
    count = 0

    def batch(ep, features, timeout):
        nonlocal count
        count += 1
        version = "2" if count == 2 else "1"
        probabilities = [float(r["test_id"] % 2) if version == "2" else 0.5 for r in features]
        return {"model_version": version, "probabilities": probabilities}, "id"

    adapter.invoke_batch.side_effect = batch
    summary = canary.run_drill(adapter, plan, rows, tmp_path / "run")
    assert summary["decision"]["status"] == "degradation"
    assert summary["decision"]["lower_group"] == summary["group_for_version"]["1"]
    assert summary["status"] == "inconclusive"  # Do not silently promote the candidate.
    assert summary["cleanup"]["ok"]
