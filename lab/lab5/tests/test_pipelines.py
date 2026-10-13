"""Offline compiler checks using the installed SDK, not a mock PipelineJob."""
from __future__ import annotations

import copy
import json
import shlex
import socket
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import azure.ai.ml
import azure.identity
import pytest
import requests
import yaml

from cloudlayer import pipelines
from cloudlayer import lab5_scope as scope
from cloudlayer.azure_registry import WORKSPACE
from scripts import pipeline_step
from src import config

EXAMPLE = Path(__file__).resolve().parents[1] / "cloud.env.example"
TRAINING_ID = "11111111-1111-4111-8111-111111111111"
RELEASE_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    deny = Mock(side_effect=AssertionError("Compiler must not contact cloud or run processes"))
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, deny)
    monkeypatch.setattr(requests.sessions.Session, "request", deny)
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(azure.ai.ml, "MLClient", deny)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", deny)
    for key in (*config.STAGING_SLOTS, "SERVING_INSTANCE", "SERVING_IMAGE",
                "SERVING_ALLOWED_IP", "MLFLOW_TRACKING_URI"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def options(tmp_path):
    raw = tmp_path / "raw data.csv"
    raw.write_text("test data is not read or trained during compilation\n")
    dvc = tmp_path / "raw.dvc"
    dvc.write_text("outs: []\n")  # Runtime validation owns this check, not compilation.
    cfg = replace(
        config.load_staging(EXAMPLE),
        serving_image=pipelines.ACR_SERVER + "/lab4-serve@sha256:" + "b" * 64,
        serving_allowed_ip="8.8.8.8/32",
        mlflow_tracking_uri=(
            f"azureml://malaysiawest.api.azureml.ms/mlflow/v1.0{scope.SCOPE}"
            f"/providers/Microsoft.MachineLearningServices/workspaces/{WORKSPACE}"
        ),
    )
    return dict(
        raw_data=raw, dvc_metadata=dvc, source_commit="a" * 40,
        image=pipelines.ACR_SERVER + "/lab5-pipeline@sha256:" + "c" * 64,
        compute="lab5-unit-test", training_client_id=TRAINING_ID, release_client_id=RELEASE_ID,
        staging=cfg,
    )


def document(options):
    job = pipelines.compile_azure(pipelines.load_spec(), **options)
    output = options["raw_data"].parent / "compiled.yaml"
    pipelines.dump_azure(job, output)
    return job, yaml.safe_load(output.read_text())


def test_real_sdk_builds_and_serializes_six_steps_offline(options):
    job, doc = document(options)
    assert isinstance(job, azure.ai.ml.entities.PipelineJob)
    assert set(doc["jobs"]) == set(pipelines.HANDOFFS), json.dumps(doc, indent=2)
    assert doc["settings"]["continue_on_step_failure"] is False
    assert doc["settings"]["force_rerun"] is True
    assert doc["tags"] == scope.TAGS
    assert set(doc["outputs"]) == {"training", "gate", "registered_model", "deployment"}
    output = options["raw_data"].parent / "compiled.yaml"
    reloaded = azure.ai.ml.load_job(output)
    assert set(reloaded.jobs) == set(pipelines.HANDOFFS)
    for name in ("raw_data", "dvc_metadata"):
        assert doc["inputs"][name]["path"] == str(options[name].resolve())
        assert Path(reloaded.inputs[name].path).resolve() == options[name].resolve()
        assert Path(reloaded.inputs[name].path).is_file()
    azure.ai.ml.MLClient.assert_not_called()
    azure.identity.DefaultAzureCredential.assert_not_called()


def test_serialized_edges_keep_gate_and_version_on_the_only_deploy_path(options):
    _, doc = document(options)
    edges = {
        "ingest": {"raw": "parent.inputs.raw_data"},
        "validate": {"raw": "parent.jobs.ingest.outputs.raw_data"},
        "train": {"validated": "parent.jobs.validate.outputs.validated",
                  "dvc_metadata": "parent.inputs.dvc_metadata",
                  "source_commit": "parent.inputs.source_commit"},
        "evaluate": {"training": "parent.jobs.train.outputs.bundle", "minimum": "parent.inputs.gate_minimum"},
        "register": {"training": "parent.jobs.train.outputs.bundle", "gate": "parent.jobs.evaluate.outputs.decision"},
        "deploy_staging": {"registered": "parent.jobs.register.outputs.registered"},
    }
    for name, expected in edges.items():
        assert set(doc["jobs"][name]["inputs"]) == set(expected)
        for key, binding in expected.items():
            actual = doc["jobs"][name]["inputs"][key]
            if isinstance(actual, dict):
                actual = actual.get("path", actual.get("value"))
            assert actual == "${{" + binding + "}}"
    exports = {"train": ("bundle", "training"), "evaluate": ("decision", "gate"),
               "register": ("registered", "registered_model"),
               "deploy_staging": ("deployment", "deployment")}
    for step, (output, published) in exports.items():
        assert doc["jobs"][step]["outputs"][output] == "${{parent.outputs." + published + "}}"


def test_components_use_image_code_and_explicit_runtime_settings(options):
    _, doc = document(options)
    for name, node in doc["jobs"].items():
        component = node["component"]
        assert not component.get("code")
        assert component["environment"]["image"] == options["image"]
        assert component["is_deterministic"] is False
        assert node["compute"] == "azureml:lab5-unit-test"
        assert node["resources"]["instance_count"] == 1
        client_id = RELEASE_ID if name == "deploy_staging" else TRAINING_ID
        assert node["identity"] == {"type": "managed_identity", "client_id": client_id}
        env = node["environment_variables"]
        assert env["PYTHONHASHSEED"] == "20260101"
        if name in {"register", "deploy_staging"}:
            assert env["AZURE_CLIENT_ID"] == client_id
            assert env["CLOUD_PROVIDER"] == "azure"
        for port in component["inputs"].values():
            if port["type"].startswith("uri_"):
                assert port["mode"] == "download"
        assert all(port == {"type": "uri_folder", "mode": "upload"}
                   for port in component["outputs"].values())
    deploy_env = doc["jobs"]["deploy_staging"]["environment_variables"]
    assert deploy_env["ENDPOINT_NAME"] == "lab5-staging"
    assert deploy_env["SERVING_IMAGE"] == options["staging"].serving_image
    assert "MODEL_URI" not in deploy_env  # Must come from this run's registration.json.
    assert "schedule" not in doc


@pytest.mark.parametrize("key,value", [
    ("source_commit", "a" * 40 + "-dirty"), ("source_commit", "abcdef0"),
    ("image", pipelines.ACR_SERVER + "/lab5:latest"),
    ("image", "other.azurecr.io/lab5@sha256:" + "c" * 64),
    ("compute", "azureml:somewhere"),
    ("training_client_id", ""), ("release_client_id", "00000000-0000-0000-0000-000000000000"),
    ("gate_minimum", 0.79), ("gate_minimum", float("nan")),
    ("gate_minimum", True), ("gate_minimum", 1.01),
])
def test_invalid_compile_settings_fail_offline(options, key, value):
    options[key] = value
    with pytest.raises(ValueError):
        pipelines.compile_azure(pipelines.load_spec(), **options)


@pytest.mark.parametrize("key", ["raw_data", "dvc_metadata"])
def test_missing_local_input_fails_before_compilation(options, key):
    options[key] = options[key].parent / "missing"
    with pytest.raises(ValueError, match="existing file"):
        pipelines.compile_azure(pipelines.load_spec(), **options)


def test_blank_staging_configuration_is_not_deployment_ready(options):
    options["staging"] = config.load_staging(EXAMPLE)
    with pytest.raises(ValueError):
        pipelines.compile_azure(pipelines.load_spec(), **options)


@pytest.mark.parametrize("change", ["missing_gate", "bypass_validate", "false_dependency", "missing_step",
                                     "unknown_input", "unused_input", "extra_schedule", "bad_output"])
def test_broken_neutral_dag_is_rejected(change):
    spec = copy.deepcopy(pipelines.load_spec())
    if change == "missing_gate":
        del spec["steps"][4]["inputs"]["gate"]
    elif change == "bypass_validate":
        spec["steps"][2]["inputs"]["validated"] = "ingest.raw_data"
    elif change == "false_dependency":
        spec["steps"][4]["depends_on"] = ["train"]
    elif change == "missing_step":
        spec["steps"].pop(1)
    elif change == "unknown_input":
        spec["steps"][0]["command"][5] = "${inputs.unknown}"
    elif change == "unused_input":
        spec["steps"][0]["command"][5] = "ignored.csv"
    elif change == "extra_schedule":
        spec["schedule"] = "daily"
    else:
        spec["steps"][2]["outputs"] = {"wrong": "folder"}
    with pytest.raises(ValueError):
        pipelines.validate_spec(spec)


def test_neutral_spec_has_no_azure_runtime_values():
    text = pipelines.SPEC.read_text()
    for provider_value in ("azureml:", "azurecr.io", "/subscriptions/", "${{", "cloudlayer.azure"):
        assert provider_value not in text


def test_export_refuses_to_overwrite_a_file(options):
    job, _ = document(options)
    output = options["raw_data"].parent / "compiled.yaml"
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        pipelines.dump_azure(job, output)
    assert output.read_bytes() == before


def test_compiled_gate_command_fails_without_a_pass_report(options, tmp_path):
    from scripts import evaluation_gate

    options["gate_minimum"] = 0.9
    _, doc = document(options)
    assert doc["inputs"]["gate_minimum"] == 0.9
    training = tmp_path / "train results"
    training.mkdir()
    (training / "metrics.json").write_text('{"val_roc_auc": 0.844499}')
    decision_dir = tmp_path / "gate result"
    command = doc["jobs"]["evaluate"]["component"]["command"]
    for binding, value in {"inputs.training": str(training), "inputs.minimum": "0.9",
                           "outputs.decision": str(decision_dir)}.items():
        command = command.replace("${{" + binding + "}}", value)
    tokens = shlex.split(command)
    assert tokens[:3] == ["python", "-m", "scripts.evaluation_gate"]
    assert evaluation_gate.main(tokens[3:]) == 1
    assert not (decision_dir / "gate.json").exists()
    # This checks the serialized command locally, not execution by AML's scheduler.


@pytest.mark.parametrize("step,module", [("register", "cloudlayer.azure_registry"),
                                        ("deploy_staging", "cloudlayer.azure_staging")])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_dispatch_preserves_arguments_and_failure(monkeypatch, step, module, exit_code):
    monkeypatch.setenv("CLOUD_PROVIDER", "azure")
    run = Mock(return_value=exit_code)
    monkeypatch.setattr(module + ".main", run)
    arguments = ["--output-dir", "/tmp/a path with spaces"]
    assert pipeline_step.main([step, *arguments]) == exit_code
    run.assert_called_once_with(arguments)


@pytest.mark.parametrize("provider", ["", "gcp", "AZURE"])
def test_dispatch_requires_explicit_supported_provider(monkeypatch, provider):
    monkeypatch.setenv("CLOUD_PROVIDER", provider)
    run = Mock(side_effect=AssertionError("Must not call registry"))
    monkeypatch.setattr("cloudlayer.azure_registry.main", run)
    assert pipeline_step.main(["register"]) == 1
    run.assert_not_called()


def test_compile_cli_writes_only_local_yaml(options, monkeypatch, capsys):
    monkeypatch.setattr(pipelines, "load_staging", lambda _: options["staging"])
    output = options["raw_data"].parent / "cli.yaml"
    arguments = ["--output", str(output)]
    for key, value in options.items():
        if key != "staging":
            arguments.extend(["--" + key.replace("_", "-"), str(value)])
    assert pipelines.main(arguments) == 0
    assert "No upload or submission" in capsys.readouterr().out
    assert set(azure.ai.ml.load_job(output).jobs) == set(pipelines.HANDOFFS)
    before = output.read_bytes()
    assert pipelines.main(arguments) == 1
    assert output.read_bytes() == before
    azure.ai.ml.MLClient.assert_not_called()
    azure.identity.DefaultAzureCredential.assert_not_called()
