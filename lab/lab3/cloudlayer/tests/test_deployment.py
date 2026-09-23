"""Provider-specific deployment tests belong beside the Azure adapter.

All CLI and HTTP boundaries are replaced; no cloud resource or model is accessed.
"""
import copy
import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from cloudlayer import azure
from cloudlayer.azure import AzureAdapter
from src import config

SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
CLIENT = "00000000-0000-0000-0000-000000000003"
SCOPE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/test-lab"
IDENTITY = f"{SCOPE}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/lab3-id"
ENVIRONMENT = f"{SCOPE}/providers/Microsoft.App/managedEnvironments/lab3-env"
URL = "https://lab3-api.test.malaysiawest.azurecontainerapps.io"
MODEL = "models:/test-model/1"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected external call or real polling sleep")
    monkeypatch.setattr(azure.subprocess, "run", unexpected)
    monkeypatch.setattr(azure.requests, "post", unexpected)
    monkeypatch.setattr(azure.time, "sleep", unexpected)


@pytest.fixture
def cfg():
    return config.Config(
        provider="azure", project_id="test-lab", region="malaysiawest", blob_uri="",
        container_registry="example.azurecr.io/lab3",
        mlflow_tracking_uri=f"azureml://malaysiawest.api.azureml.ms/mlflow/v1.0{SCOPE}/providers/Microsoft.MachineLearningServices/workspaces/test-workspace",
        model_registry_name="test-model", identity_ref=PRINCIPAL,
        azure_subscription_id=SUBSCRIPTION, azure_ml_workspace="test-workspace", model_version="1",
        serving_image="example.azurecr.io/lab3@sha256:" + "a" * 64,
        endpoint_name="lab3-api", azure_containerapps_environment="lab3-env",
        azure_managed_identity_id=IDENTITY, serving_allowed_ip="8.8.8.8/32",
    )


@pytest.fixture
def arm(cfg, monkeypatch):
    state = SimpleNamespace(
        calls=[], bodies=[], existing=None, polls=[],
        environment={"location": "malaysiawest", "tags": cfg.tags(3), "properties": {
            "provisioningState": "Succeeded",
            "workloadProfiles": [{"name": "Consumption", "workloadProfileType": "Consumption"}],
        }},
        identity={"tags": cfg.tags(3), "properties": {"principalId": PRINCIPAL, "clientId": CLIENT}},
        ready={"tags": cfg.tags(3), "properties": {
            "environmentId": ENVIRONMENT, "provisioningState": "Succeeded",
            "latestRevisionName": "lab3-api--new", "latestReadyRevisionName": "lab3-api--new",
            "configuration": {"activeRevisionsMode": "Single", "ingress": {"fqdn": URL.removeprefix("https://")}},
        }},
    )
    state.put_result = copy.deepcopy(state.ready)

    def run(command, **kwargs):
        state.calls.append((command, kwargs))
        assert command[0] == "az"
        assert kwargs["check"] and kwargs["capture_output"] and kwargs["text"]
        assert kwargs["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"
        assert kwargs["timeout"] == 90
        assert "--only-show-errors" in command and command[-2:] == ["--output", "json"]
        if command[1:3] == ["resource", "list"]:
            assert command[command.index("--subscription") + 1] == SUBSCRIPTION
            assert command[command.index("--resource-group") + 1] == "test-lab"
            result = [{"name": "lab3-api"}] if state.existing else []
        else:
            assert command[1] == "rest"
            method = command[command.index("--method") + 1]
            url = command[command.index("--url") + 1]
            assert url.startswith(f"https://management.azure.com{SCOPE}/providers/")
            if method == "get" and "/managedEnvironments/" in url:
                result = state.environment
            elif method == "get" and "/userAssignedIdentities/" in url:
                result = state.identity
            elif method == "put" and "/containerApps/lab3-api?" in url:
                state.bodies.append(json.loads(command[command.index("--body") + 1]))
                result = state.put_result
            elif method == "get" and "/containerApps/lab3-api?" in url:
                result = state.polls.pop(0) if state.bodies else state.existing
                assert result is not None
            else:
                raise AssertionError(f"Unexpected Azure operation: {method} {url}")
        return SimpleNamespace(stdout=json.dumps(result))

    monkeypatch.setattr(azure.subprocess, "run", run)
    return state


@pytest.mark.parametrize("location", ["malaysiawest", "Malaysia West"])
def test_deploy_builds_scoped_digest_pinned_identity_config(cfg, arm, location):
    arm.environment["location"] = location
    assert AzureAdapter(cfg).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance) == URL
    assert len(arm.bodies) == 1
    body = arm.bodies[0]
    assert body["tags"] == cfg.tags(3)
    assert body["identity"] == {"type": "UserAssigned", "userAssignedIdentities": {IDENTITY: {}}}
    props = body["properties"]
    assert props["environmentId"] == ENVIRONMENT and props["workloadProfileName"] == "Consumption"
    deployment = props["configuration"]
    assert deployment["registries"] == [{"server": "example.azurecr.io", "identity": IDENTITY}]
    assert "secrets" not in deployment
    ingress = deployment["ingress"]
    assert ingress["external"] and not ingress["allowInsecure"] and ingress["targetPort"] == 8080
    assert ingress["ipSecurityRestrictions"] == [{"name": "lab3-client", "action": "Allow", "ipAddressRange": "8.8.8.8/32"}]
    assert deployment["activeRevisionsMode"] == "Single"
    assert ingress["traffic"] == [{"latestRevision": True, "weight": 100}]
    template = props["template"]
    assert template["scale"]["minReplicas"] == 0 and template["scale"]["maxReplicas"] == 1
    container, = template["containers"]
    assert container["image"] == cfg.serving_image
    assert container["resources"] == {"cpu": 0.5, "memory": "1Gi"}
    assert container["command"] == ["uvicorn"] and container["args"][-2:] == ["--workers", "1"]
    assert {entry["name"]: entry["value"] for entry in container["env"]} == {
        "CLOUD_PROVIDER": "azure", "MLFLOW_TRACKING_URI": cfg.mlflow_tracking_uri,
        "MODEL_REGISTRY_NAME": "test-model", "MODEL_VERSION": "1", "AZURE_CLIENT_ID": CLIENT,
    }
    probes = {probe["type"]: probe for probe in container["probes"]}
    assert probes["Startup"]["httpGet"] == {"path": "/ready", "port": 8080}
    assert probes["Startup"]["periodSeconds"] * probes["Startup"]["failureThreshold"] == 300
    assert probes["Readiness"]["httpGet"]["path"] == "/ready"
    assert probes["Liveness"]["httpGet"]["path"] == "/health"
    # The exact boundary sequence excludes grants, image builds and environment creation.
    assert [call[0][1:3] for call in arm.calls] == [
        ["rest", "--method"], ["rest", "--method"], ["resource", "list"], ["rest", "--method"],
    ]


@pytest.mark.parametrize("field,value", [
    ("serving_image", "example.azurecr.io/lab3:latest"),
    ("serving_image", "other.azurecr.io/lab3@sha256:" + "a" * 64),
    ("azure_subscription_id", "../other"), ("project_id", "test/other"),
    ("serving_allowed_ip", "0.0.0.0/0"), ("serving_allowed_ip", "8.8.8.0/24"),
    ("serving_allowed_ip", "127.0.0.1/32"), ("serving_allowed_ip", ""),
    ("azure_managed_identity_id", IDENTITY.replace("test-lab", "other-rg")),
    ("identity_ref", "not-a-principal-id"), ("region", "../region"),
    ("mlflow_tracking_uri", "azureml://other-workspace"),
    ("azure_containerapps_environment", "../other-env"),
])
def test_invalid_config_is_rejected_before_any_cli_call(cfg, arm, field, value):
    with pytest.raises(ValueError):
        AzureAdapter(replace(cfg, **{field: value})).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance)
    assert not arm.calls


@pytest.mark.parametrize("model,endpoint,instance", [
    ("models:/test-model/latest", "lab3-api", "0.5cpu-1Gi"),
    ("models:/test-model/2", "lab3-api", "0.5cpu-1Gi"),
    (MODEL, "../other", "0.5cpu-1Gi"), (MODEL, "lab3-api", "Standard_DS3_v2"),
])
def test_invalid_arguments_are_rejected_before_cli(cfg, arm, model, endpoint, instance):
    with pytest.raises(ValueError):
        AzureAdapter(cfg).deploy(model, endpoint, instance)
    assert not arm.calls


@pytest.mark.parametrize("failure", ["environment-owner", "environment-not-ready", "region", "profile", "identity-owner", "principal"])
def test_invalid_prerequisites_never_create_app(cfg, arm, failure):
    if failure == "environment-owner":
        arm.environment["tags"]["lab"] = "2"
    elif failure == "environment-not-ready":
        arm.environment["properties"]["provisioningState"] = "Creating"
    elif failure == "region":
        arm.environment["location"] = "eastus"
    elif failure == "profile":
        arm.environment["properties"]["workloadProfiles"] = []
    elif failure == "identity-owner":
        arm.identity["tags"] = {}
    else:
        arm.identity["properties"]["principalId"] = CLIENT
    with pytest.raises(ValueError):
        AzureAdapter(cfg).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance)
    assert not arm.bodies


@pytest.mark.parametrize("failure", ["tags", "environment", "canary"])
def test_existing_app_cannot_be_overwritten_outside_task2(cfg, arm, failure):
    arm.existing = copy.deepcopy(arm.ready)
    if failure == "tags":
        arm.existing["tags"]["lab"] = "2"
    elif failure == "environment":
        arm.existing["properties"]["environmentId"] = "another-environment"
    else:
        arm.existing["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
    with pytest.raises(ValueError):
        AzureAdapter(cfg).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance)
    assert not arm.bodies


def test_existing_lab3_app_can_be_updated_without_removing_other_tags(cfg, arm):
    arm.existing = copy.deepcopy(arm.ready)
    arm.existing["tags"]["note"] = "keep"
    assert AzureAdapter(cfg).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance) == URL
    assert arm.bodies[0]["tags"]["note"] == "keep"


def test_poll_waits_for_new_revision_not_previous_ready_revision(cfg, arm, monkeypatch):
    arm.put_result["properties"]["latestReadyRevisionName"] = "lab3-api--old"
    arm.polls = [arm.ready]
    sleep = Mock()
    monkeypatch.setattr(azure.time, "sleep", sleep)
    assert AzureAdapter(cfg).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance) == URL
    sleep.assert_called_once_with(5)


@pytest.mark.parametrize("status", ["Failed", "Canceled"])
def test_failed_deployment_is_not_reported_ready(cfg, arm, status):
    arm.put_result["properties"]["provisioningState"] = status
    with pytest.raises(RuntimeError, match="Deployment failed"):
        AzureAdapter(cfg).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance)
    assert len(arm.bodies) == 1


def test_timeout_does_not_retry_deployment_or_delete_resource(cfg, arm, monkeypatch):
    arm.put_result["properties"]["provisioningState"] = "InProgress"
    monkeypatch.setattr(azure.time, "monotonic", Mock(side_effect=[0, 601]))
    with pytest.raises(TimeoutError, match="no auto-delete"):
        AzureAdapter(cfg).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance)
    assert len(arm.bodies) == 1


def test_cli_failure_stops_without_fallback_or_role_changes(cfg, monkeypatch):
    failure = subprocess.CalledProcessError(1, ["az", "rest"], stderr="AuthorizationFailed")
    runner = Mock(side_effect=failure)
    monkeypatch.setattr(azure.subprocess, "run", runner)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        AzureAdapter(cfg).deploy(MODEL, cfg.endpoint_name, cfg.serving_instance)
    assert raised.value is failure
    assert runner.call_count == 1


def test_invoke_resolves_name_and_preserves_payload(cfg, arm, monkeypatch):
    arm.existing = arm.ready
    payload = {"example": 1}
    result = {"probability": 0.25, "model_version": "1"}
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: result))
    monkeypatch.setattr(azure.requests, "post", post)
    assert AzureAdapter(cfg).invoke(cfg.endpoint_name, payload) == result
    post.assert_called_once_with(URL + "/predict", json=payload, timeout=(10, 240), allow_redirects=False)
    assert len(arm.calls) == 1 and not arm.bodies


def test_invoke_accepts_deploy_url_without_cli(cfg, monkeypatch):
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"model_version": "1"}))
    monkeypatch.setattr(azure.requests, "post", post)
    assert AzureAdapter(cfg).invoke(URL + "/", {}) == {"model_version": "1"}


@pytest.mark.parametrize("endpoint", ["http://example.com", URL + "?token=secret", URL + "/other", "https://user:password@host.azurecontainerapps.io", "https://host.azurecontainerapps.io.evil.test"])
def test_invoke_rejects_unsafe_urls_without_network(cfg, endpoint):
    with pytest.raises(ValueError):
        AzureAdapter(cfg).invoke(endpoint, {})


@pytest.mark.parametrize("status", [301, 401, 403, 422, 503])
def test_invoke_surfaces_http_failure_without_retry(cfg, monkeypatch, status):
    post = Mock(return_value=SimpleNamespace(status_code=status))
    monkeypatch.setattr(azure.requests, "post", post)
    with pytest.raises(RuntimeError, match=str(status)):
        AzureAdapter(cfg).invoke(URL, {})
    assert post.call_count == 1


def test_invoke_preserves_timeout(cfg, monkeypatch):
    monkeypatch.setattr(azure.requests, "post", Mock(side_effect=requests.Timeout("timed out")))
    with pytest.raises(requests.Timeout):
        AzureAdapter(cfg).invoke(URL, {})


@pytest.mark.parametrize("body", [[], "not-an-object", None])
def test_invoke_rejects_non_object_json(cfg, monkeypatch, body):
    monkeypatch.setattr(azure.requests, "post", Mock(return_value=SimpleNamespace(status_code=200, json=lambda: body)))
    with pytest.raises(ValueError, match="JSON object"):
        AzureAdapter(cfg).invoke(URL, {})


def test_deployment_config_needs_no_blob_setting(cfg, monkeypatch):
    for key in (*config.CAPABILITY_SLOTS, *config.AZURE_WORKSPACE_SLOTS, *config.DEPLOYMENT_SLOTS, "MODEL_VERSION", "SERVING_INSTANCE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "CLOUD_PROVIDER": cfg.provider, "PROJECT_ID": cfg.project_id, "REGION": cfg.region,
        "CONTAINER_REGISTRY": cfg.container_registry, "MLFLOW_TRACKING_URI": cfg.mlflow_tracking_uri,
        "MODEL_REGISTRY_NAME": cfg.model_registry_name, "MODEL_VERSION": cfg.model_version,
        "IDENTITY_REF": cfg.identity_ref, "AZURE_SUBSCRIPTION_ID": cfg.azure_subscription_id,
        "AZURE_ML_WORKSPACE": cfg.azure_ml_workspace, "SERVING_IMAGE": cfg.serving_image,
        "ENDPOINT_NAME": cfg.endpoint_name, "AZURE_CONTAINERAPPS_ENVIRONMENT": cfg.azure_containerapps_environment,
        "AZURE_MANAGED_IDENTITY_ID": cfg.azure_managed_identity_id, "SERVING_ALLOWED_IP": cfg.serving_allowed_ip,
    }.items():
        monkeypatch.setenv(key, value)
    assert config.load_deployment() == cfg
    monkeypatch.delenv("SERVING_ALLOWED_IP")
    with pytest.raises(RuntimeError, match="SERVING_ALLOWED_IP"):
        config.load_deployment()
