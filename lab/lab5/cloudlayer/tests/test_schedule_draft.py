"""Local checks only: no schedule creation, asset uploads or pipeline submission."""
import copy
import io
import socket
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import azure.ai.ml
import azure.identity
import pytest
import requests
import yaml
from azure.ai.ml.entities import JobSchedule, PipelineJob

from cloudlayer import pipelines
from cloudlayer.azure_staging import validate_settings
from cloudlayer import azure_config as config

ROOT = Path(__file__).resolve().parents[2]
SCHEDULE = ROOT / "pipeline/schedule.draft.yaml"
PIPELINE = ROOT / "pipeline/scheduled-pipeline.draft.yaml"
TRAINING = "7e44580e-f307-411e-b25b-7dc7723710b4"
RELEASE = "c1ee259d-4522-4a94-bce9-6eeac16adfcd"
COMPONENTS = {
    "ingest": "6ded1cfa-4477-46b0-86d1-89ee22edfa1c",
    "validate": "4578a3e1-eef1-4e41-9d1e-ef961fa1d15e",
    "train": "b60ac6a8-8867-49b1-871d-c13785707594",
    "evaluate": "7234ff3e-2f6c-49df-bcab-02c56532c343",
    "register": "413b0dc8-8f6f-40f7-972b-f0e6e682245f",
    "deploy_staging": "3fb178fe-972c-4ed2-a009-7705e29400e6",
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    deny = Mock(side_effect=AssertionError("Schedule draft checks must stay offline"))
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, deny)
    monkeypatch.setattr(requests.sessions.Session, "request", deny)
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(azure.ai.ml, "MLClient", deny)
    for credential in ("DefaultAzureCredential", "AzureCliCredential", "ManagedIdentityCredential"):
        monkeypatch.setattr(azure.identity, credential, deny)
    for key in (*config.STAGING_SLOTS, "SERVING_INSTANCE", "SERVING_IMAGE",
                "SERVING_ALLOWED_IP", "MLFLOW_TRACKING_URI"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def document():
    return yaml.safe_load(PIPELINE.read_text())


def test_installed_sdk_loads_schedule_and_local_job_without_cloud():
    # azure-ai-ml 1.35.0 has no public load_schedule helper. Use its YAML schema
    # loader only; this does not construct MLClient or resolve cloud assets.
    data = yaml.safe_load(SCHEDULE.read_text())
    schedule = JobSchedule._load(data=data, yaml_path=str(SCHEDULE))
    assert isinstance(schedule.create_job, PipelineJob)
    assert set(schedule.create_job.jobs) == set(pipelines.HANDOFFS)
    assert schedule.trigger.time_zone == "SE Asia Standard Time"
    assert schedule.trigger.schedule.week_days == ["monday"]
    assert schedule.create_job.settings.default_datastore == "lab5_artifacts"
    assert all(node.compute == "cpu-lab5" for node in schedule.create_job.jobs.values())
    assert all(node.identity.object_id is None for node in schedule.create_job.jobs.values())
    assert schedule.create_job.name != "lab5-ci-39226985486-1"
    stream = io.StringIO()
    schedule.dump(stream)
    assert "lab5-weekly-retraining" in stream.getvalue()
    azure.ai.ml.MLClient.assert_not_called()


def test_weekly_pattern_has_only_one_occurrence_in_review_window():
    doc = yaml.safe_load(SCHEDULE.read_text())
    trigger = doc["trigger"]
    assert trigger["type"] == "recurrence"
    assert (trigger["frequency"], trigger["interval"]) == ("week", 1)
    assert trigger["schedule"] == {"week_days": ["monday"], "hours": [13], "minutes": [0]}
    assert trigger["time_zone"] == "SE Asia Standard Time"
    zone = ZoneInfo("Asia/Bangkok")
    start, end = (datetime.fromisoformat(trigger[key]).replace(tzinfo=zone)
                  for key in ("start_time", "end_time"))
    assert start < end < start + timedelta(days=7)
    candidate = start.replace(hour=13, minute=0, second=0)
    assert candidate.weekday() == 0 and start < candidate < end
    assert candidate.utcoffset() == timedelta(hours=7)
    assert candidate + timedelta(days=7) > end
    assert doc["create_job"] == "./scheduled-pipeline.draft.yaml"
    # is_enabled is read-back-only in this SDK, not a safe create-disabled flag.
    assert "is_enabled" not in doc


@pytest.mark.parametrize("step", COMPONENTS)
def test_exact_component_compute_identity_and_timeout_are_reused(document, step):
    node = document["jobs"][step]
    assert node["component"] == "azureml:azureml_anonymous:" + COMPONENTS[step]
    assert node["compute"] == "cpu-lab5"
    assert node["identity"] == {
        "type": "managed_identity", "client_id": RELEASE if step == "deploy_staging" else TRAINING,
    }
    assert node["resources"] == {"instance_count": 1}
    assert node["limits"] == {"timeout": 600}
    assert not {"code", "command", "environment"} & node.keys()
    if step in {"register", "deploy_staging"}:
        assert node["environment_variables"]["AZURE_CLIENT_ID"] == node["identity"]["client_id"]


def test_inputs_are_exact_uploaded_files_and_original_source_sha(document):
    root = "azureml://datastores/lab5_inputs/paths/LocalUpload/"
    expected = {
        "raw_data": "d867f5b0b5985ab022b7dd92ec798cba7eb9d7883cb65d8cdaba545a8126f670/sensors.csv",
        "dvc_metadata": "dde3a609bdacb4fbc7008ec18b3a19a3881eaedd45431707196655018c505b0f/raw.dvc",
    }
    for key, path in expected.items():
        assert document["inputs"][key] == {
            "type": "uri_file", "mode": "download", "path": root + path,
        }
    assert document["inputs"]["source_commit"] == "5c1483ed9000a18c7722cafecba9aa07d07d5048"
    assert document["inputs"]["gate_minimum"] == 0.80


def test_handoffs_and_failure_stop_are_unchanged(document):
    assert set(document["jobs"]) == set(pipelines.HANDOFFS)
    for step, bindings in pipelines.HANDOFFS.items():
        expected = {}
        for key, reference in bindings.items():
            parent, output = reference.split(".")
            path = f"parent.inputs.{output}" if parent == "inputs" else f"parent.jobs.{parent}.outputs.{output}"
            expected[key] = {"path": "${{" + path + "}}"}
        assert document["jobs"][step]["inputs"] == expected
    assert document["settings"] == {
        "continue_on_step_failure": False, "force_rerun": True,
        "default_datastore": "azureml:lab5_artifacts",
    }
    assert set(document["outputs"]) == {"training", "gate", "registered_model", "deployment"}
    for output in document["outputs"].values():
        assert output == {"type": "uri_folder", "mode": "upload"}
    assert not {"name", "id", "status", "creation_context", "services", "properties"} & document.keys()


def test_ip_is_placeholder_not_an_old_ip_or_open_ingress(document):
    env = document["jobs"]["deploy_staging"]["environment_variables"]
    assert env["SERVING_ALLOWED_IP"] == "REPLACE_WITH_APPROVED_PUBLIC_IPV4_32"
    cfg = replace(
        config.load_staging(ROOT / "cloud.env.example"),
        serving_image=env["SERVING_IMAGE"], serving_allowed_ip=env["SERVING_ALLOWED_IP"],
        mlflow_tracking_uri=env["MLFLOW_TRACKING_URI"],
    )
    with pytest.raises(ValueError, match="public IPv4"):
        validate_settings(cfg)
    # A synthetic public /32 is checked in memory only, never saved or deployed.
    assert validate_settings(replace(cfg, serving_allowed_ip="8.8.8.8/32")) == "8.8.8.8/32"
    assert env["ENDPOINT_NAME"] == "lab5-staging"
    assert env["SERVING_IMAGE"] == (
        "itcs355u6688124.azurecr.io/itcs355-lab4@sha256:"
        "654ed915ca7c12f59b619979f3795c705b82eb2a9ee23620b520e185095c6bb8"
    )


def test_sdk_round_trip_does_not_overwrite_draft_files(document, tmp_path):
    original = {p: p.read_bytes() for p in (SCHEDULE, PIPELINE)}
    ready = copy.deepcopy(document)
    ready["jobs"]["deploy_staging"]["environment_variables"]["SERVING_ALLOWED_IP"] = "8.8.8.8/32"
    job = azure.ai.ml.load_job(io.StringIO(yaml.safe_dump(ready)))
    output = tmp_path / "job.yaml"
    job.dump(output)
    reloaded = azure.ai.ml.load_job(output)
    assert reloaded.settings.default_datastore == "lab5_artifacts"
    assert all(node.compute == "cpu-lab5" for node in reloaded.jobs.values())
    assert {p: p.read_bytes() for p in original} == original
