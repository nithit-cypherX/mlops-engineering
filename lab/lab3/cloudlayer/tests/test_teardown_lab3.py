"""Fixed-scope deletion tests: every credential, CLI, HTTP and sleep is fake."""
import copy
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from cloudlayer import teardown_lab3 as cleanup


@pytest.fixture
def rig(monkeypatch):
    fail = Mock(side_effect=AssertionError("Unexpected real boundary"))
    monkeypatch.setattr(cleanup.requests, "request", fail)
    monkeypatch.setattr(cleanup, "AzureCliCredential", fail)
    monkeypatch.setattr(cleanup.time, "sleep", fail)
    state = SimpleNamespace(resources={}, cli_calls=[], reads=[], deleted=[], hold=set(), fault=None,
                            consumers=[], associated=[], federated=[], extra_grants=[])
    for rid in cleanup.TARGETS:
        resource = {"id": rid, "name": rid.rsplit("/", 1)[1],
                    "type": rid.split("/providers/", 1)[1].rsplit("/", 1)[0],
                    "location": "malaysiawest", "tags": dict(cleanup.TAGS),
                    "properties": {"provisioningState": "Succeeded"}}
        if rid in cleanup.ROLES:
            scope, role = cleanup.ROLES[rid]
            resource["type"] = "Microsoft.Authorization/roleAssignments"
            resource["properties"] = {
                "scope": scope, "principalId": cleanup.PRINCIPAL,
                "roleDefinitionId": f"/subscriptions/{cleanup.SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{role}",
            }
        state.resources[rid] = resource
    app = state.resources[cleanup.APP]
    app["properties"].update(runningStatus="Stopped", environmentId=cleanup.ENVIRONMENT)
    app["identity"] = {"userAssignedIdentities": {cleanup.IDENTITY: {}}}
    state.resources[cleanup.IDENTITY]["properties"]["principalId"] = cleanup.PRINCIPAL
    state.revisions = [{"name": cleanup.APP_NAME + "--0000003",
                        "properties": {"replicas": 0, "runningState": "Stopped"}}]

    def get(rid, version):
        state.reads.append(rid)
        if state.fault == ("get", rid):
            raise RuntimeError("fake read failure")
        return copy.deepcopy(state.resources.get(rid))

    def listing(rid, version):
        if state.fault == ("list", rid):
            raise RuntimeError("fake incomplete inventory")
        if rid == cleanup.APP + "/revisions":
            return copy.deepcopy(state.revisions)
        assert rid.endswith(("/containerApps", "/jobs"))
        items = state.consumers[:]
        if cleanup.APP in state.resources and rid.endswith("/containerApps"):
            items.append(state.resources[cleanup.APP])
        return copy.deepcopy(items)

    def delete(rid):
        assert rid in cleanup.TARGETS
        if state.fault == ("delete", rid):
            raise RuntimeError("fake delete failure")
        state.deleted.append(rid)
        if rid not in state.hold:
            state.resources.pop(rid, None)

    def cli(args):
        state.cli_calls.append(args)
        if args[:2] == ["identity", "list-resources"]:
            return copy.deepcopy(state.associated)
        if args[:3] == ["identity", "federated-credential", "list"]:
            return copy.deepcopy(state.federated)
        if args[:3] == ["role", "assignment", "list"]:
            return [rid for rid in cleanup.ROLES if rid in state.resources] + state.extra_grants
        raise AssertionError(f"Unexpected CLI call: {args}")

    state.client = SimpleNamespace(get=get, list=listing, delete=delete)
    state.factory = Mock(return_value=state.client)
    monkeypatch.setattr(cleanup, "Arm", state.factory)
    monkeypatch.setattr(cleanup.AzureAdapter, "_az_json", cli)
    state.cfg = SimpleNamespace(**cleanup.APPROVED_SCOPE)
    return state


def test_preview_reads_without_any_deletion(rig):
    result = cleanup.teardown(rig.cfg)
    assert result == {"mode": "preview", "would_delete": list(cleanup.TARGETS), "already_absent": []}
    assert not rig.deleted


def test_only_five_reviewed_ids_deleted_in_order_with_final_absence_check(rig, capsys):
    result = cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert result["all_five_absent"] and result["mode"] == "complete"
    assert result["confirmed_deleted"] == rig.deleted == list(cleanup.TARGETS)
    assert rig.reads[-5:] == list(cleanup.TARGETS)
    assert cleanup.ACR not in rig.deleted and cleanup.WORKSPACE not in rig.deleted
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["confirmed_deleted"] for e in events] == list(cleanup.TARGETS)
    assert all(e["utc"] for e in events)


@pytest.mark.parametrize("field", list(cleanup.APPROVED_SCOPE))
def test_changed_scope_stops_before_client(rig, field):
    setattr(rig.cfg, field, "different")
    with pytest.raises(ValueError, match="scope"):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    rig.factory.assert_not_called()


def test_wrong_confirmation_stops_before_client(rig):
    with pytest.raises(ValueError, match="exact"):
        cleanup.teardown(rig.cfg, "yes")
    rig.factory.assert_not_called()


@pytest.mark.parametrize("rid", list(cleanup.TARGETS))
@pytest.mark.parametrize("field", ["id", "name", "type"])
def test_wrong_identity_metadata_blocks_every_delete(rig, rid, field):
    rig.resources[rid][field] = "wrong"
    with pytest.raises(ValueError, match="identity/type"):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert not rig.deleted


@pytest.mark.parametrize("rid", [cleanup.APP, cleanup.ENVIRONMENT, cleanup.IDENTITY])
@pytest.mark.parametrize("field", ["course", "student", "lab", "location"])
def test_wrong_tag_or_location_blocks_every_delete(rig, rid, field):
    target = rig.resources[rid] if field == "location" else rig.resources[rid]["tags"]
    target[field] = "wrong"
    with pytest.raises(ValueError, match="tags/location"):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert not rig.deleted


@pytest.mark.parametrize("fault", ["running", "provisioning", "environment", "identity", "principal",
                                  "replicas", "revision_state", "missing_replicas", "no_revisions"])
def test_unsafe_state_blocks_every_delete(rig, fault):
    props = rig.resources[cleanup.APP]["properties"]
    if fault == "running": props["runningStatus"] = "Running"
    elif fault == "provisioning": props["provisioningState"] = "Updating"
    elif fault == "environment": props["environmentId"] = "other"
    elif fault == "identity": rig.resources[cleanup.APP]["identity"] = {}
    elif fault == "principal": rig.resources[cleanup.IDENTITY]["properties"]["principalId"] = "other"
    elif fault == "replicas": rig.revisions[0]["properties"]["replicas"] = 1
    elif fault == "revision_state": rig.revisions[0]["properties"]["runningState"] = "Running"
    elif fault == "missing_replicas": rig.revisions[0]["properties"].pop("replicas")
    else: rig.revisions.clear()
    with pytest.raises(ValueError):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert not rig.deleted


@pytest.mark.parametrize("rid", list(cleanup.ROLES))
@pytest.mark.parametrize("field", ["scope", "principalId", "roleDefinitionId"])
def test_changed_grant_blocks_every_delete(rig, rid, field):
    rig.resources[rid]["properties"][field] = "other"
    with pytest.raises(ValueError, match="Role assignment"):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert not rig.deleted


@pytest.mark.parametrize("fault", ["app_environment", "app_identity", "associated", "federated", "grant"])
def test_shared_or_unreviewed_identity_use_blocks_every_delete(rig, fault):
    if fault.startswith("app_"):
        other = {"id": "other", "properties": {}, "identity": {}}
        if fault == "app_environment": other["properties"]["environmentId"] = cleanup.ENVIRONMENT
        else: other["identity"]["userAssignedIdentities"] = {cleanup.IDENTITY: {}}
        rig.consumers.append(other)
    elif fault == "associated": rig.associated.append({"id": "other-resource"})
    elif fault == "federated": rig.federated.append({"name": "external-ci"})
    else: rig.extra_grants.append("unreviewed-grant")
    with pytest.raises(ValueError):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert not rig.deleted


def test_partial_prior_cleanup_is_idempotent(rig):
    for rid in list(cleanup.TARGETS)[:3]: rig.resources.pop(rid)
    result = cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert result["all_five_absent"] and rig.deleted == list(cleanup.TARGETS)[3:]


def test_all_absent_is_confirmed_without_delete(rig):
    rig.resources.clear()
    assert cleanup.teardown(rig.cfg, cleanup.APP_NAME)["confirmed_deleted"] == []
    assert not rig.deleted and not rig.cli_calls


@pytest.mark.parametrize("phase", ["get", "delete"])
def test_error_stops_and_never_claims_complete(rig, phase):
    rig.fault = (phase, cleanup.ENVIRONMENT)
    with pytest.raises(RuntimeError, match="fake"):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert rig.deleted == ([] if phase == "get" else [cleanup.APP])


def test_new_consumer_after_app_deletion_blocks_environment_and_identity(rig):
    original = rig.client.delete
    def delete(rid):
        original(rid)
        rig.consumers.append({"id": "new-job", "properties": {"environmentId": cleanup.ENVIRONMENT}})
    rig.client.delete = delete
    with pytest.raises(ValueError, match="Another"):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert rig.deleted == [cleanup.APP]


def test_missing_final_absence_is_not_success(rig):
    rig.hold.add(cleanup.IDENTITY)
    with pytest.raises(RuntimeError, match="still present"):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)


def test_reappearing_app_blocks_dependent_deletions(rig):
    rig.hold.add(cleanup.APP)
    with pytest.raises(RuntimeError, match="earlier target"):
        cleanup.teardown(rig.cfg, cleanup.APP_NAME)
    assert rig.deleted == [cleanup.APP]


@pytest.fixture
def http(monkeypatch):
    credential = Mock()
    credential.get_token.return_value.token = "unit-test-token"
    factory = Mock(return_value=credential)
    monkeypatch.setattr(cleanup, "AzureCliCredential", factory)
    cli = Mock(return_value={"id": cleanup.SUBSCRIPTION, "state": "Enabled"})
    monkeypatch.setattr(cleanup.AzureAdapter, "_az_json", cli)
    request = Mock()
    monkeypatch.setattr(cleanup.requests, "request", request)
    monkeypatch.setattr(cleanup.time, "sleep", Mock())
    return SimpleNamespace(client=cleanup.Arm(), request=request, factory=factory, cli=cli)


def response(status, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body)


@pytest.mark.parametrize("status", [301, 401, 403, 409, 429, 500])
def test_http_failures_never_count_as_absence_or_retry(http, status):
    http.request.return_value = response(status)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        http.client.get(cleanup.APP, cleanup.APP_API)
    assert http.request.call_count == 1


@pytest.mark.parametrize("status", [401, 403, 409, 429, 500])
def test_delete_http_failure_stops_before_readback(http, status):
    http.request.return_value = response(status)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        http.client.delete(cleanup.APP)
    assert http.request.call_count == 1


def test_network_timeout_is_not_retried_or_reported_absent(http):
    http.request.side_effect = requests.Timeout("fake timeout")
    with pytest.raises(requests.Timeout):
        http.client.get(cleanup.APP, cleanup.APP_API)
    assert http.request.call_count == 1


def test_get_404_is_absence_only_for_exact_targets(http):
    http.request.return_value = response(404)
    assert http.client.get(cleanup.APP, cleanup.APP_API) is None
    with pytest.raises(RuntimeError, match="404"):
        http.client.list(cleanup.APP + "/revisions", cleanup.APP_API)


@pytest.mark.parametrize("body", [None, {}, {"value": {}}, {"value": [], "nextLink": "more"}])
def test_incomplete_or_paged_inventory_cannot_pass(http, body):
    http.request.return_value = response(200, body)
    with pytest.raises(ValueError, match="inventory"):
        http.client.list(cleanup.APP + "/revisions", cleanup.APP_API)


def test_delete_waits_for_get_404_and_never_follows_redirects(http):
    http.request.side_effect = [response(202), response(200, {"id": cleanup.APP}), response(404)]
    http.client.delete(cleanup.APP)
    assert [call.args[0] for call in http.request.call_args_list] == ["DELETE", "GET", "GET"]
    assert all(call.kwargs["allow_redirects"] is False and call.kwargs["timeout"] == 30
               for call in http.request.call_args_list)


def test_delete_timeout_is_not_success(http, monkeypatch):
    http.request.return_value = response(202)
    monkeypatch.setattr(cleanup, "DELETE_TIMEOUT", 0)
    with pytest.raises(TimeoutError, match="not confirmed"):
        http.client.delete(cleanup.APP)


@pytest.mark.parametrize("rid", [cleanup.SCOPE, cleanup.ACR, cleanup.WORKSPACE, cleanup.APP + "/other"])
def test_transport_refuses_deleting_shared_or_unreviewed_ids(http, rid):
    with pytest.raises(ValueError, match="five reviewed"):
        http.client.request("DELETE", rid, cleanup.APP_API)
    http.request.assert_not_called()


def test_wrong_account_blocks_credentials(http):
    http.cli.return_value = {"id": "other", "state": "Enabled"}
    http.factory.reset_mock()
    with pytest.raises(ValueError, match="subscription"):
        cleanup.Arm()
    http.factory.assert_not_called()


def test_cli_help_does_not_load_config_or_call_cloud(monkeypatch):
    loader = Mock(side_effect=AssertionError("No config"))
    monkeypatch.setattr(cleanup.config, "load", loader)
    monkeypatch.setattr(sys, "argv", ["teardown_lab3", "--help"])
    with pytest.raises(SystemExit) as result:
        cleanup.main()
    assert result.value.code == 0
    loader.assert_not_called()


def test_main_preview_has_no_delete(rig, monkeypatch, capsys):
    monkeypatch.setattr(cleanup.config, "load", Mock(return_value=rig.cfg))
    monkeypatch.setattr(sys, "argv", ["teardown_lab3"])
    assert cleanup.main() == 0 and not rig.deleted
    output = capsys.readouterr().out
    assert "Preview only" in output and cleanup.RETAINED in output


def test_main_read_failure_returns_nonzero(rig, monkeypatch, capsys):
    monkeypatch.setattr(cleanup.config, "load", Mock(return_value=rig.cfg))
    monkeypatch.setattr(sys, "argv", ["teardown_lab3", "--confirm", cleanup.APP_NAME])
    rig.fault = ("get", cleanup.APP)
    assert cleanup.main() == 1
    assert "not undone" in capsys.readouterr().err and not rig.deleted


def test_main_cli_failure_is_reported_without_false_completion(rig, monkeypatch, capsys):
    monkeypatch.setattr(cleanup.config, "load", Mock(return_value=rig.cfg))
    monkeypatch.setattr(sys, "argv", ["teardown_lab3"])
    monkeypatch.setattr(cleanup.AzureAdapter, "_az_json",
                        Mock(side_effect=subprocess.TimeoutExpired("az", 90)))
    assert cleanup.main() == 1
    assert "Teardown stopped" in capsys.readouterr().err and not rig.deleted
