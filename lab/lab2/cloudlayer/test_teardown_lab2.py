"""Offline teardown checks. No credentials or real Azure clients are used."""
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from azure.ai.ml.constants import ListViewType
from azure.ai.ml.entities import AmlCompute
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from cloudlayer import teardown_lab2 as cleanup


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    cfg = SimpleNamespace(**cleanup.APPROVED_SCOPE)
    compute = AmlCompute(
        name="cpu-lab2", id=cleanup.COMPUTE_ID,
        tags=dict(cleanup.EXPECTED_TAGS), provisioning_state="Succeeded",
    )
    client = Mock()
    client.compute.get.side_effect = [compute, ResourceNotFoundError("not found")]
    client.jobs.list.return_value = [
        SimpleNamespace(name=f"job-{i}", status=status)
        for i, status in enumerate(["Completed", "Failed", "Failed", "Completed"])
    ]
    client.compute.begin_delete.return_value.done.return_value = True
    factory = Mock(return_value=client)
    credential = Mock()
    monkeypatch.setattr(cleanup, "MLClient", factory)
    monkeypatch.setattr(cleanup, "DefaultAzureCredential", credential)
    return SimpleNamespace(cfg=cfg, compute=compute, client=client,
                           factory=factory, credential=credential)


def test_deletes_only_compute_and_confirms_absence(setup):
    assert cleanup.teardown_compute(setup.cfg) == [cleanup.COMPUTE_ID]
    setup.factory.assert_called_once_with(
        setup.credential.return_value, setup.cfg.azure_subscription_id,
        setup.cfg.project_id, setup.cfg.azure_ml_workspace,
    )
    assert setup.client.mock_calls == [
        call.compute.get("cpu-lab2"),
        call.jobs.list(list_view_type=ListViewType.ALL),
        call.compute.begin_delete("cpu-lab2", action="Delete"),
        call.compute.begin_delete().result(timeout=600),
        call.compute.begin_delete().done(),
        call.compute.get("cpu-lab2"),
    ]  # No job, model, artifact, workspace, Storage or ACR write.


@pytest.mark.parametrize("field", list(cleanup.APPROVED_SCOPE))
def test_changed_scope_stops_before_credentials_or_client(setup, field):
    setattr(setup.cfg, field, "different")
    with pytest.raises(ValueError, match="reviewed"):
        cleanup.teardown_compute(setup.cfg)
    setup.factory.assert_not_called()
    setup.credential.assert_not_called()


@pytest.mark.parametrize("fault", ["type", "name", "id", "tags", "course", "student", "lab", "state"])
def test_wrong_resource_metadata_cannot_be_deleted(setup, fault):
    values = dict(name="cpu-lab2", id=cleanup.COMPUTE_ID,
                  tags=dict(cleanup.EXPECTED_TAGS), provisioning_state="Succeeded")
    if fault in {"course", "student", "lab"}:
        values["tags"][fault] = "wrong"
    elif fault == "tags":
        values["tags"] = None
    elif fault == "state":
        values["provisioning_state"] = "Updating"
    elif fault != "type":
        values[fault] = "wrong"
    compute = SimpleNamespace(**values) if fault == "type" else AmlCompute(**values)
    setup.client.compute.get.side_effect = [compute]
    with pytest.raises(ValueError, match="did not match"):
        cleanup.teardown_compute(setup.cfg)
    setup.client.jobs.list.assert_not_called()
    setup.client.compute.begin_delete.assert_not_called()


@pytest.mark.parametrize("status", ["Running", "Queued", "Preparing", "NotStarted", "CancelRequested",
                                   "Finalizing", "Unknown", None])
def test_unfinished_or_unknown_job_blocks_deletion_regardless_of_tags(setup, status):
    setup.client.jobs.list.return_value.append(
        SimpleNamespace(name="other-job", status=status, tags={"lab": "3"}),
    )
    with pytest.raises(RuntimeError, match="nothing deleted"):
        cleanup.teardown_compute(setup.cfg)
    setup.client.compute.begin_delete.assert_not_called()


def test_absent_compute_does_not_delete_or_inspect_jobs(setup):
    setup.client.compute.get.side_effect = ResourceNotFoundError("not found")
    assert cleanup.teardown_compute(setup.cfg) == []
    assert setup.client.mock_calls == [call.compute.get("cpu-lab2")]


@pytest.mark.parametrize("phase", ["get", "jobs", "delete", "poll", "readback"])
def test_service_failure_propagates_without_retry_or_success(setup, phase):
    error = HttpResponseError("service unavailable")
    if phase == "get":
        setup.client.compute.get.side_effect = error
    elif phase == "jobs":
        def pages():
            yield SimpleNamespace(name="finished", status="Completed")
            raise error  # A later page fails: do not act on a partial inventory.
        setup.client.jobs.list.return_value = pages()
    elif phase == "delete":
        setup.client.compute.begin_delete.side_effect = error
    elif phase == "poll":
        setup.client.compute.begin_delete.return_value.result.side_effect = error
    else:
        setup.client.compute.get.side_effect = [setup.compute, error]
    with pytest.raises(HttpResponseError, match="service unavailable"):
        cleanup.teardown_compute(setup.cfg)
    assert setup.client.compute.begin_delete.call_count == (0 if phase in {"get", "jobs"} else 1)


def test_pending_poller_is_not_reported_as_deleted(setup):
    setup.client.compute.begin_delete.return_value.done.return_value = False
    with pytest.raises(TimeoutError, match="not confirmed"):
        cleanup.teardown_compute(setup.cfg)
    assert setup.client.compute.get.call_count == 1


def test_resource_remaining_after_poller_is_not_reported_as_deleted(setup):
    setup.client.compute.get.side_effect = [setup.compute, setup.compute]
    with pytest.raises(RuntimeError, match="still exists"):
        cleanup.teardown_compute(setup.cfg)
    setup.client.compute.begin_delete.assert_called_once()


@pytest.mark.parametrize("absent", [False, True])
def test_command_reports_result_and_preserved_resources(setup, monkeypatch, capsys, absent):
    monkeypatch.setattr(sys, "argv", ["teardown_lab2"])
    monkeypatch.setattr(cleanup.config, "load", Mock(return_value=setup.cfg))
    if absent:
        setup.client.compute.get.side_effect = ResourceNotFoundError("not found")
    cleanup.main()
    output = capsys.readouterr().out
    assert ("nothing deleted" if absent else "Deleted and confirmed absent") in output
    assert "Jobs, workspace, models, artifacts, Storage and ACR are retained" in output
    assert "may still incur costs" in output


def test_help_never_reads_config_or_creates_client(setup, monkeypatch):
    load = Mock()
    monkeypatch.setattr(cleanup.config, "load", load)
    monkeypatch.setattr(sys, "argv", ["teardown_lab2", "--help"])
    with pytest.raises(SystemExit) as result:
        cleanup.main()
    assert result.value.code == 0
    load.assert_not_called()
    setup.factory.assert_not_called()
