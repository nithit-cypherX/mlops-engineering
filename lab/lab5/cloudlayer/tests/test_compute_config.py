"""Check the local compute draft with the installed SDK; never provision it."""
from pathlib import Path
import socket
from unittest.mock import Mock

import azure.ai.ml
import azure.identity
import pytest
import requests
import yaml

from cloudlayer import lab5_scope as scope

CONFIG = Path(__file__).resolve().parents[2] / "pipeline" / "compute.yaml"
IDENTITY_ROOT = scope.SCOPE + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
IDENTITIES = [IDENTITY_ROOT + "id-itcs355-u6688124-lab5-" + role
              for role in ("training", "release")]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    deny = Mock(side_effect=AssertionError("Compute draft check must stay offline"))
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, deny)
    monkeypatch.setattr(requests.sessions.Session, "request", deny)
    monkeypatch.setattr(azure.ai.ml, "MLClient", deny)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", deny)


def test_compute_draft_loads_and_serializes_offline():
    compute = azure.ai.ml.load_compute(CONFIG)
    assert isinstance(compute, azure.ai.ml.entities.AmlCompute)
    body = compute._to_rest_object().as_dict()
    assert body["location"] == scope.REGION
    assert body["properties"]["computeType"] == "AmlCompute"
    assert body["properties"]["disableLocalAuth"] is True
    assert body["properties"]["properties"]["osType"] == "Linux"
    azure.ai.ml.MLClient.assert_not_called()
    azure.identity.DefaultAzureCredential.assert_not_called()


def test_compute_draft_keeps_reviewed_cost_and_network_limits():
    compute = azure.ai.ml.load_compute(CONFIG)
    assert compute.name == "cpu-lab5"
    assert compute.size == "Standard_F2s_v2"
    assert compute.tier == "dedicated"
    assert compute.min_instances == 0
    assert compute.max_instances == 1
    assert compute.idle_time_before_scale_down == 120
    assert compute.ssh_public_access_enabled is False
    assert compute.ssh_settings is None
    assert compute.enable_node_public_ip is True
    assert compute.tags == scope.TAGS
    props = compute._to_rest_object().as_dict()["properties"]["properties"]
    assert props["scaleSettings"] == {
        "maxNodeCount": 1, "minNodeCount": 0, "nodeIdleTimeBeforeScaleDown": "PT2M",
    }
    assert props["remoteLoginPortPublicAccess"] == "Disabled"


def test_compute_attaches_only_the_two_proposed_job_identities():
    source = yaml.safe_load(CONFIG.read_text())
    assert source["identity"] == {
        "type": "user_assigned",
        "user_assigned_identities": [{"resource_id": value} for value in IDENTITIES],
    }
    body = azure.ai.ml.load_compute(CONFIG)._to_rest_object().as_dict()
    assert body["identity"]["type"] == "UserAssigned"
    assert list(body["identity"]["userAssignedIdentities"]) == IDENTITIES
    assert scope.RUNTIME not in body["identity"]["userAssignedIdentities"]
