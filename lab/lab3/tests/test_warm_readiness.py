"""Offline regression cases for the warm diagnostic's startup gate."""
import copy
from unittest.mock import Mock

import pytest

from scripts import warm_readiness as gate


@pytest.fixture
def ready():
    return {
        "configuration_unchanged": True,
        "state": {"runningStatus": "Running", "provisioningState": "Succeeded",
                  "latestRevisionName": "revision-3", "latestReadyRevisionName": "revision-3"},
        "replicas": [
            {"name": "revision-2", "active": False, "replicas": 0},
            {"name": "revision-3", "active": True, "replicas": 1,
             "replica_details": [{"name": "replica-a", "properties": {
                 "runningState": "Running", "containers": [
                     {"ready": True, "runningState": "Running", "restartCount": 0}
                 ]}}]},
        ],
    }


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(gate.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gate.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_waits_for_summary_and_live_replica_before_passing(ready, clock):
    delayed = copy.deepcopy(ready)
    delayed["replicas"][1]["replicas"] = 0
    retained = copy.deepcopy(ready)
    retained["replicas"][1]["replica_details"][0]["properties"]["runningState"] = "NotRunning"
    reader = Mock(side_effect=[delayed, retained, ready])
    saved = []
    snapshot, name = gate.wait_for_warm(reader, saved.append, deadline=7)
    assert snapshot == ready and name == "replica-a"
    assert saved == [delayed, retained, ready] and clock[0] == 6
    assert [call.args for call in reader.call_args_list] == [(7,), (7,), (7,)]
    ready["state"]["runningStatus"] = "Stopped"
    assert saved[-1]["state"]["runningStatus"] == "Running"


@pytest.mark.parametrize("path,value", [
    (("state", "runningStatus"), "Stopped"),
    (("state", "provisioningState"), "Updating"),
    (("state", "latestReadyRevisionName"), "revision-2"),
    (("replicas", 1, "name"), "other-revision"),
    (("replicas", 1, "active"), False),
    (("replicas", 0, "active"), True),
    (("replicas", 0, "replicas"), 1),
    (("replicas", 1, "active"), None),
    (("replicas", 1, "replicas"), 0),
    (("replicas", 1, "replicas"), 2),
    (("replicas", 1, "replicas"), True),
    (("replicas", 1, "replica_details"), []),
    (("replicas", 1, "replica_details", 0, "name"), ""),
    (("replicas", 1, "replica_details", 0, "properties", "runningState"), "NotRunning"),
    (("replicas", 1, "replica_details", 0, "properties", "runningState"), None),
    (("replicas", 1, "replica_details", 0, "properties", "containers", 0, "ready"), False),
    (("replicas", 1, "replica_details", 0, "properties", "containers", 0, "runningState"), "Terminated"),
    (("replicas", 1, "replica_details", 0, "properties", "containers", 0, "restartCount"), 1),
])
def test_invalid_readiness_never_passes(ready, path, value):
    target = ready
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert gate.warm_replica(ready) is None


def test_two_replica_records_do_not_pass(ready):
    details = ready["replicas"][1]["replica_details"]
    details.append(copy.deepcopy(details[0]))
    assert gate.warm_replica(ready) is None


def test_timeout_saves_each_snapshot_without_extending_deadline(ready, clock):
    ready["replicas"][1]["replicas"] = 0
    reader = Mock(return_value=ready)
    saved = []
    with pytest.raises(TimeoutError, match="readiness deadline"):
        gate.wait_for_warm(reader, saved.append, deadline=7)
    assert len(saved) == reader.call_count == 3 and clock[0] == 7


@pytest.mark.parametrize("change", ["configuration", "provisioning"])
def test_records_before_failing_on_drift_or_terminal_state(ready, clock, change):
    if change == "configuration":
        ready["configuration_unchanged"] = False
    else:
        ready["state"]["provisioningState"] = "Failed"
    reader = Mock(return_value=ready)
    saved = []
    with pytest.raises(RuntimeError):
        gate.wait_for_warm(reader, saved.append, deadline=7)
    assert saved == [ready] and reader.call_count == 1 and clock[0] == 0


def test_expired_deadline_does_not_read(clock):
    reader = Mock()
    with pytest.raises(TimeoutError):
        gate.wait_for_warm(reader, Mock(), deadline=0)
    reader.assert_not_called()


def test_late_ready_result_is_logged_but_not_accepted(ready, clock):
    def read(deadline):
        clock[0] = deadline
        return ready
    saved = []
    with pytest.raises(TimeoutError):
        gate.wait_for_warm(read, saved.append, deadline=7)
    assert saved == [ready] and clock[0] == 7


def test_read_error_is_logged_and_not_retried(clock):
    reader = Mock(side_effect=TimeoutError("read exceeded its remaining budget"))
    saved = []
    with pytest.raises(TimeoutError, match="remaining budget"):
        gate.wait_for_warm(reader, saved.append, deadline=7)
    assert reader.call_count == 1
    assert saved == [{"read_error": "TimeoutError: read exceeded its remaining budget"}]


def test_cannot_pass_if_evidence_cannot_be_saved(ready, clock):
    reader = Mock(return_value=ready)
    with pytest.raises(OSError, match="log unavailable"):
        gate.wait_for_warm(reader, Mock(side_effect=OSError("log unavailable")), deadline=7)
    assert reader.call_count == 1
