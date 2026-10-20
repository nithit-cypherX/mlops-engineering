"""ARM-shaped staging tests: no credentials, network, sleep, or real resources."""
from __future__ import annotations

import copy
import json
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import azure.identity
import pytest
import requests

from cloudlayer import azure_staging as staging
from cloudlayer import lab5_scope as scope
from cloudlayer import azure_config as config

CLIENT = "22222222-2222-4222-8222-222222222222"
EXAMPLE = Path(__file__).resolve().parents[2] / "cloud.env.example"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, Mock(side_effect=AssertionError("No network")))
    monkeypatch.setattr(
        requests, "request", Mock(side_effect=AssertionError("Unexpected HTTP")),
    )
    monkeypatch.setattr(
        azure.identity, "DefaultAzureCredential",
        Mock(side_effect=AssertionError("No real credentials")),
    )
    for key in (*config.STAGING_SLOTS, "SERVING_INSTANCE", "SERVING_IMAGE",
                "SERVING_ALLOWED_IP", "MLFLOW_TRACKING_URI"):
        monkeypatch.delenv(key, raising=False)
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(staging.time, "monotonic", lambda: clock.now)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(staging.time, "sleep", sleep)
    monkeypatch.setattr(staging, "DEPLOY_TIMEOUT", 15)


@pytest.fixture
def cfg():
    return replace(
        config.load_staging(EXAMPLE),
        serving_image=staging.ACR_SERVER + "/lab4-serve@sha256:" + "a" * 64,
        serving_allowed_ip="8.8.8.8/32",
        mlflow_tracking_uri=(
            f"azureml://malaysiawest.api.azureml.ms/mlflow/v1.0{scope.SCOPE}"
            f"/providers/Microsoft.MachineLearningServices/workspaces/{staging.WORKSPACE}"
        ),
    )


@pytest.fixture
def registration(tmp_path):
    path = tmp_path / "registration.json"
    path.write_text(json.dumps({
        "name": staging.MODEL_NAME, "version": "7",
        "id": staging.MODEL_ROOT + "/versions/7",
        "model_uri": f"azureml:{staging.MODEL_NAME}:7",
    }))
    return path


@pytest.fixture
def arm(cfg, monkeypatch):
    state = SimpleNamespace(
        calls=[], writes=[], existing=None, polls=[], revisions=[],
        credential=SimpleNamespace(get_token=Mock(return_value=SimpleNamespace(token="fake-unit-token"))),
        environment={
            "id": scope.ENVIRONMENT, "location": "Malaysia West", "tags": {"project": "feedbackpulse"},
            "properties": {"provisioningState": "Succeeded", "workloadProfiles": [
                {"name": "Consumption", "workloadProfileType": "Consumption"},
            ]},
        },
        identity={
            "id": scope.RUNTIME, "location": "malaysiawest", "tags": dict(scope.TAGS),
            "properties": {"clientId": CLIENT, "principalId": "11111111-1111-4111-8111-111111111111"},
        },
        ready={
            "id": scope.APP, "tags": dict(scope.TAGS), "location": "malaysiawest",
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {scope.RUNTIME: {}}},
            "properties": {
                "environmentId": scope.ENVIRONMENT, "provisioningState": "Succeeded",
                "latestRevisionName": "lab5-staging--new",
                "configuration": {"activeRevisionsMode": "Single",
                                  "registries": [{"server": staging.ACR_SERVER, "identity": scope.RUNTIME}],
                                  "ingress": {
                    "fqdn": "lab5-staging.example.malaysiawest.azurecontainerapps.io",
                    "allowInsecure": False, "targetPort": 8080, "external": True,
                    "ipSecurityRestrictions": [
                        {"name": "lab5-client", "action": "Allow", "ipAddressRange": "8.8.8.8/32"},
                    ],
                }},
            },
        },
        revision={
            "name": "lab5-staging--new",
            "properties": {
                "active": True, "provisioningState": "Provisioned", "healthState": "Healthy",
                "runningState": "Running", "trafficWeight": 100, "replicas": 1,
                "template": {"containers": [{
                    "name": "api", "image": cfg.serving_image,
                    "env": [{"name": key, "value": value} for key, value in {
                        "CLOUD_PROVIDER": "azure", "MLFLOW_TRACKING_URI": cfg.mlflow_tracking_uri,
                        "MODEL_REGISTRY_NAME": staging.MODEL_NAME, "MODEL_VERSION": "7",
                        "AZURE_CLIENT_ID": CLIENT,
                    }.items()],
                }]},
            },
        },
    )

    def request(method, url, **kwargs):
        state.calls.append((method, url, kwargs))
        assert url.startswith("https://management.azure.com" + scope.SCOPE + "/providers/")
        assert kwargs["allow_redirects"] is False
        assert 0 < kwargs["timeout"] <= 90
        assert kwargs["headers"] == {"Authorization": "Bearer fake-unit-token"}
        status = 200
        if scope.ENVIRONMENT + "?" in url and method == "GET":
            payload = state.environment
        elif scope.RUNTIME + "?" in url and method == "GET":
            assert url.endswith("?api-version=2023-01-31")
            payload = state.identity
        elif scope.APP + "/revisions/lab5-staging--new?" in url and method == "GET":
            payload = state.revisions.pop(0) if state.revisions else state.revision
        elif scope.APP + "?" in url and method == "GET":
            payload = (state.polls.pop(0) if state.polls else state.ready) if state.writes else state.existing
            if payload is None:
                status, payload = 404, {"error": {"code": "ResourceNotFound"}}
        elif scope.APP + "?" in url and method == "PUT":
            state.writes.append(copy.deepcopy(kwargs["json"]))
            status, payload = 201, {"properties": {"provisioningState": "InProgress"}}
        else:
            raise AssertionError(f"Out-of-scope operation: {method} {url}")
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(status_code=status, content=b"json", json=lambda: copy.deepcopy(payload))

    monkeypatch.setattr(requests, "request", request)
    return state


def deploy(registration, cfg, arm):
    return staging.deploy_staging(registration, cfg, credential=arm.credential)


@pytest.mark.parametrize("running_state", ["Running", "RunningAtMaxScale"])
def test_scoped_payload_and_exact_registered_version(cfg, registration, arm, running_state):
    arm.revision["properties"]["runningState"] = running_state
    before_env, before_id = copy.deepcopy(arm.environment), copy.deepcopy(arm.identity)
    result = deploy(registration, cfg, arm)
    assert result["status"] == "control_plane_verified"
    assert result["model"] == json.loads(registration.read_text())
    assert result["revision"] == "lab5-staging--new"
    assert result["serving_image"] == cfg.serving_image
    assert result["revision_state"] == "healthy"
    assert result["http_smoke_tested"] is False
    assert len(arm.writes) == 1
    body = arm.writes[0]
    assert body["tags"] == scope.TAGS
    assert body["identity"] == {"type": "UserAssigned", "userAssignedIdentities": {scope.RUNTIME: {}}}
    props = body["properties"]
    assert props["environmentId"] == scope.ENVIRONMENT
    assert props["workloadProfileName"] == "Consumption"
    settings = props["configuration"]
    assert settings["activeRevisionsMode"] == "Single"
    assert settings["registries"] == [{"server": staging.ACR_SERVER, "identity": scope.RUNTIME}]
    assert "secrets" not in settings
    ingress = settings["ingress"]
    assert ingress["external"] is True and ingress["allowInsecure"] is False
    assert ingress["targetPort"] == 8080
    assert ingress["traffic"] == [{"latestRevision": True, "weight": 100}]
    assert ingress["ipSecurityRestrictions"] == [
        {"name": "lab5-client", "action": "Allow", "ipAddressRange": "8.8.8.8/32"},
    ]
    template = props["template"]
    assert template["scale"]["minReplicas"] == 0 and template["scale"]["maxReplicas"] == 1
    container, = template["containers"]
    assert container["image"] == cfg.serving_image
    assert container["resources"] == {"cpu": 0.5, "memory": "1Gi"}
    assert container["args"][-2:] == ["--workers", "1"]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["MODEL_VERSION"] == "7" and env["MODEL_REGISTRY_NAME"] == staging.MODEL_NAME
    assert env["AZURE_CLIENT_ID"] == CLIENT
    assert {p["type"]: p["httpGet"]["path"] for p in container["probes"]} == {
        "Startup": "/ready", "Readiness": "/ready", "Liveness": "/health",
    }
    assert [method for method, _, _ in arm.calls] == ["GET", "GET", "GET", "PUT", "GET", "GET"]
    assert arm.environment == before_env and arm.identity == before_id


@pytest.mark.parametrize("field, value", [
    ("endpoint_name", "feedbackpulse-api"), ("azure_containerapps_environment", "other-env"),
    ("azure_managed_identity_id", scope.RUNTIME + "-other"),
    ("serving_image", ""), ("serving_image", staging.ACR_SERVER + "/serve:latest"),
    ("serving_image", "other.azurecr.io/serve@sha256:" + "a" * 64),
    ("serving_allowed_ip", ""), ("serving_allowed_ip", "0.0.0.0/0"),
    ("serving_allowed_ip", "8.8.8.0/24"), ("serving_allowed_ip", "127.0.0.1/32"),
    ("serving_allowed_ip", "::1/128"), ("mlflow_tracking_uri", ""),
    ("mlflow_tracking_uri", "azureml://another-workspace"),
])
def test_invalid_settings_fail_before_token_or_http(cfg, registration, arm, field, value):
    with pytest.raises(ValueError):
        deploy(registration, replace(cfg, **{field: value}), arm)
    assert not arm.calls
    arm.credential.get_token.assert_not_called()


@pytest.mark.parametrize("field, value", [
    ("version", "latest"), ("version", 7), ("version", "0"),
    ("name", "production"), ("model_uri", "models:/other/latest"), ("id", "/other/workspace/model/7"),
])
def test_bad_registration_never_deploys(cfg, registration, arm, field, value):
    document = json.loads(registration.read_text())
    document[field] = value
    registration.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        deploy(registration, cfg, arm)
    assert not arm.calls


@pytest.mark.parametrize("payload", ["{", "[]", "null", "{}"])
def test_malformed_registration_never_deploys(cfg, registration, arm, payload):
    registration.write_text(payload)
    with pytest.raises(ValueError):
        deploy(registration, cfg, arm)
    assert not arm.calls


@pytest.mark.parametrize("problem", ["env_id", "env_failed", "env_profile", "identity_id", "identity_tags", "identity_client"])
def test_wrong_prerequisite_stops_before_write(cfg, registration, arm, problem):
    if problem == "env_id":
        arm.environment["id"] += "-other"
    elif problem == "env_failed":
        arm.environment["properties"]["provisioningState"] = "Failed"
    elif problem == "env_profile":
        arm.environment["properties"]["workloadProfiles"] = []
    elif problem == "identity_id":
        arm.identity["id"] += "-other"
    elif problem == "identity_tags":
        arm.identity["tags"] = {}
    else:
        arm.identity["properties"]["clientId"] = "not-a-uuid"
    with pytest.raises(ValueError):
        deploy(registration, cfg, arm)
    assert not arm.writes


def test_existing_owned_app_keeps_extra_tags(cfg, registration, arm):
    arm.existing = copy.deepcopy(arm.ready)
    arm.existing["tags"]["purpose"] = "keep-me"
    deploy(registration, cfg, arm)
    assert arm.writes[0]["tags"]["purpose"] == "keep-me"


@pytest.mark.parametrize("problem", ["id", "tags", "environment", "mode", "pending"])
def test_unapproved_existing_app_is_not_overwritten(cfg, registration, arm, problem):
    arm.existing = copy.deepcopy(arm.ready)
    if problem == "id":
        arm.existing["id"] = scope.APP.replace("lab5-staging", "feedbackpulse-api")
    elif problem == "tags":
        arm.existing["tags"]["lab"] = "4"
    elif problem == "environment":
        arm.existing["properties"]["environmentId"] += "-other"
    elif problem == "mode":
        arm.existing["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
    else:
        arm.existing["properties"]["provisioningState"] = "InProgress"
    with pytest.raises(ValueError):
        deploy(registration, cfg, arm)
    assert not arm.writes


def test_provisioning_success_waits_for_matching_revision(cfg, registration, arm):
    waiting = copy.deepcopy(arm.revision)
    waiting["properties"]["provisioningState"] = "Provisioning"
    arm.revisions = [waiting, arm.revision]
    assert deploy(registration, cfg, arm)["revision_state"] == "healthy"
    assert len(arm.writes) == 1
    assert len([url for _, url, _ in arm.calls if "/revisions/" in url]) == 2


@pytest.mark.parametrize("health_state", [None, "None", "Unhealthy"])
def test_running_at_max_scale_requires_healthy(cfg, registration, arm, health_state):
    arm.revision["properties"].update(runningState="RunningAtMaxScale", healthState=health_state)
    with pytest.raises(TimeoutError, match="no automatic delete or redeploy"):
        deploy(registration, cfg, arm)
    assert len(arm.writes) == 1


@pytest.mark.parametrize("running_state", ["Running", "RunningAtMaxScale", "ScaledToZero"])
@pytest.mark.parametrize("problem", [
    "image", "version", "tracking", "identity", "traffic", "ip_rules", "attachment", "registry", "ingress",
])
def test_wrong_revision_or_access_times_out_without_redeploy(cfg, registration, arm, problem, running_state):
    props = arm.revision["properties"]
    props["runningState"] = running_state
    if running_state == "ScaledToZero":
        props["replicas"] = 0
    api = props["template"]["containers"][0]
    if problem == "image":
        api["image"] = cfg.serving_image.replace("a" * 64, "b" * 64)
    elif problem in {"version", "tracking", "identity"}:
        key = {"version": "MODEL_VERSION", "tracking": "MLFLOW_TRACKING_URI", "identity": "AZURE_CLIENT_ID"}[problem]
        next(entry for entry in api["env"] if entry["name"] == key)["value"] = "wrong"
    elif problem == "traffic":
        props["trafficWeight"] = 0
    elif problem == "attachment":
        arm.ready["identity"]["userAssignedIdentities"] = {}
    elif problem == "registry":
        arm.ready["properties"]["configuration"]["registries"] = []
    elif problem == "ingress":
        arm.ready["properties"]["configuration"]["ingress"]["external"] = False
    else:
        arm.ready["properties"]["configuration"]["ingress"]["ipSecurityRestrictions"] = []
    with pytest.raises(TimeoutError, match="no automatic delete or redeploy"):
        deploy(registration, cfg, arm)
    assert len(arm.writes) == 1


@pytest.mark.parametrize("target", ["app", "revision"])
def test_failed_deployment_is_not_success(cfg, registration, arm, target):
    resource = arm.ready if target == "app" else arm.revision
    resource["properties"]["provisioningState"] = "Failed"
    with pytest.raises(RuntimeError, match="failed"):
        deploy(registration, cfg, arm)
    assert len(arm.writes) == 1


@pytest.mark.parametrize("health_state", [None, "None", "Healthy"])
def test_scaled_to_zero_is_not_reported_as_http_ready(cfg, registration, arm, health_state):
    arm.revision["properties"].update(runningState="ScaledToZero", healthState=health_state, replicas=0)
    result = deploy(registration, cfg, arm)
    assert result["status"] == "control_plane_verified"
    assert result["revision_state"] == "scaled_to_zero"
    assert result["http_smoke_tested"] is False
    assert len(arm.writes) == 1
    assert staging.time.monotonic() == 0


@pytest.mark.parametrize("health_state, replicas", [
    ("Healthy", 1), ("Healthy", None), ("Unhealthy", 0), ("Unknown", 0),
])
def test_scaled_to_zero_rejects_unexpected_health_or_replicas(cfg, registration, arm, health_state, replicas):
    arm.revision["properties"].update(runningState="ScaledToZero", healthState=health_state, replicas=replicas)
    with pytest.raises(TimeoutError, match="no automatic delete or redeploy"):
        deploy(registration, cfg, arm)
    assert len(arm.writes) == 1


def test_readiness_timeout_can_recover_without_another_put(cfg, registration, arm):
    arm.polls = [requests.Timeout("simulated read timeout"), arm.ready]
    assert deploy(registration, cfg, arm)["revision_state"] == "healthy"
    assert len(arm.writes) == 1


@pytest.mark.parametrize("status, code", [(403, "AuthorizationFailed"), (404, "AuthorizationFailed"), (302, "Redirect"), (500, "InternalError")])
def test_http_errors_never_mean_missing_or_trigger_write(monkeypatch, cfg, registration, arm, status, code):
    original = requests.request

    def request(method, url, **kwargs):
        if scope.APP + "?" in url:
            return SimpleNamespace(status_code=status, content=b"json", json=lambda: {"error": {"code": code}})
        return original(method, url, **kwargs)

    monkeypatch.setattr(requests, "request", request)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        deploy(registration, cfg, arm)
    assert not arm.writes


def test_put_timeout_is_never_retried(cfg, registration, arm, monkeypatch):
    original = requests.request
    writes = Mock(side_effect=requests.Timeout("write outcome unknown"))

    def request(method, url, **kwargs):
        if method == "PUT":
            return writes()
        return original(method, url, **kwargs)

    monkeypatch.setattr(requests, "request", request)
    with pytest.raises(RuntimeError, match="Inspect before retrying"):
        deploy(registration, cfg, arm)
    writes.assert_called_once()


@pytest.mark.parametrize("running_state", ["Running", "ScaledToZero"])
def test_cli_writes_verified_deployment_file_only(cfg, registration, arm, tmp_path, monkeypatch, running_state):
    monkeypatch.setattr(staging, "load_staging", lambda _: cfg)
    arm.revision["properties"]["runningState"] = running_state
    if running_state == "ScaledToZero":
        arm.revision["properties"]["replicas"] = 0
    output = tmp_path / "deploy"
    assert staging.main([
        "--registration", str(registration), "--output-dir", str(output),
    ], credential=arm.credential) == 0
    result = json.loads((output / "deployment.json").read_text())
    assert result["status"] == "control_plane_verified"
    assert result["model"]["version"] == "7"
    assert result["revision_state"] == ("scaled_to_zero" if running_state == "ScaledToZero" else "healthy")
    assert result["http_smoke_tested"] is False
    assert len(arm.writes) == 1
    azure.identity.DefaultAzureCredential.assert_not_called()


def test_cli_bad_config_never_creates_credentials(registration, tmp_path):
    output = tmp_path / "deploy"
    assert staging.main([
        "--registration", str(registration), "--env-file", str(EXAMPLE), "--output-dir", str(output),
    ]) == 1
    azure.identity.DefaultAzureCredential.assert_not_called()
    assert not (output / "deployment.json").exists()


def test_cli_preserves_existing_output_before_any_cloud_call(cfg, registration, arm, tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "load_staging", lambda _: cfg)
    output = tmp_path / "deploy"
    output.mkdir()
    old = output / "deployment.json"
    old.write_text("old result")
    assert staging.main([
        "--registration", str(registration), "--output-dir", str(output),
    ], credential=arm.credential) == 1
    assert not arm.calls
    assert old.read_text() == "old result"


@pytest.mark.parametrize("running_state", ["Failed", "ActivationFailed"])
def test_cli_failed_revision_never_writes_success(cfg, registration, arm, tmp_path, monkeypatch, capsys, running_state):
    monkeypatch.setattr(staging, "load_staging", lambda _: cfg)
    arm.revision["properties"]["runningState"] = running_state
    output = tmp_path / "deploy"
    assert staging.main([
        "--registration", str(registration), "--output-dir", str(output),
    ], credential=arm.credential) == 1
    assert not (output / "deployment.json").exists()
    assert staging.time.monotonic() == 0
    assert len(arm.writes) == 1
    assert len([url for _, url, _ in arm.calls if "/revisions/" in url]) == 1
    captured = capsys.readouterr()
    assert "Staging revision failed" in captured.err
    assert not captured.out


def test_cli_requires_registration():
    with pytest.raises(SystemExit) as exc:
        staging.main([])
    assert exc.value.code == 2
    azure.identity.DefaultAzureCredential.assert_not_called()


def test_cli_output_failure_does_not_repeat_deployment(cfg, registration, arm, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(staging, "load_staging", lambda _: cfg)
    original = Path.open
    output = tmp_path / "deploy"

    def open_checked(path, *args, **kwargs):
        if path == output / "deployment.json":
            raise OSError("simulated full disk")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_checked)
    assert staging.main([
        "--registration", str(registration), "--output-dir", str(output),
    ], credential=arm.credential) == 1
    assert len(arm.writes) == 1
    captured = capsys.readouterr()
    assert "Deployment was verified but output was not saved" in captured.err
    assert not captured.out
