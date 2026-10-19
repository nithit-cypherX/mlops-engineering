"""Draft CI checks. No Docker, credentials, network, or Azure submission."""
from __future__ import annotations

import json
import re
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import azure.ai.ml
import azure.identity
import pytest
import yaml

from cloudlayer import pipelines
from cloudlayer.azure_storage import ARTIFACTS_DATASTORE, INPUTS_DATASTORE
from cloudlayer.lab5_scope import SCOPE

LAB = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = LAB.parents[1] / ".github/workflows/lab5-ci.yml"
TEXT = WORKFLOW_PATH.read_text()
# BaseLoader keeps the GitHub key `on` as a string, not YAML 1.1's boolean True.
WORKFLOW = yaml.load(TEXT, Loader=yaml.BaseLoader)
JOB = WORKFLOW["jobs"]["pipeline"]
STEPS = JOB["steps"]


def step(name):
    return next(item for item in STEPS if item.get("name") == name)


@pytest.fixture
def snippets(monkeypatch):
    deny = Mock(side_effect=AssertionError("Offline only"))
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(azure.ai.ml, "MLClient", deny)
    monkeypatch.setattr(azure.identity, "AzureCliCredential", deny)
    script = step("Submit once and wait for the managed result")["run"]
    source = script.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    namespace = {"__name__": "offline_ci_test"}
    exec(compile(source, str(WORKFLOW_PATH), "exec"), namespace)
    return namespace


def test_manual_main_only_and_disabled_until_configured():
    assert set(WORKFLOW["on"]) == {"workflow_dispatch"}
    assert JOB["if"] == "github.ref == 'refs/heads/main'"
    assert JOB["environment"] == "lab5-ci"
    assert WORKFLOW["concurrency"]["cancel-in-progress"] == "false"
    guard = STEPS[0]
    assert guard["env"]["ENABLED"] == "${{ vars.LAB5_CI_ENABLED }}"
    assert 'test "$ENABLED" = true' in guard["run"]
    assert "exit 1" in guard["run"]
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert JOB["permissions"] == {"contents": "read", "id-token": "write"}
    assert "continue-on-error" not in TEXT


@pytest.mark.parametrize("enabled,client_id,tenant_id,ip,accepted", [
    ("", "test-client", "test-tenant", "8.8.8.8/32", False),
    ("false", "test-client", "test-tenant", "8.8.8.8/32", False),
    ("true", "", "test-tenant", "8.8.8.8/32", False),
    ("true", "test-client", "", "8.8.8.8/32", False),
    ("true", "test-client", "test-tenant", "", False),
    ("true", "test-client", "test-tenant", "8.8.8.8/32", True),
])
def test_enablement_guard_without_cloud(enabled, client_id, tenant_id, ip, accepted):
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", STEPS[0]["run"]],
        env={"ENABLED": enabled, "CLIENT_ID": client_id, "TENANT_ID": tenant_id,
             "SERVING_ALLOWED_IP": ip}, capture_output=True, text=True, timeout=5, check=False,
    )
    assert (result.returncode == 0) is accepted


def test_all_shell_blocks_parse_without_executing_them():
    for item in STEPS:
        if "run" in item:
            result = subprocess.run(["bash", "-n"], input=item["run"], capture_output=True,
                                    text=True, timeout=5, check=False)
            assert result.returncode == 0, (item["name"], result.stderr)


def test_actions_are_pinned_and_checkout_matches_image_lineage():
    for item in STEPS:
        if "uses" in item:
            assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", item["uses"])
    checkout = next(s for s in STEPS if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": "false"}
    build = step("Build and test the exact pipeline image before login")["run"]
    assert '--build-arg GIT_COMMIT="$GITHUB_SHA"' in build
    assert "--platform linux/amd64 --load" in build
    assert "--network none" in build and "test_staging.py" in build
    assert "/app/src" not in build and "/app/cloudlayer" not in build
    assert "--require-hashes -r requirements.txt" in TEXT


def test_checks_precede_login_push_and_single_submission():
    names = [s.get("name", s.get("uses")) for s in STEPS]
    ordered = ["Offline tests and lint", "Restore the existing hash-checked dataset",
               "Build and test the exact pipeline image before login",
               "Log in as the Lab 5 CI identity", "Push the tested image and resolve its digest",
               "Submit once and wait for the managed result"]
    assert [names.index(name) for name in ordered] == sorted(names.index(name) for name in ordered)
    assert "${{ steps.build.outputs.image }}" == step(
        "Push the tested image and resolve its digest")["env"]["IMAGE_TAG"]
    assert "${{ steps.push.outputs.image }}" == step(
        "Submit once and wait for the managed result")["env"]["PIPELINE_IMAGE"]
    assert TEXT.count("client.jobs.create_or_update(job)") == 1
    assert "az containerapp" not in TEXT and "az role assignment" not in TEXT
    assert "az identity create" not in TEXT
    assert "upload-artifact" not in TEXT  # No YAML containing the private /32 IP is published.
    assert JOB["env"]["SERVING_ALLOWED_IP"] == "${{ secrets.LAB5_SERVING_ALLOWED_IP }}"
    assert "api.ipify" not in TEXT and "0.0.0.0/0" not in TEXT


def test_ci_role_has_only_explicit_ml_submission_actions():
    role = json.loads((LAB / "pipeline/ci-role.json").read_text())
    assert role["IsCustom"] is True
    # Availability is RG-scoped; the later assignment must be workspace-scoped.
    assert role["AssignableScopes"] == [SCOPE]
    assert "Assign only at mlw-itcs355-u6688124" in role["Description"]
    actions = role["Actions"]
    assert len(actions) == len(set(actions)) == 22
    suffixes = {a.removeprefix("Microsoft.MachineLearningServices/workspaces/") for a in actions}
    assert suffixes == {
        "read", "computes/read", "datastores/read", "jobs/read", "jobs/write",
        "components/read", "components/write", "components/versions/read", "components/versions/write",
        "modules/read",  # Azure required this during component registration in CI #3.
        "modules/write",  # Azure required this during component registration in CI #2.
        "experiments/runs/read",  # Azure required this during job submission in CI #4.
        "experiments/runs/submit/action",  # Azure required this during job submission in CI #5.
        # Reviewed draft additions; CI #7 did not identify the missing inner action.
        "experiments/read", "experiments/write", "experiments/runs/write",
        "metadata/artifacts/read", "metadata/artifacts/write",
        "environments/read", "environments/write", "environments/versions/read", "environments/versions/write",
    }
    assert all(a.startswith("Microsoft.MachineLearningServices/workspaces/") for a in actions)
    assert all("*" not in a and "secret" not in a.lower() and "delete" not in a for a in actions)
    assert role["DataActions"] == role["NotActions"] == role["NotDataActions"] == []


def test_job_factory_reuses_six_step_dag_and_cost_identity_guards(snippets, tmp_path, monkeypatch):
    lab = tmp_path / "lab5"
    lab.mkdir()
    raw = tmp_path / "lab4/data/raw"
    raw.mkdir(parents=True)
    (raw / "sensors.csv").write_text("offline compile only\n")
    (raw.parent / "raw.dvc").write_text("outs: []\n")
    (lab / "cloud.env.example").write_text((LAB / "cloud.env.example").read_text())
    monkeypatch.chdir(lab)
    for key, value in JOB["env"].items():
        monkeypatch.setenv(key, value)
    for key, value in {
        "SERVING_ALLOWED_IP": "8.8.8.8/32", "GITHUB_SHA": "a" * 40,
        "PIPELINE_IMAGE": "itcs355u6688124.azurecr.io/itcs355-lab5@sha256:" + "b" * 64,
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, value)
    job = snippets["make_job"]()
    assert job.name == "lab5-ci-123-1"
    assert set(job.jobs) == set(pipelines.HANDOFFS)
    output = tmp_path / "compiled.yaml"
    pipelines.dump_azure(job, output)
    document = yaml.safe_load(output.read_text())
    assert document["inputs"]["gate_minimum"] == 0.80
    assert document["inputs"]["source_commit"] == "a" * 40
    assert job.settings.continue_on_step_failure is False
    assert job.settings.force_rerun is True
    assert job.settings.default_datastore == ARTIFACTS_DATASTORE
    assert document["settings"]["default_datastore"] == "azureml:" + ARTIFACTS_DATASTORE
    for name, node in job.jobs.items():
        assert node.compute == "cpu-lab5"
        assert document["jobs"][name]["compute"] == "azureml:cpu-lab5"
        assert node.resources.instance_count == 1
        assert node.limits.timeout == 600
        assert document["jobs"][name]["limits"]["timeout"] == 600
        key = "RELEASE_CLIENT_ID" if name == "deploy_staging" else "TRAINING_CLIENT_ID"
        assert node.identity.client_id == JOB["env"][key]
        assert node.component.code is None
    for name in ("raw_data", "dvc_metadata"):
        assert job.inputs[name]._data.datastore == INPUTS_DATASTORE
    monkeypatch.setenv("SERVING_ALLOWED_IP", "0.0.0.0/0")
    with pytest.raises(ValueError):
        snippets["make_job"]()
    azure.ai.ml.MLClient.assert_not_called()
    azure.identity.AzureCliCredential.assert_not_called()


@pytest.fixture
def client(snippets):
    client = Mock()
    client.datastores.get.side_effect = [
        azure.ai.ml.load_datastore(LAB / f"pipeline/datastore-{kind}.yaml")
        for kind in ("inputs", "artifacts")
    ]
    client.jobs.create_or_update.return_value = SimpleNamespace(name="lab5-ci-123-1")
    return client


@pytest.mark.parametrize("status", ["Completed", "Failed", "Canceled", "NotResponding"])
def test_managed_terminal_result_is_not_confused_with_submission(snippets, client, status):
    client.jobs.get.side_effect = [SimpleNamespace(status="Running"), SimpleNamespace(status=status)]
    sleep = Mock()
    job = object()
    if status == "Completed":
        snippets["submit_and_wait"](client, job, clock=lambda: 0, sleep=sleep)
    else:
        with pytest.raises(RuntimeError, match=status):
            snippets["submit_and_wait"](client, job, clock=lambda: 0, sleep=sleep)
    client.jobs.create_or_update.assert_called_once_with(job)
    sleep.assert_called_once_with(30)
    client.jobs.cancel.assert_not_called()


def test_wait_timeout_reports_running_job_without_resubmitting(snippets, client):
    client.jobs.get.return_value = SimpleNamespace(status="Running")
    clock = Mock(side_effect=[0, 0, 3601])
    with pytest.raises(TimeoutError, match="may still be running"):
        snippets["submit_and_wait"](client, object(), clock=clock, sleep=Mock())
    client.jobs.create_or_update.assert_called_once()
    client.jobs.cancel.assert_not_called()


def test_wrong_datastore_stops_before_any_submission(snippets, client):
    client.datastores.get.side_effect = [SimpleNamespace()]
    with pytest.raises(ValueError):
        snippets["submit_and_wait"](client, object())
    client.jobs.create_or_update.assert_not_called()


def test_submission_error_does_not_retry_or_report_completion(snippets, client, capsys):
    client.jobs.create_or_update.side_effect = RuntimeError("Permission denied")
    with pytest.raises(RuntimeError, match="Permission denied"):
        snippets["submit_and_wait"](client, object())
    client.jobs.create_or_update.assert_called_once()
    client.jobs.get.assert_not_called()
    assert "Completed" not in capsys.readouterr().out
