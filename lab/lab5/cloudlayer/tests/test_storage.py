"""Storage drafts and real SDK serialization, with all network/auth blocked."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import socket

import azure.ai.ml
import azure.identity
import pytest
import requests
import yaml

from cloudlayer import azure_storage as storage, lab5_scope as scope

PIPELINE = Path(__file__).resolve().parents[2] / "pipeline"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    deny = Mock(side_effect=AssertionError("Storage tests must remain offline"))
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, deny)
    monkeypatch.setattr(requests.sessions.Session, "request", deny)
    monkeypatch.setattr(azure.ai.ml, "MLClient", deny)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", deny)


@pytest.mark.parametrize("suffix,name", [
    ("inputs", storage.INPUTS_DATASTORE), ("artifacts", storage.ARTIFACTS_DATASTORE),
])
def test_drafts_load_as_credentialless_blob_containers(suffix, name):
    path = PIPELINE / f"datastore-{suffix}.yaml"
    doc = yaml.safe_load(path.read_text())
    assert "credentials" not in doc
    assert "is_default" not in doc
    store = azure.ai.ml.load_datastore(path)
    storage.validate_datastore(store, name=name)
    props = store._to_rest_object().as_dict()["properties"]
    assert props["credentials"] == {"credentialsType": "None"}
    assert props["accountName"] == storage.STORAGE_ACCOUNT
    assert props["containerName"] == storage.CONTAINERS[name]
    assert props["tags"] == scope.TAGS


@pytest.mark.parametrize("field,value", [
    ("name", "workspaceblobstore"), ("account_name", "anotheraccount"),
    ("container_name", "azureml"), ("protocol", "wasbs"),
    ("endpoint", "example.invalid"), ("tags", {}),
])
def test_changed_storage_target_is_rejected(field, value):
    store = azure.ai.ml.load_datastore(PIPELINE / "datastore-artifacts.yaml")
    setattr(store, field, value)
    with pytest.raises(ValueError):
        storage.validate_datastore(store, name=storage.ARTIFACTS_DATASTORE)


@pytest.mark.parametrize("credentials", [
    {"account_key": "synthetic-not-a-secret"}, {"sas_token": "synthetic-not-a-secret"},
])
def test_stored_credentials_are_rejected(credentials):
    from azure.ai.ml.entities import AzureBlobDatastore, AccountKeyConfiguration, SasTokenConfiguration

    kind = AccountKeyConfiguration if "account_key" in credentials else SasTokenConfiguration
    store = AzureBlobDatastore(
        name=storage.ARTIFACTS_DATASTORE, account_name=storage.STORAGE_ACCOUNT,
        container_name=storage.CONTAINERS[storage.ARTIFACTS_DATASTORE],
        credentials=kind(**credentials), tags=scope.TAGS,
    )
    with pytest.raises(ValueError, match="identity access"):
        storage.validate_datastore(store, name=storage.ARTIFACTS_DATASTORE)


def test_unknown_name_or_datastore_kind_is_rejected():
    with pytest.raises(ValueError, match="two reviewed"):
        storage.validate_datastore(None, name="workspaceblobstore")
    with pytest.raises(ValueError, match="storage target"):
        storage.validate_datastore(SimpleNamespace(), name=storage.ARTIFACTS_DATASTORE)


@pytest.mark.parametrize("suffix,name", [
    ("inputs", storage.INPUTS_DATASTORE), ("artifacts", storage.ARTIFACTS_DATASTORE),
])
def test_installed_sdk_uses_caller_identity_without_listing_secrets(suffix, name):
    from azure.ai.ml._artifacts._artifact_utilities import get_datastore_info

    credential = object()  # Not an Azure credential; no token is requested.
    operations = Mock(spec=["get", "get_default", "_list_secrets", "_credential"])
    operations._credential = credential
    operations.get.return_value = azure.ai.ml.load_datastore(PIPELINE / f"datastore-{suffix}.yaml")
    operations.get_default.side_effect = AssertionError("No workspace default")
    operations._list_secrets.side_effect = AssertionError("No stored secrets")
    info = get_datastore_info(operations, name)
    assert info["credential"] is credential
    operations.get.assert_called_once_with(name)
    operations.get_default.assert_not_called()
    operations._list_secrets.assert_not_called()
