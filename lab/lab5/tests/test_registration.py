"""Registration checks with synthetic files and a mock registry, never Azure."""
from __future__ import annotations

import hashlib
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

import azure.ai.ml
import azure.identity
import pytest
import yaml
from azure.core.exceptions import AzureError

from cloudlayer import azure_registry as registry
from cloudlayer.azure_storage import ARTIFACTS_DATASTORE
from cloudlayer.azure_staging import read_registration
from scripts import evaluation_gate as gate


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(
            socket.socket, method, Mock(side_effect=AssertionError("No network")),
        )
    monkeypatch.setattr(
        azure.identity, "DefaultAzureCredential",
        Mock(side_effect=AssertionError("No real credentials in tests")),
    )
    monkeypatch.setattr(
        azure.ai.ml, "MLClient", Mock(side_effect=AssertionError("No real cloud client in tests")),
    )


def sync_hashes(directory):
    report = directory / "metrics.json"
    payload = json.loads(report.read_text())
    payload["model_files_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (directory / "model").iterdir() if path.is_file()
    }
    report.write_text(json.dumps(payload))


@pytest.fixture
def candidate(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    payload = {
        "git_commit": "a" * 40 + "-dirty",
        "data_fingerprint": "b" * 64, "dvc_hash": "c" * 32 + ".dir",
        "seed": 20260101, "params": {"n_estimators": 100, "max_depth": 12},
        "val_roc_auc": 0.85, "val_pr_auc": 0.4,
        "test_roc_auc": 0.84, "test_pr_auc": 0.45,
    }
    document = {
        "metadata": payload.copy(),
        "flavors": {
            "sklearn": {"pickled_model": "model.pkl"},
            "python_function": {"model_path": "model.pkl"},
        },
    }
    (model / "MLmodel").write_text(yaml.safe_dump(document))
    # This is deliberately not a pickle: validation must never execute model code.
    for name in registry.MODEL_FILES - {"MLmodel"}:
        (model / name).write_text("offline test fixture, not a trained model")
    (tmp_path / "metrics.json").write_text(json.dumps(payload))
    sync_hashes(tmp_path)
    return tmp_path


@pytest.fixture
def client():
    fake = SimpleNamespace(
        subscription_id=registry.SUBSCRIPTION, resource_group_name=registry.GROUP,
        workspace_name=registry.WORKSPACE,
        models=Mock(spec=["create_or_update", "get"]),
        datastores=Mock(spec=["get"]),
    )
    fake.datastores.get.return_value = azure.ai.ml.load_datastore(
        Path(__file__).resolve().parents[1] / "pipeline" / "datastore-artifacts.yaml",
    )
    fake.models.create_or_update.return_value = SimpleNamespace(
        name=registry.MODEL_NAME, version="7",
    )

    def get(**_):
        sent = fake.models.create_or_update.call_args.args[0]
        return SimpleNamespace(
            name=registry.MODEL_NAME, version="7", type="mlflow_model",
            id=f"{registry.MODEL_ROOT}/versions/7", tags=sent.tags.copy(),
        )

    fake.models.get.side_effect = get
    return fake


def change_report(directory, key, value):
    path = directory / "metrics.json"
    payload = json.loads(path.read_text())
    payload[key] = value
    path.write_text(json.dumps(payload))


def test_pass_registers_once_with_lineage_without_promotion(candidate, client):
    before = {p: p.read_bytes() for p in candidate.rglob("*") if p.is_file()}
    result = registry.register_training(candidate, client=client)
    assert result == {
        "name": registry.MODEL_NAME, "version": "7",
        "id": f"{registry.MODEL_ROOT}/versions/7",
        "model_uri": f"azureml:{registry.MODEL_NAME}:7",
    }
    client.models.create_or_update.assert_called_once()
    client.models.get.assert_called_once_with(name=registry.MODEL_NAME, version="7")
    sent = client.models.create_or_update.call_args.args[0]
    assert sent.name == registry.MODEL_NAME
    assert sent.version is None
    assert sent.datastore == ARTIFACTS_DATASTORE
    client.datastores.get.assert_called_once_with(name=ARTIFACTS_DATASTORE)
    assert sent.stage is None
    assert sent.type == "mlflow_model"
    assert sent.path == str((candidate / "model").resolve())
    assert sent.tags["git_commit"] == "a" * 40 + "-dirty"
    assert sent.tags["data_fingerprint"] == "b" * 64
    assert sent.tags["dvc_hash"] == "c" * 32 + ".dir"
    assert sent.tags["seed"] == "20260101"
    assert sent.tags["val_roc_auc"] == "0.85"
    assert sent.tags["gate_metric"] == "val_roc_auc"
    assert sent.tags["gate_minimum"] == "0.8"
    assert sent.tags["training_record_sha256"] == hashlib.sha256(
        (candidate / "metrics.json").read_bytes(),
    ).hexdigest()
    assert sent.tags["model_sha256"] == hashlib.sha256(
        (candidate / "model" / "model.pkl").read_bytes(),
    ).hexdigest()
    assert {p: p.read_bytes() for p in candidate.rglob("*") if p.is_file()} == before
    assert len(client.models.mock_calls) == 2


@pytest.mark.parametrize("score", [0.79, None, True, "0.99", float("nan"), float("inf")])
def test_bad_or_low_score_never_calls_registry(candidate, client, score):
    change_report(candidate, "val_roc_auc", score)
    with pytest.raises(ValueError, match="val_roc_auc"):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("payload", ["{", "null", "[]", '{"test_roc_auc": 1.0}'])
def test_bad_metrics_file_never_calls_registry(candidate, client, payload):
    (candidate / "metrics.json").write_text(payload)
    with pytest.raises(ValueError):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("key, value", [
    ("git_commit", "unknown"), ("data_fingerprint", ""), ("dvc_hash", None),
    ("seed", True), ("seed", -1), ("params", {}),
    ("test_roc_auc", float("nan")),
])
def test_incomplete_lineage_or_other_metrics_block_registration(candidate, client, key, value):
    change_report(candidate, key, value)
    with pytest.raises(ValueError):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("name", ["metrics.json", *["model/" + n for n in sorted(registry.MODEL_FILES)]])
def test_missing_files_block_registration(candidate, client, name):
    (candidate / name).unlink()
    with pytest.raises((OSError, ValueError)):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("name", sorted(registry.MODEL_FILES))
def test_changed_model_files_block_registration(candidate, client, name):
    with (candidate / "model" / name).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="hashes"):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


def test_old_export_without_hashes_is_not_silently_upgraded(candidate, client):
    path = candidate / "metrics.json"
    payload = json.loads(path.read_text())
    del payload["model_files_sha256"]
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="hashes"):
        registry.register_training(candidate, client=client)
    assert path.read_bytes() == before
    assert client.models.mock_calls == []


@pytest.mark.parametrize("key, value", [
    ("val_roc_auc", 0.99), ("git_commit", "d" * 40), ("data_fingerprint", "e" * 64),
    ("params", {"n_estimators": 999}),
])
def test_mixed_metrics_or_lineage_block_even_if_scores_pass(candidate, client, key, value):
    change_report(candidate, key, value)
    with pytest.raises(ValueError, match="metadata and training"):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("content", ["[", "[]", "null"])
def test_invalid_model_metadata_blocks_registration(candidate, client, content):
    (candidate / "model" / "MLmodel").write_text(content)
    sync_hashes(candidate)
    with pytest.raises(ValueError):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


def test_flavor_cannot_point_outside_verified_model(candidate, client):
    path = candidate / "model" / "MLmodel"
    document = yaml.safe_load(path.read_text())
    document["flavors"]["sklearn"]["pickled_model"] = "../other.pkl"
    path.write_text(yaml.safe_dump(document))
    sync_hashes(candidate)
    with pytest.raises(ValueError, match="both MLflow flavors"):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "empty"])
def test_extra_or_unsafe_artifacts_are_not_uploaded(candidate, client, kind):
    model = candidate / "model"
    if kind == "file":
        (model / "unrelated.env").write_text("synthetic test fixture only")
    elif kind == "directory":
        (model / "unrelated").mkdir()
    elif kind == "symlink":
        (model / "model.pkl").unlink()
        (model / "model.pkl").symlink_to(candidate / "metrics.json")
    else:
        (model / "model.pkl").write_bytes(b"")
    with pytest.raises(ValueError, match="five non-empty"):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("attribute", ["subscription_id", "resource_group_name", "workspace_name"])
def test_wrong_cloud_scope_never_calls_registry(candidate, client, attribute):
    setattr(client, attribute, "other-scope")
    with pytest.raises(ValueError, match="unexpected"):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("operation", ["create_or_update", "get"])
def test_registry_error_is_not_success_or_blindly_retried(candidate, client, operation):
    getattr(client.models, operation).side_effect = AzureError("simulated registry failure")
    with pytest.raises(RuntimeError, match="before retrying"):
        registry.register_training(candidate, client=client)
    assert client.models.create_or_update.call_count == 1
    assert client.models.get.call_count == (1 if operation == "get" else 0)


@pytest.mark.parametrize("field, value", [("name", "production"), ("version", None), ("version", "")])
def test_bad_create_response_does_not_report_success(candidate, client, field, value):
    setattr(client.models.create_or_update.return_value, field, value)
    with pytest.raises(RuntimeError, match="Unexpected registration"):
        registry.register_training(candidate, client=client)
    client.models.get.assert_not_called()


@pytest.mark.parametrize("field, value", [
    ("name", "production"), ("version", "8"), ("type", "custom_model"),
    ("id", "/other/models/version/7"), ("tags", {}), ("tags", None),
])
def test_readback_mismatch_is_not_success(candidate, client, field, value):
    original = client.models.get.side_effect

    def different(**kwargs):
        result = original(**kwargs)
        setattr(result, field, value)
        return result

    client.models.get.side_effect = different
    with pytest.raises(RuntimeError, match="read-back verification"):
        registry.register_training(candidate, client=client)
    assert client.models.create_or_update.call_count == 1
    assert client.models.get.call_count == 1


@pytest.fixture
def step(candidate):
    gate_path = candidate / "gate.json"
    gate_path.write_text(json.dumps(gate.decision((candidate / "metrics.json").read_bytes())))
    output = candidate / "registered"
    return SimpleNamespace(
        gate=gate_path, output=output,
        args=["--training-dir", str(candidate), "--gate-report", str(gate_path),
              "--output-dir", str(output)],
    )


def test_command_writes_only_verified_version_after_readback(step, client, capsys):
    assert registry.main(step.args, client=client) == 0
    result = json.loads((step.output / "registration.json").read_text())
    assert result == {
        "name": registry.MODEL_NAME, "version": "7", "id": f"{registry.MODEL_ROOT}/versions/7",
        "model_uri": f"azureml:{registry.MODEL_NAME}:7",
    }
    assert json.loads(capsys.readouterr().out) == result
    assert read_registration(step.output / "registration.json") == result
    assert len(client.models.mock_calls) == 2
    azure.identity.DefaultAzureCredential.assert_not_called()
    azure.ai.ml.MLClient.assert_not_called()


def test_command_constructs_client_for_fixed_scope_only_after_checks(step, client, monkeypatch):
    credential = object()
    auth = Mock(return_value=credential)
    factory = Mock(return_value=client)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", auth)
    monkeypatch.setattr(azure.ai.ml, "MLClient", factory)
    assert registry.main(step.args) == 0
    auth.assert_called_once_with()
    factory.assert_called_once_with(credential, registry.SUBSCRIPTION, registry.GROUP, registry.WORKSPACE)


@pytest.mark.parametrize("problem", ["missing_gate", "failed_gate", "mixed_gate", "changed_model"])
def test_command_rejects_bad_handoff_before_credentials(candidate, step, problem, capsys):
    if problem == "missing_gate":
        step.gate.unlink()
    elif problem == "changed_model":
        (candidate / "model" / "model.pkl").write_bytes(b"different bundle")
    else:
        report = json.loads(step.gate.read_text())
        if problem == "failed_gate":
            report["status"] = "fail"
        else:
            report["training_record_sha256"] = "0" * 64
        step.gate.write_text(json.dumps(report))
    assert registry.main(step.args) == 1
    azure.identity.DefaultAzureCredential.assert_not_called()
    azure.ai.ml.MLClient.assert_not_called()
    assert not (step.output / "registration.json").exists()
    captured = capsys.readouterr()
    assert "REGISTER FAIL" in captured.err
    assert not captured.out


def test_command_preserves_old_output_without_another_registration(step, client):
    step.output.mkdir()
    old = step.output / "registration.json"
    old.write_text("previous result")
    assert registry.main(step.args, client=client) == 1
    assert old.read_text() == "previous result"
    assert client.models.mock_calls == []


@pytest.mark.parametrize("operation", ["create_or_update", "get"])
def test_command_registry_failure_has_no_output_or_retry(step, client, operation, capsys):
    getattr(client.models, operation).side_effect = AzureError("simulated failure")
    assert registry.main(step.args, client=client) == 1
    assert not (step.output / "registration.json").exists()
    assert client.models.create_or_update.call_count == 1
    captured = capsys.readouterr()
    assert "before retrying" in captured.err
    assert not captured.out


def test_command_bad_readback_cannot_publish_version(step, client):
    client.models.get.side_effect = None
    client.models.get.return_value = SimpleNamespace(version="7", tags={})
    assert registry.main(step.args, client=client) == 1
    assert not (step.output / "registration.json").exists()


def test_command_output_write_failure_reports_created_version(step, client, monkeypatch, capsys):
    original = Path.open

    def open_checked(path, *args, **kwargs):
        if path == step.output / "registration.json":
            raise OSError("simulated write failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_checked)
    assert registry.main(step.args, client=client) == 1
    client.models.create_or_update.assert_called_once()
    captured = capsys.readouterr()
    assert "Version 7 was verified" in captured.err
    assert "before retrying" in captured.err
    assert not captured.out


def test_pipeline_entry_requires_gate_argument(candidate):
    with pytest.raises(SystemExit) as exc:
        registry.main(["--training-dir", str(candidate), "--output-dir", str(candidate / "out")])
    assert exc.value.code == 2
    azure.identity.DefaultAzureCredential.assert_not_called()


def test_stricter_gate_policy_is_logged_in_registry_tags(candidate, step, client):
    step.gate.write_text(json.dumps(gate.decision((candidate / "metrics.json").read_bytes(), 0.82)))
    assert registry.main(step.args, client=client) == 0
    assert client.models.create_or_update.call_args.args[0].tags["gate_minimum"] == "0.82"


def test_bad_candidate_stops_before_datastore_lookup(candidate, client):
    (candidate / "model" / "model.pkl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hashes"):
        registry.register_training(candidate, client=client)
    assert client.datastores.mock_calls == []
    assert client.models.mock_calls == []


def test_missing_or_inaccessible_datastore_stops_before_upload(candidate, client):
    client.datastores.get.side_effect = AzureError("synthetic storage read failure")
    with pytest.raises(RuntimeError, match="no registration was attempted"):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


@pytest.mark.parametrize("change", ["account", "container", "credentials", "tags"])
def test_invalid_live_storage_metadata_stops_registration(candidate, client, change):
    store = client.datastores.get.return_value
    if change == "account":
        store.account_name = "otheraccount"
    elif change == "container":
        store.container_name = "azureml"
    elif change == "credentials":
        from azure.ai.ml.entities import AccountKeyConfiguration

        store.credentials = AccountKeyConfiguration(account_key="synthetic-not-a-secret")
    else:
        store.tags = {}
    with pytest.raises(ValueError):
        registry.register_training(candidate, client=client)
    assert client.models.mock_calls == []


def test_installed_sdk_auto_version_does_not_access_default_storage(candidate, client, monkeypatch):
    # Keep SDK version allocation while checking its real control flow. Upload
    # and service calls are fake; this is not proof of real Azure registration.
    from azure.ai.ml.operations import _model_operations as sdk
    from azure.ai.ml._scope_dependent_operations import OperationScope, OperationConfig

    registry.register_training(candidate, client=client)
    model = client.models.create_or_update.call_args.args[0]
    assert model.version is None
    service = Mock()
    operations = sdk.ModelOperations(
        OperationScope(registry.SUBSCRIPTION, registry.GROUP, registry.WORKSPACE),
        OperationConfig(show_progress=False, enable_telemetry=False),
        service, client.datastores,
    )
    monkeypatch.setattr(operations, "_get_model_properties", lambda _: {})
    monkeypatch.setattr(sdk, "_get_next_version_from_container", lambda **_: "8")
    default = Mock(side_effect=AssertionError("workspace default storage accessed"))
    monkeypatch.setattr(sdk, "_get_default_datastore_info", default)

    def upload(*, artifact, **_):
        assert artifact.datastore == ARTIFACTS_DATASTORE
        artifact.path = "azureml://datastores/lab5_artifacts/paths/offline/model"
        return artifact, "offline-upload-indicator"

    monkeypatch.setattr(sdk, "_check_and_upload_path", upload)

    def create(*, body, version, **_):
        body.id = f"{registry.MODEL_ROOT}/versions/{version}"
        body.properties.flavors = {}
        return body

    service.model_versions.create_or_update.side_effect = create
    assert operations.create_or_update(model).version == "8"
    default.assert_not_called()
    service.model_versions.create_or_update.assert_called_once()
