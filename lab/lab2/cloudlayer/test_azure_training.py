"""Use real SDK job definitions, but never contact Azure or start training."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import shlex
import socket
import sys

import pytest
from azure.ai.ml.entities import AmlCompute, AzureBlobDatastore, AzureFileDatastore
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from cloudlayer import azure
from src.config import Config


PARAMS = {"n_estimators": 200, "max_depth": 8, "min_samples_leaf": 5, "seed": 20260101}
IMAGE = "example.azurecr.io/lab2@sha256:" + "a" * 64
STUDY = {"mode": "tune", "trials": 12, "budget_thb": 20.0, "timeout_s": 10800,
         "trial_estimate_s": 600.0, "seed": 20260101}  # Test inputs, not a spending approval.


@pytest.fixture
def setup(monkeypatch):
    # A test must fail, not use the developer's real login, if a mock is bypassed.
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    credential = Mock()
    monkeypatch.setattr(azure, "DefaultAzureCredential", Mock(return_value=credential))
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(azure, "MLClient", factory)
    client.datastores.get_default.return_value = AzureBlobDatastore(
        name="workspaceblobstore", account_name="example",
        container_name="private-container",
    )
    client.jobs.create_or_update.side_effect = lambda job: job
    client.compute.get.return_value = AmlCompute(
        name="cpu-lab2", size="STANDARD_F2s_v2", tier="Dedicated", location="malaysiawest",
        min_instances=0, max_instances=1,
    )
    monkeypatch.setattr(azure, "uuid4", lambda: SimpleNamespace(hex="1" * 32))
    monkeypatch.setattr(azure.time, "sleep", Mock())
    cfg = Config(
        provider="azure", project_id="course-resource-group", region="test-region",
        blob_uri="https://example.blob.core.windows.net/private-container/lab2",
        container_registry="example.azurecr.io/lab2",
        mlflow_tracking_uri="azureml://example-tracking", model_registry_name="course-model",
        identity_ref="22222222-2222-2222-2222-222222222222",
        azure_subscription_id="subscription-id", azure_ml_workspace="course-workspace",
        azure_ml_compute="cpu-lab2",
    )
    return azure.AzureAdapter(cfg), client, factory, credential


def test_submit_builds_cloud_only_job(setup):
    adapter, client, factory, credential = setup
    params = PARAMS.copy()
    name = adapter.submit_training(IMAGE, params)
    job = client.jobs.create_or_update.call_args.args[0]
    assert name == "lab2-" + "1" * 32
    factory.assert_called_once_with(
        credential, "subscription-id", "course-resource-group", "course-workspace",
    )
    assert params == PARAMS
    assert job.environment.image == IMAGE
    assert job.environment.conda_file is None
    assert job.code is None  # No laptop source folder is uploaded.
    assert job.compute == "cpu-lab2"
    assert job.resources.instance_count == 1
    assert job.limits.timeout == 1800
    assert job.identity.type == "managed_identity"
    assert job.identity.object_id == adapter.cfg.identity_ref
    assert job.tags == adapter.cfg.tags(2)
    assert job.environment_variables == {
        "PYTHONHASHSEED": "20260101", "MLFLOW_TRACKING_URI": adapter.cfg.mlflow_tracking_uri,
    }
    root = "azureml://datastores/workspaceblobstore/paths/lab2"
    assert job.inputs["raw"].path == root + "/data/sensors.csv"
    assert job.inputs["dvc"].path == root + "/data/raw.dvc"
    for item in job.inputs.values():
        assert item.type == "uri_file"
        assert item.mode == "download"
    assert job.outputs["artifacts"].path == root + "/runs/" + name
    assert job.outputs["artifacts"].type == "uri_folder"
    assert job.outputs["artifacts"].mode == "upload"
    assert "--raw-path '${{inputs.raw}}'" in job.command
    assert "--dvc-metadata-path '${{inputs.dvc}}'" in job.command
    assert "--output-dir '${{outputs.artifacts}}'" in job.command
    for key, value in PARAMS.items():
        assert f"--{key.replace('_', '-')} {value}" in job.command
    job._validate(raise_error=True)  # SDK schema validation is offline.
    assert job._to_rest_object()  # Exercise the installed SDK's serialization too.
    client.jobs.create_or_update.assert_called_once()
    client.compute.get.assert_not_called()  # Original Task 1 path is unchanged.


def test_study_submits_one_cloud_job_with_the_actual_compute_and_cli_contract(setup, monkeypatch):
    from src import tune

    adapter, client, _, _ = setup
    params = STUDY.copy()
    name = adapter.submit_training(IMAGE, params)
    job = client.jobs.create_or_update.call_args.args[0]
    assert params == STUDY
    client.compute.get.assert_called_once_with("cpu-lab2")
    client.jobs.create_or_update.assert_called_once()
    assert name == job.name
    assert job.code is None
    assert job.environment.image == IMAGE
    assert job.environment.conda_file is None
    assert job.compute == "cpu-lab2"
    assert job.resources.instance_count == 1
    assert job.limits.timeout == 10800
    assert job.identity.object_id == adapter.cfg.identity_ref
    checkpoint_uri = adapter.cfg.blob_uri + f"/studies/{name}/checkpoint.json"
    assert job.tags == {**adapter.cfg.tags(2), "checkpoint_uri": checkpoint_uri, "study_image": IMAGE}
    assert job.environment_variables == {
        "CLOUD_PROVIDER": "azure", "PYTHONHASHSEED": "20260101",
        "MLFLOW_TRACKING_URI": adapter.cfg.mlflow_tracking_uri,
        "BLOB_URI": adapter.cfg.blob_uri,
    }
    root = "azureml://datastores/workspaceblobstore/paths/lab2"
    assert job.inputs["raw"].path == root + "/data/sensors.csv"
    assert job.inputs["dvc"].path == root + "/data/raw.dvc"
    assert all(item.type == "uri_file" and item.mode == "download"
               for item in job.inputs.values())
    assert job.outputs["artifacts"].path == root + "/runs/" + name
    assert job.outputs["artifacts"].mode == "upload"  # Not a durable cloud resume claim.
    assert job.outputs["artifacts"].type == "uri_folder"
    tokens = shlex.split(job.command)
    assert tokens[:3] == ["python", "-m", "src.tune"]
    monkeypatch.setattr(sys, "argv", tokens[2:])
    parsed = tune.parse_args()  # Actual tuner must accept every generated flag.
    assert parsed.trials == 12
    assert parsed.budget_thb == 20.0
    assert parsed.trial_estimate_s == 600.0
    assert parsed.seed == 20260101
    assert parsed.instance == "Standard_F2s_v2"
    assert parsed.experiment == "itcs355-lab2"
    assert str(parsed.raw_path) == "${{inputs.raw}}"
    assert str(parsed.dvc_metadata_path) == "${{inputs.dvc}}"
    assert str(parsed.checkpoint) == "${{outputs.artifacts}}/tune_checkpoint.json"
    assert parsed.checkpoint_key == f"studies/{name}/checkpoint.json"
    assert parsed.image_uri == IMAGE
    assert parsed.resume_uri is None
    assert parsed.test_interruption is False
    job._validate(raise_error=True)
    assert job._to_rest_object()


@pytest.mark.parametrize("enabled", [False, True])
def test_study_interruption_flag_reaches_the_actual_tuner_parser(setup, monkeypatch, enabled):
    from src import tune

    adapter, client, _, _ = setup
    adapter.submit_training(IMAGE, {**STUDY, "test_interruption": enabled})
    job = client.jobs.create_or_update.call_args.args[0]
    tokens = shlex.split(job.command)
    monkeypatch.setattr(sys, "argv", tokens[2:])
    parsed = tune.parse_args()
    assert parsed.test_interruption is enabled
    assert ("--test-interruption" in tokens) is enabled
    assert parsed.resume_uri is None
    assert parsed.trials == 12
    job._validate(raise_error=True)
    assert job._to_rest_object()
    client.jobs.create_or_update.assert_called_once()


@pytest.mark.parametrize("overrides", [
    {"test_interruption": "true"}, {"test_interruption": 1}, {"test_interruption": None},
    {"test_interruption": True, "trials": 4},
    {"test_interruption": True, "resume_from": "lab2-" + "2" * 32},
])
def test_invalid_interruption_request_does_not_contact_azure(setup, overrides):
    adapter, _, factory, _ = setup
    with pytest.raises(ValueError, match="test_interruption"):
        adapter.submit_training(IMAGE, {**STUDY, **overrides})
    factory.assert_not_called()


def test_task_one_rejects_the_study_only_interruption_switch(setup):
    adapter, _, factory, _ = setup
    with pytest.raises(ValueError, match="exactly"):
        adapter.submit_training(IMAGE, {**PARAMS, "test_interruption": True})
    factory.assert_not_called()


@pytest.mark.parametrize("status", ["Completed", "Failed", "Canceled"])
def test_resume_reads_a_stopped_study_but_writes_a_new_checkpoint(setup, monkeypatch, status):
    from src import tune

    adapter, client, _, _ = setup
    old_id = "lab2-" + "2" * 32
    source = adapter.cfg.blob_uri + f"/studies/{old_id}/checkpoint.json"
    client.jobs.get.return_value = SimpleNamespace(
        name=old_id, status=status,
        tags={**adapter.cfg.tags(2), "checkpoint_uri": source, "study_image": IMAGE},
    )
    name = adapter.submit_training(IMAGE, {**STUDY, "resume_from": old_id})
    job = client.jobs.create_or_update.call_args.args[0]
    monkeypatch.setattr(sys, "argv", shlex.split(job.command)[2:])
    args = tune.parse_args()
    assert args.resume_uri == source
    assert args.test_interruption is False
    assert args.checkpoint_key == f"studies/{name}/checkpoint.json"
    assert name != old_id
    assert job.tags["checkpoint_uri"] != source
    assert job.tags["resume_from"] == old_id
    client.jobs.get.assert_called_once_with(old_id)
    client.jobs.create_or_update.assert_called_once()


@pytest.mark.parametrize("status", ["Running", "Queued", "Finalizing", "CancelRequested"])
def test_resume_waits_for_the_source_to_stop(setup, status):
    adapter, client, _, _ = setup
    client.jobs.get.return_value = SimpleNamespace(status=status)
    with pytest.raises(ValueError, match="must have stopped"):
        adapter.submit_training(IMAGE, {**STUDY, "resume_from": "lab2-" + "2" * 32})
    client.jobs.create_or_update.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("study_image", "other-image"), ("checkpoint_uri", "https://other.invalid/checkpoint.json"),
    ("lab", "1"), ("student", "other-student"),
])
def test_resume_rejects_wrong_image_or_checkpoint_ownership(setup, field, value):
    adapter, client, _, _ = setup
    old_id = "lab2-" + "2" * 32
    tags = {**adapter.cfg.tags(2), "study_image": IMAGE,
            "checkpoint_uri": adapter.cfg.blob_uri + f"/studies/{old_id}/checkpoint.json"}
    tags[field] = value
    client.jobs.get.return_value = SimpleNamespace(name=old_id, status="Failed", tags=tags)
    with pytest.raises(ValueError, match="this lab's checkpointed study"):
        adapter.submit_training(IMAGE, {**STUDY, "resume_from": old_id})
    client.jobs.create_or_update.assert_not_called()


@pytest.mark.parametrize("job_id", ["", "../other", "lab2-x; echo unsafe", 12])
def test_invalid_resume_source_stops_before_azure(setup, job_id):
    adapter, _, factory, _ = setup
    with pytest.raises(ValueError, match="resume_from"):
        adapter.submit_training(IMAGE, {**STUDY, "resume_from": job_id})
    factory.assert_not_called()


@pytest.mark.parametrize("overrides", [
    {"mode": "other"}, {"budget_thb": 0}, {"budget_thb": -1}, {"budget_thb": 151},
    {"budget_thb": float("nan")}, {"budget_thb": float("inf")}, {"budget_thb": True},
    {"budget_thb": "20; echo unsafe"}, {"trials": 13}, {"trials": 0}, {"trials": True},
    {"timeout_s": 0}, {"timeout_s": 599}, {"timeout_s": 1000.5}, {"timeout_s": True},
    {"trial_estimate_s": 0}, {"trial_estimate_s": float("inf")},
    {"seed": -1}, {"seed": 2**32}, {"instance": "local"},
])
def test_invalid_study_args_do_not_contact_azure(setup, overrides):
    adapter, _, factory, _ = setup
    with pytest.raises(ValueError):
        adapter.submit_training(IMAGE, {**STUDY, **overrides})
    factory.assert_not_called()


@pytest.mark.parametrize("missing", ["budget_thb", "timeout_s", "seed"])
def test_study_has_no_implicit_spending_allocation_or_timeout(setup, missing):
    adapter, _, factory, _ = setup
    with pytest.raises(ValueError, match="exactly"):
        adapter.submit_training(IMAGE, {k: v for k, v in STUDY.items() if k != missing})
    factory.assert_not_called()


@pytest.mark.parametrize("overrides", [
    {"size": "Standard_DS3_v2"}, {"size": "unknown"},
    {"location": "eastus"}, {"tier": "LowPriority"},
])
def test_unverified_compute_profile_does_not_submit(setup, overrides):
    adapter, client, _, _ = setup
    client.compute.get.return_value = AmlCompute(**{
        "name": "cpu-lab2", "size": "Standard_F2s_v2", "location": "malaysiawest",
        "tier": "Dedicated", **overrides,
    })
    with pytest.raises(ValueError, match="verified Dedicated"):
        adapter.submit_training(IMAGE, STUDY)
    client.jobs.create_or_update.assert_not_called()


def test_timeout_compute_estimate_must_fit_the_study_allocation(setup):
    adapter, client, _, _ = setup
    # Three hours at the verified 2.900677 THB/hour costs more than 8 THB.
    with pytest.raises(ValueError, match="at timeout exceeds budget_thb"):
        adapter.submit_training(IMAGE, {**STUDY, "budget_thb": 8.0})
    client.jobs.create_or_update.assert_not_called()


def test_compute_lookup_error_is_not_hidden_or_retried(setup):
    adapter, client, _, _ = setup
    error = ResourceNotFoundError(message="Compute missing")
    client.compute.get.side_effect = error
    with pytest.raises(ResourceNotFoundError) as caught:
        adapter.submit_training(IMAGE, STUDY)
    assert caught.value is error
    client.compute.get.assert_called_once()
    client.jobs.create_or_update.assert_not_called()


def test_wait_allows_a_study_to_run_longer_than_one_hour(setup, monkeypatch):
    adapter, client, _, _ = setup
    client.jobs.get.side_effect = [
        SimpleNamespace(status="Running", limits=SimpleNamespace(timeout=10800)),
        SimpleNamespace(name="study", status="Completed", outputs={}),
    ]
    monkeypatch.setattr(azure.time, "monotonic", Mock(side_effect=[0, 7200]))
    assert adapter.wait_training("study")["status"] == "Completed"
    azure.time.sleep.assert_called_once_with(10)


def test_study_client_wait_remains_bounded_without_cancelling(setup, monkeypatch):
    adapter, client, _, _ = setup
    client.jobs.get.return_value = SimpleNamespace(
        status="Queued", limits=SimpleNamespace(timeout=10800),
    )
    monkeypatch.setattr(azure.time, "monotonic", Mock(side_effect=[0, 12600]))
    with pytest.raises(TimeoutError, match="may still be running"):
        adapter.wait_training("study")
    client.jobs.cancel.assert_not_called()
    client.jobs.create_or_update.assert_not_called()


@pytest.mark.parametrize("image", [
    "example.azurecr.io/lab2:latest", "example.azurecr.io/lab2@sha256:short",
    "other.azurecr.io/lab2@sha256:" + "a" * 64,
])
def test_invalid_image_does_not_contact_azure(setup, image):
    adapter, _, factory, _ = setup
    with pytest.raises(ValueError, match="digest-pinned"):
        adapter.submit_training(image, PARAMS)
    factory.assert_not_called()


@pytest.mark.parametrize("params", [
    {}, {**PARAMS, "command": "other-command"},
    {**PARAMS, "n_estimators": 0}, {**PARAMS, "max_depth": -1},
    {**PARAMS, "min_samples_leaf": True}, {**PARAMS, "max_depth": "8; echo unsafe"},
    {**PARAMS, "seed": -1}, {**PARAMS, "seed": 2**32},
])
def test_invalid_args_do_not_contact_azure(setup, params):
    adapter, _, factory, _ = setup
    with pytest.raises(ValueError):
        adapter.submit_training(IMAGE, params)
    factory.assert_not_called()


@pytest.mark.parametrize("uri", [
    "/local/data", "https://example.blob.core.windows.net/private-container",
    "https://example.blob.core.windows.net/private-container/lab2?sig=not-a-real-token",
    "https://example.blob.core.windows.net/private-container/../lab1",
])
def test_invalid_blob_path_does_not_contact_azure(setup, uri):
    adapter, _, factory, _ = setup
    adapter.cfg = replace(adapter.cfg, blob_uri=uri)
    with pytest.raises(ValueError, match="BLOB_URI"):
        adapter.submit_training(IMAGE, PARAMS)
    factory.assert_not_called()


@pytest.mark.parametrize("datastore", [
    AzureFileDatastore(
        name="workspacefiles", account_name="example", file_share_name="private-share",
    ),
    AzureBlobDatastore(
        name="workspaceblobstore", account_name="other", container_name="private-container",
    ),
    AzureBlobDatastore(
        name="workspaceblobstore", account_name="example", container_name="other-container",
    ),
], ids=["azure-file", "wrong-account", "wrong-container"])
def test_wrong_datastore_stops_submission(setup, datastore):
    adapter, client, _, _ = setup
    client.datastores.get_default.return_value = datastore
    with pytest.raises(ValueError, match="default Blob datastore"):
        adapter.submit_training(IMAGE, PARAMS)
    client.jobs.create_or_update.assert_not_called()


def test_submit_permission_error_is_not_hidden_or_retried(setup):
    adapter, client, _, _ = setup
    error = HttpResponseError(message="AuthorizationFailed")
    client.jobs.create_or_update.side_effect = error
    with pytest.raises(HttpResponseError) as caught:
        adapter.submit_training(IMAGE, PARAMS)
    assert caught.value is error
    client.jobs.create_or_update.assert_called_once()


def test_wait_tracks_progress_until_completed(setup):
    adapter, client, factory, _ = setup
    output = "azureml://datastores/workspaceblobstore/paths/lab2/runs/test-job"
    client.jobs.get.side_effect = [
        SimpleNamespace(status=status) for status in ("Queued", "Running", "Finalizing")
    ] + [SimpleNamespace(name="test-job", status="Completed",
                         outputs={"artifacts": SimpleNamespace(path=output)})]
    assert adapter.wait_training("test-job") == {
        "job_id": "test-job", "status": "Completed", "outputs": {"artifacts": output},
    }
    assert client.jobs.get.call_count == 4
    assert azure.time.sleep.call_count == 3
    factory.assert_called_once()
    client.jobs.create_or_update.assert_not_called()


@pytest.mark.parametrize("status", ["Failed", "Canceled"])
def test_wait_does_not_report_failure_or_cancellation_as_success(setup, status):
    adapter, client, _, _ = setup
    client.jobs.get.return_value = SimpleNamespace(status=status)
    with pytest.raises(RuntimeError, match=status):
        adapter.wait_training("test-job")
    azure.time.sleep.assert_not_called()


def test_wait_timeout_does_not_claim_cancellation_or_resubmit(setup, monkeypatch):
    adapter, client, _, _ = setup
    client.jobs.get.return_value = SimpleNamespace(status="Queued")
    monkeypatch.setattr(azure.time, "monotonic", Mock(side_effect=[0, 3600]))
    with pytest.raises(TimeoutError, match="may still be running"):
        adapter.wait_training("test-job")
    client.jobs.cancel.assert_not_called()
    client.jobs.create_or_update.assert_not_called()
    azure.time.sleep.assert_not_called()


@pytest.mark.parametrize("error", [
    ResourceNotFoundError(message="Job not found"), HttpResponseError(message="Forbidden"),
])
def test_wait_preserves_lookup_errors(setup, error):
    adapter, client, _, _ = setup
    client.jobs.get.side_effect = error
    with pytest.raises(type(error)) as caught:
        adapter.wait_training("test-job")
    assert caught.value is error
    client.jobs.get.assert_called_once_with("test-job")
    azure.time.sleep.assert_not_called()
