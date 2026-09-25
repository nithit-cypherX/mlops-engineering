"""Azure-specific fake ARM/registry/HTTP contracts for Task 4; no live writes."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import mlflow.tracking
import pytest

from cloudlayer import azure
from cloudlayer.azure import AzureAdapter
from cloudlayer.tests.test_deployment import cfg, CLIENT, ENVIRONMENT, URL  # noqa: F401


@pytest.fixture
def rig(cfg, monkeypatch):
    adapter = AzureAdapter(cfg)
    baseline = cfg.endpoint_name + "--0000003"
    template = {"containers": [{"name": "api", "image": cfg.serving_image,
        "resources": {"cpu": 0.5, "memory": "1Gi"}, "command": ["uvicorn"],
        "args": ["service.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"],
        "env": [{"name": k, "value": v} for k, v in {
            "CLOUD_PROVIDER": "azure", "MODEL_VERSION": "1", "AZURE_CLIENT_ID": CLIENT,
            "MLFLOW_TRACKING_URI": cfg.mlflow_tracking_uri,
            "MODEL_REGISTRY_NAME": cfg.model_registry_name}.items()],
        "probes": [{"type": "Readiness", "httpGet": {"path": "/ready", "port": 8080}}]}],
        "scale": {"minReplicas": 0, "maxReplicas": 1}}
    app = {"location": cfg.region, "tags": cfg.tags(3), "properties": {
        "environmentId": ENVIRONMENT, "runningStatus": "Stopped", "provisioningState": "Succeeded",
        "latestRevisionName": baseline, "configuration": {"activeRevisionsMode": "Single",
            "ingress": {"fqdn": URL.removeprefix("https://"), "allowInsecure": False,
                "traffic": [{"latestRevision": True, "weight": 100}],
                "ipSecurityRestrictions": [{"action": "Allow", "ipAddressRange": cfg.serving_allowed_ip}]}}}}
    revisions = [{"name": baseline, "properties": {"active": True, "replicas": 0,
        "fqdn": "old.test.azurecontainerapps.io", "template": template}}]
    state = SimpleNamespace(adapter=adapter, app=app, revisions=revisions, calls=[], fail=None)

    def run(command, **kwargs):
        assert command[:2] == ["az", "rest"] and kwargs["check"]
        assert kwargs["timeout"] <= 30 and kwargs["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"
        method = command[command.index("--method") + 1]
        url = command[command.index("--url") + 1]
        root, query = adapter._app_url(cfg.endpoint_name).split("?", 1)
        assert url.startswith(root) and url.endswith("?" + query)
        path = url[len(root):].split("?")[0]
        body = json.loads(command[command.index("--body") + 1]) if "--body" in command else None
        state.calls.append((method, path, body))
        if state.fail and state.fail(method, path, body):
            raise RuntimeError("fake operation failure")
        if method == "get":
            result = {"value": state.revisions} if path == "/revisions" else state.app
            return SimpleNamespace(stdout=json.dumps(result))
        if method == "patch":
            props = body["properties"]
            if "configuration" in props:
                state.app["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
                state.app["properties"]["configuration"]["ingress"]["traffic"] = props["configuration"]["ingress"]["traffic"]
            else:
                name = cfg.endpoint_name + "--" + props["template"]["revisionSuffix"]
                state.revisions.append({"name": name, "properties": {"active": True, "replicas": 0,
                    "fqdn": "new.test.azurecontainerapps.io", "template": props["template"]}})
        elif path == "/start":
            state.app["properties"]["runningStatus"] = "Running"
            for rev in state.revisions:
                rev["properties"]["replicas"] = 1
        elif path == "/stop":
            state.app["properties"]["runningStatus"] = "Stopped"
            for rev in state.revisions:
                rev["properties"]["replicas"] = 0
        elif path.endswith("/deactivate"):
            name = path.split("/")[2]
            next(r for r in state.revisions if r["name"] == name)["properties"]["active"] = False
        else:
            raise AssertionError("Unexpected fake ARM operation")
        return SimpleNamespace(stdout="")  # Real POST/PATCH responses can be empty.

    monkeypatch.setattr(azure.subprocess, "run", run)
    monkeypatch.setattr(azure.time, "sleep", Mock(side_effect=AssertionError("Unexpected polling")))
    monkeypatch.setattr(azure.requests, "get", lambda url, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {"model_version": "1" if "//old." in url else "2"}))
    monkeypatch.setattr(azure.requests, "post", Mock(side_effect=AssertionError("Unexpected HTTP")))
    registry = Mock()
    registry.get_model_version.side_effect = lambda name, version: SimpleNamespace(status="READY", run_id=version)
    monkeypatch.setattr(mlflow.tracking, "MlflowClient", Mock(return_value=registry))
    state.registry = registry
    state.plan = lambda: adapter.canary_plan(baseline, "2", "cny-123456")
    return state


def test_preflight_read_only_and_template_changes_only_version_suffix(rig):
    original = copy.deepcopy(rig.revisions[0]["properties"]["template"])
    plan = rig.plan()
    assert all(method == "get" for method, _, _ in rig.calls)
    template = copy.deepcopy(plan["template"])
    assert template.pop("revisionSuffix") == "cny-123456"
    for env in template["containers"][0]["env"]:
        if env["name"] == "MODEL_VERSION":
            assert env["value"] == "2"
            env["value"] = "1"
    assert template == original


@pytest.mark.parametrize("change", ["tags", "running", "replicas", "mode", "image", "cpu", "ingress"])
def test_preflight_refuses_unsafe_state_without_mutation(rig, change):
    props = rig.app["properties"]
    container = rig.revisions[0]["properties"]["template"]["containers"][0]
    if change == "tags":
        rig.app["tags"]["lab"] = "2"
    elif change == "running":
        props["runningStatus"] = "Running"
    elif change == "replicas":
        rig.revisions[0]["properties"]["replicas"] = 1
    elif change == "mode":
        props["configuration"]["activeRevisionsMode"] = "Multiple"
    elif change == "image":
        container["image"] = "unreviewed-image"
    elif change == "cpu":
        container["resources"]["cpu"] = 1
    elif change == "ingress":
        props["configuration"]["ingress"]["ipSecurityRestrictions"] = []
    with pytest.raises(ValueError):
        rig.plan()
    assert all(method == "get" for method, _, _ in rig.calls)


def test_prepare_pins_baseline_before_candidate_then_routes_and_cleans(rig):
    plan = rig.plan()
    ingress_before = copy.deepcopy(rig.app["properties"]["configuration"]["ingress"])
    rig.adapter.canary_prepare(plan)
    mutations = [(m, p, b) for m, p, b in rig.calls if m != "get"]
    assert mutations[0][2]["properties"]["configuration"]["ingress"]["traffic"] == [
        {"revisionName": plan["baseline"], "weight": 100}]
    assert "template" in mutations[1][2]["properties"]
    assert mutations[2][:2] == ("post", "/start")
    traffic = rig.adapter.canary_route(plan, 10)
    assert [r["weight"] for r in traffic] == [90, 10]
    assert all("latestRevision" not in r for r in traffic)
    result = rig.adapter.canary_cleanup(plan)
    assert result["ok"] and result["stopped_zero_replicas"] and result["candidate_inactive"]
    ingress = rig.app["properties"]["configuration"]["ingress"]
    assert ingress["ipSecurityRestrictions"] == ingress_before["ipSecurityRestrictions"]
    assert ingress["traffic"] == [{"revisionName": plan["baseline"], "weight": 100}]
    assert rig.app["properties"]["configuration"]["activeRevisionsMode"] == "Multiple"


def test_cleanup_still_stops_when_traffic_restore_fails(rig):
    plan = rig.plan()
    rig.adapter.canary_prepare(plan)
    rig.fail = lambda method, path, body: method == "patch"
    result = rig.adapter.canary_cleanup(plan)
    assert not result["ok"] and result["stopped_zero_replicas"]
    assert result["errors"] == ["restore_traffic:RuntimeError"]
    assert any(path == "/stop" for _, path, _ in rig.calls)


def test_unregistered_or_unready_candidate_is_not_created(rig):
    rig.registry.get_model_version.side_effect = lambda *args: SimpleNamespace(status="PENDING_REGISTRATION")
    with pytest.raises(ValueError, match="READY"):
        rig.plan()
    assert all(method == "get" for method, _, _ in rig.calls)


def test_unsafe_weight_and_candidate_absence_cannot_mutate(rig):
    plan = rig.plan()
    for weight in (50, 100, 10):
        with pytest.raises(ValueError):
            rig.adapter.canary_route(plan, weight)
    assert all(method == "get" for method, _, _ in rig.calls)


def test_batch_uses_only_main_endpoint_no_redirects_or_session(rig, monkeypatch):
    post = Mock(return_value=SimpleNamespace(status_code=200, headers={"x-request-id": "123"},
                                            json=lambda: {"probabilities": [0.3], "model_version": "2"}))
    monkeypatch.setattr(azure.requests, "post", post)
    body, request_id = rig.adapter.invoke_batch(URL, [{"value": 1}], timeout=4)
    assert request_id == "123" and body["model_version"] == "2"
    post.assert_called_once_with(URL + "/predict/batch", json={"rows": [{"value": 1}]},
        timeout=4, allow_redirects=False, headers={"Connection": "close"})


def test_modified_candidate_template_cannot_change_resources(rig):
    plan = rig.plan()
    plan["template"]["containers"][0]["resources"]["cpu"] = 1
    with pytest.raises(ValueError):
        rig.adapter.canary_prepare(plan)
    assert all(method == "get" for method, _, _ in rig.calls)


def test_readiness_access_failure_can_be_cleaned(rig, monkeypatch):
    plan = rig.plan()
    monkeypatch.setattr(azure.requests, "get", lambda *args, **kw: SimpleNamespace(status_code=403))
    with pytest.raises(RuntimeError, match="Readiness failed"):
        rig.adapter.canary_prepare(plan)
    assert rig.adapter.canary_cleanup(plan)["ok"]
    assert not any(b and b.get("properties", {}).get("configuration", {}).get("ingress", {}).get(
        "traffic", [{}])[0].get("weight") == 90 for _, _, b in rig.calls)


def test_traffic_unconfirmed_times_out_without_retrying_mutation(rig, monkeypatch):
    plan = rig.plan()
    request = rig.adapter._canary_request
    clock = [0.0]
    monkeypatch.setattr(azure.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(azure.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def pending(method, path="", body=None, deadline=None):
        result = request(method, path, body, deadline)
        if method == "patch":
            rig.app["properties"]["provisioningState"] = "Updating"
        return result

    monkeypatch.setattr(rig.adapter, "_canary_request", pending)
    with pytest.raises(TimeoutError, match="not confirmed"):
        rig.adapter.canary_route(plan, 0, deadline=4)
    assert clock[0] == 4
    assert len([m for m, _, _ in rig.calls if m == "patch"]) == 1


def test_deactivate_failure_does_not_skip_stop_or_claim_cleanup(rig, monkeypatch):
    plan = rig.plan()
    rig.adapter.canary_prepare(plan)
    rig.fail = lambda method, path, body: path.endswith("/deactivate")
    clock = [0.0]
    monkeypatch.setattr(azure.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(azure.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    result = rig.adapter.canary_cleanup(plan)
    assert not result["ok"] and result["stopped_zero_replicas"]
    assert not result["candidate_inactive"] and result["errors"] == ["deactivate_candidate:RuntimeError"]
    assert any(path == "/stop" for _, path, _ in rig.calls)
