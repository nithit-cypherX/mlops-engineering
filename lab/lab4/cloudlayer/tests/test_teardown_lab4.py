"""Offline scope/state checks. No Azure login, traffic or mutation."""
import copy
import json
import os
import shlex
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from azure.core.exceptions import ClientAuthenticationError

from cloudlayer import lab4_scope as scope
from cloudlayer import teardown_lab4 as preview


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    fail = Mock(side_effect=AssertionError("Unexpected real boundary"))
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(scope, "AzureCliCredential", fail)
    for method in ("get", "post", "put", "patch", "delete", "request"):
        monkeypatch.setattr(preview.requests, method, fail)


@pytest.fixture
def cfg():
    return SimpleNamespace(provider="azure", project_id=scope.GROUP,
                           azure_subscription_id=scope.SUBSCRIPTION,
                           endpoint_name="itcs355-lab4-staging",
                           azure_ml_workspace="mlw-itcs355-u6688124")


@pytest.fixture
def reader():
    resources = {}
    for rid in scope.TARGETS:
        kind = ("Microsoft.Authorization/roleAssignments" if rid in scope.GRANTS
                else rid.split("/providers/", 1)[1].rsplit("/", 1)[0].replace("/mlw-itcs355-u6688124", ""))
        resources[rid] = {"id": rid, "type": kind, "tags": {"course": "itcs355", "lab": "4"}, "properties": {}}
    for rid in (scope.RULE, scope.ACTION_GROUP):
        resources[rid]["tags"] = {"course": "ITCS355", "lab": "lab4"}
    # AML schedules put their tags inside properties.
    resources[scope.SCHEDULE]["properties"] = {"isEnabled": False, "tags": resources[scope.SCHEDULE].pop("tags")}
    resources[scope.RULE]["properties"]["enabled"] = False
    resources[scope.COMPUTE]["properties"]["properties"] = {"currentNodeCount": 0, "targetNodeCount": 0}
    for rid, principal in scope.IDENTITIES.items():
        resources[rid]["properties"]["principalId"] = principal
    for rid, (assigned_scope, principal, role) in scope.GRANTS.items():
        resources[rid]["properties"] = {"scope": assigned_scope, "principalId": principal,
                                      "roleDefinitionId": f"/subscriptions/{scope.SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{role}"}
    collections = {
        scope.APP + "/revisions": [{"properties": {"replicas": 0}}],
        scope.WORKSPACE + "/schedules?listViewType=All": [resources[scope.SCHEDULE]],
        scope.WORKSPACE + "/jobs": [{"properties": {"status": "Completed"}}],
    }
    return SimpleNamespace(resources=resources, collections=collections,
                           get=Mock(side_effect=lambda rid, api: copy.deepcopy(resources[rid])),
                           list=Mock(side_effect=lambda rid, api: copy.deepcopy(collections[rid])))


def test_exact_preview_never_claims_completion(cfg, reader):
    result = preview.preview(cfg, reader)
    assert result["mode"] == "preview_only" and result["deletion_enabled"] is False
    assert result["teardown_complete"] is False and result["blockers"] == []
    assert {r["id"] for r in result["reviewed_candidates"]} == set(scope.TARGETS)
    assert len(scope.TARGETS) == 13 and len(scope.GRANTS) == 5
    assert scope.WORKSPACE not in scope.TARGETS and scope.ACR not in scope.TARGETS
    assert not any(rid.endswith(("-lab4-ci", "-lab4-push", "-lab4-deploy")) for rid in scope.TARGETS)
    assert not hasattr(preview.ReadOnlyArm, "delete")
    reader.list.assert_any_call(scope.WORKSPACE + "/schedules?listViewType=All", "2025-09-01")


@pytest.mark.parametrize("field,value", [
    ("provider", "local"), ("project_id", "other"), ("azure_subscription_id", "other"),
    ("endpoint_name", "other"), ("azure_ml_workspace", "other"),
])
def test_wrong_config_stops_before_read(cfg, reader, field, value):
    setattr(cfg, field, value)
    with pytest.raises(ValueError):
        preview.preview(cfg, reader)
    reader.get.assert_not_called()


@pytest.mark.parametrize("fault", ["id", "type", "tag", "principal", "role-scope", "role-definition"])
def test_changed_inventory_is_not_treated_as_safe(cfg, reader, fault):
    item = reader.resources[scope.APP]
    if fault in {"id", "type"}:
        item[fault] = "different"
    elif fault == "tag":
        item["tags"]["lab"] = "3"
    elif fault == "principal":
        reader.resources[scope.RUNTIME]["properties"]["principalId"] = "different"
    else:
        item = reader.resources[next(iter(scope.GRANTS))]["properties"]
        item["scope" if fault == "role-scope" else "roleDefinitionId"] = "different"
    with pytest.raises(ValueError):
        preview.preview(cfg, reader)


@pytest.mark.parametrize("fault", ["schedule", "alert", "nodes", "replicas", "job", "other-schedule"])
def test_running_or_unknown_state_requires_review(cfg, reader, fault):
    if fault == "schedule":
        reader.resources[scope.SCHEDULE]["properties"]["isEnabled"] = True
    elif fault == "alert":
        reader.resources[scope.RULE]["properties"]["enabled"] = None
    elif fault == "nodes":
        reader.resources[scope.COMPUTE]["properties"]["properties"]["targetNodeCount"] = 1
    elif fault == "replicas":
        reader.collections[scope.APP + "/revisions"][0]["properties"]["replicas"] = 1
    elif fault == "job":
        reader.collections[scope.WORKSPACE + "/jobs"][0]["properties"]["status"] = "NotResponding"
    else:
        reader.collections[scope.WORKSPACE + "/schedules?listViewType=All"].append({"id": "unreviewed"})
    assert preview.preview(cfg, reader)["blockers"]


def test_absent_target_is_not_a_completed_teardown(cfg, reader):
    reader.resources[scope.APP] = None
    result = preview.preview(cfg, reader)
    assert next(r for r in result["reviewed_candidates"] if r["id"] == scope.APP)["state"] == "absent"
    assert result["teardown_complete"] is False


def test_schedule_optional_type_requires_exact_name_and_id(cfg, reader):
    item = reader.resources[scope.SCHEDULE]
    item.pop("type")
    item["name"] = "lab4-drift-schedule-check"
    assert not preview.preview(cfg, reader)["blockers"]
    item["name"] = "other"
    with pytest.raises(ValueError):
        preview.preview(cfg, reader)
    item["name"] = "lab4-drift-schedule-check"
    item["id"] = scope.SCHEDULE + "-other"
    with pytest.raises(ValueError):
        preview.preview(cfg, reader)


def test_other_resources_still_require_type(cfg, reader):
    reader.resources[scope.APP].pop("type")
    with pytest.raises(ValueError):
        preview.preview(cfg, reader)


def test_wrong_confirm_is_rejected_before_authentication(cfg, monkeypatch):
    monkeypatch.setattr(preview.config, "load", Mock(return_value=cfg))
    monkeypatch.setattr(sys, "argv", ["teardown", "--confirm", "yes"])
    assert preview.main() == 1
    scope.AzureCliCredential.assert_not_called()


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_errors_are_not_absent_or_retried(monkeypatch, status):
    monkeypatch.setattr(scope, "access_token", Mock(return_value="test-only"))
    get = Mock(return_value=SimpleNamespace(status_code=status))
    monkeypatch.setattr(preview.requests, "get", get)
    with pytest.raises(RuntimeError):
        preview.ReadOnlyArm().get(scope.APP, "2025-07-01")
    get.assert_called_once()
    preview.requests.delete.assert_not_called()


def test_404_is_absent_only_for_reviewed_target(monkeypatch):
    monkeypatch.setattr(scope, "access_token", Mock(return_value="test-only"))
    get = Mock(return_value=SimpleNamespace(status_code=404))
    monkeypatch.setattr(preview.requests, "get", get)
    arm = preview.ReadOnlyArm()
    assert arm.get(scope.APP, "2025-07-01") is None
    with pytest.raises(RuntimeError):
        arm.get(scope.WORKSPACE, "2025-09-01")


def test_pagination_cannot_send_token_to_other_host(monkeypatch):
    monkeypatch.setattr(scope, "access_token", Mock(return_value="test-only"))
    payload = {"value": [], "nextLink": "https://example.com" + scope.APP + "/revisions?page=2"}
    get = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: payload))
    monkeypatch.setattr(preview.requests, "get", get)
    with pytest.raises(ValueError):
        preview.ReadOnlyArm().list(scope.APP + "/revisions", "2025-07-01")
    get.assert_called_once()


@pytest.mark.parametrize("failure", [False, True])
def test_token_helper_restores_environment_and_sanitizes_failure(monkeypatch, failure):
    monkeypatch.setenv("AZURE_EXTENSION_DIR", "previous-directory")
    monkeypatch.delenv("AZURE_EXTENSION_USE_DYNAMIC_INSTALL", raising=False)
    manager = MagicMock()
    if failure:
        manager.__enter__.return_value.get_token.side_effect = ClientAuthenticationError("private detail")
    else:
        def token(*args):
            assert os.environ["AZURE_EXTENSION_DIR"] != "previous-directory"
            assert os.environ["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"
            return SimpleNamespace(token="test-only")
        manager.__enter__.return_value.get_token.side_effect = token
    monkeypatch.setattr(scope, "AzureCliCredential", Mock(return_value=manager))
    if failure:
        with pytest.raises(RuntimeError) as exc:
            scope.access_token()
        assert "private detail" not in str(exc.value)
    else:
        assert scope.access_token() == "test-only"
    assert os.environ["AZURE_EXTENSION_DIR"] == "previous-directory"
    assert "AZURE_EXTENSION_USE_DYNAMIC_INSTALL" not in os.environ


@pytest.fixture
def deletion_reader(reader, monkeypatch):
    monkeypatch.setattr(preview, "check_ci_quiet", Mock())
    for rid, identity in ((scope.APP, scope.RUNTIME), (scope.COMPUTE, scope.DRIFT_IDENTITY)):
        reader.resources[rid]["identity"] = {"userAssignedIdentities": {identity: {}}}
        reader.resources[rid]["properties"]["provisioningState"] = "Succeeded"
    reader.resources[scope.APP]["properties"]["environmentId"] = scope.ENVIRONMENT
    reader.resources[scope.COMPUTE]["properties"].update(computeType="AmlCompute", isAttachedCompute=False)
    reader.collections.update({
        preview.SUB + "/resources": [{"id": scope.WORKSPACE, "type": "Microsoft.MachineLearningServices/workspaces"}],
        preview.SUB + "/providers/Microsoft.App/containerApps": [reader.resources[scope.APP]],
        preview.SUB + "/providers/Microsoft.App/jobs": [],
        scope.WORKSPACE + "/computes": [reader.resources[scope.COMPUTE]],
        preview.SUB + "/providers/Microsoft.Authorization/roleAssignments": [reader.resources[r] for r in scope.GRANTS],
        scope.RUNTIME + "/federatedIdentityCredentials": [],
        scope.DRIFT_IDENTITY + "/federatedIdentityCredentials": [],
    })
    reader.associated = Mock(side_effect=lambda rid: [{"id": scope.APP if rid == scope.RUNTIME else scope.COMPUTE}])
    reader.delete = Mock(side_effect=lambda rid: reader.resources.__setitem__(rid, None))
    return reader


def test_no_confirm_means_read_only_without_ci_lookup(cfg, reader, monkeypatch):
    ci = Mock(side_effect=AssertionError("Preview must not require GitHub"))
    mutation = Mock(side_effect=AssertionError("Preview must not enable deletion"))
    monkeypatch.setattr(preview, "check_ci_quiet", ci)
    monkeypatch.setattr(preview, "DeletionArm", mutation)
    assert preview.teardown(cfg, reader=reader)["mode"] == "preview_only"
    ci.assert_not_called()
    mutation.assert_not_called()


def test_exact_deletion_order_and_explicit_rerun(cfg, deletion_reader):
    reader = deletion_reader
    result = preview.teardown(cfg, preview.CONFIRM, reader)
    assert result["teardown_complete"] is True and result["mode"] == "complete"
    assert result["confirmed_absent_after_delete"] == list(scope.TARGETS)
    assert [call.args[0] for call in reader.delete.call_args_list] == list(scope.TARGETS)
    assert len(reader.delete.call_args_list) == 13
    assert preview.check_ci_quiet.call_count == 14
    reader.delete.reset_mock()
    result = preview.teardown(cfg, preview.CONFIRM, reader)
    assert result["already_absent"] == list(scope.TARGETS)
    reader.delete.assert_not_called()


@pytest.mark.parametrize("fault", [
    "other-app", "other-job", "other-compute", "generic-identity", "other-workspace", "other-alert",
    "federation", "other-grant", "associated", "app-env", "app-identity", "unstable", "attached-compute",
])
def test_dependency_guards_stop_before_first_delete(cfg, deletion_reader, fault):
    r = deletion_reader
    if fault in {"other-app", "other-job"}:
        kind = "containerApps" if fault == "other-app" else "jobs"
        r.collections[preview.SUB + f"/providers/Microsoft.App/{kind}"].append(
            {"id": "other", "properties": {"environmentId": scope.ENVIRONMENT}})
    elif fault in {"other-compute", "generic-identity"}:
        key = scope.WORKSPACE + "/computes" if fault == "other-compute" else preview.SUB + "/resources"
        r.collections[key].append({"id": "other", "type": "any/provider",
                                  "identity": {"userAssignedIdentities": {scope.DRIFT_IDENTITY: {}}}})
    elif fault in {"other-workspace", "other-alert"}:
        kind = "Microsoft.MachineLearningServices/workspaces" if fault == "other-workspace" else "Microsoft.Insights/metricAlerts"
        r.collections[preview.SUB + "/resources"].append({"id": "other", "type": kind})
    elif fault == "federation":
        r.collections[scope.RUNTIME + "/federatedIdentityCredentials"] = [{"name": "unexpected"}]
    elif fault == "other-grant":
        r.collections[preview.SUB + "/providers/Microsoft.Authorization/roleAssignments"].append(
            {"id": "other", "properties": {"principalId": scope.IDENTITIES[scope.DRIFT_IDENTITY]}})
    elif fault == "associated":
        r.associated.side_effect = None
        r.associated.return_value = [{"id": "unreviewed"}]
    elif fault == "app-env":
        r.resources[scope.APP]["properties"]["environmentId"] = "other"
    elif fault == "app-identity":
        r.resources[scope.APP]["identity"]["userAssignedIdentities"] = {"other": {}}
    elif fault == "unstable":
        r.resources[scope.COMPUTE]["properties"]["provisioningState"] = "Updating"
    else:
        r.resources[scope.COMPUTE]["properties"]["isAttachedCompute"] = True
    with pytest.raises(ValueError):
        preview.teardown(cfg, preview.CONFIRM, r)
    r.delete.assert_not_called()


def test_active_runtime_blocks_confirmed_delete(cfg, deletion_reader):
    deletion_reader.resources[scope.COMPUTE]["properties"]["properties"]["currentNodeCount"] = 1
    with pytest.raises(ValueError, match="zero nodes"):
        preview.teardown(cfg, preview.CONFIRM, deletion_reader)
    deletion_reader.delete.assert_not_called()


def test_partial_failure_stops_and_explicit_rerun_only_deletes_remaining(cfg, deletion_reader, capsys):
    r = deletion_reader
    def delete(rid):
        if rid == scope.APP:
            raise RuntimeError("HTTP 403")
        r.resources[rid] = None
    r.delete.side_effect = delete
    with pytest.raises(RuntimeError):
        preview.teardown(cfg, preview.CONFIRM, r)
    assert [c.args[0] for c in r.delete.call_args_list] == list(scope.TARGETS)[:4]
    assert len(capsys.readouterr().out.splitlines()) == 3
    r.delete.reset_mock()
    r.delete.side_effect = lambda rid: r.resources.__setitem__(rid, None)
    result = preview.teardown(cfg, preview.CONFIRM, r)
    assert result["already_absent"] == list(scope.TARGETS)[:3]
    assert result["confirmed_absent_after_delete"] == list(scope.TARGETS)[3:]


def test_deleted_target_reappearing_blocks_dependents(cfg, deletion_reader):
    r = deletion_reader
    schedule = copy.deepcopy(r.resources[scope.SCHEDULE])
    def delete(rid):
        r.resources[rid] = None
        if rid == scope.RULE:
            r.resources[scope.SCHEDULE] = schedule
    r.delete.side_effect = delete
    with pytest.raises(ValueError, match="reappeared"):
        preview.teardown(cfg, preview.CONFIRM, r)
    assert r.delete.call_count == 2


def test_ci_reenabled_during_cleanup_stops(cfg, deletion_reader):
    preview.check_ci_quiet.side_effect = [None, None, ValueError("CI active")]
    with pytest.raises(ValueError):
        preview.teardown(cfg, preview.CONFIRM, deletion_reader)
    assert deletion_reader.delete.call_count == 1


def test_no_completion_until_final_all_targets_absent(cfg, deletion_reader):
    r = deletion_reader
    base_get = r.get.side_effect
    def get(rid, api):
        if r.delete.call_count == 13 and rid == scope.SCHEDULE:
            return {"reappeared": True}
        return base_get(rid, api)
    r.get.side_effect = get
    with pytest.raises(RuntimeError, match="still present"):
        preview.teardown(cfg, preview.CONFIRM, r)


@pytest.mark.parametrize("status", ["active", "disabled_inactivity", None])
def test_ci_must_be_manually_disabled(monkeypatch, status):
    payload = {"id": 1, "path": ".github/workflows/lab4-ci.yml", "name": "Lab 4 CI", "state": status}
    get = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: payload))
    monkeypatch.setattr(preview.requests, "get", get)
    with pytest.raises(ValueError):
        preview.check_ci_quiet()
    get.assert_called_once()


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending", "requested", None])
def test_ci_blocks_noncompleted_run_on_second_page(monkeypatch, status):
    pages = [
        {"id": 1, "path": ".github/workflows/lab4-ci.yml", "name": "Lab 4 CI", "state": "disabled_manually"},
        {"total_count": 101, "workflow_runs": [{"id": i, "workflow_id": 1, "status": "completed"} for i in range(100)]},
        {"total_count": 101, "workflow_runs": [{"id": 100, "workflow_id": 1, "status": status}]},
    ]
    get = Mock(side_effect=lambda *args, **kw: SimpleNamespace(status_code=200, json=lambda: pages.pop(0)))
    monkeypatch.setattr(preview.requests, "get", get)
    with pytest.raises(ValueError, match="unfinished"):
        preview.check_ci_quiet()
    assert get.call_count == 3
    assert get.call_args.args[0].endswith("page=2")
    assert all("Authorization" not in call.kwargs["headers"] for call in get.call_args_list)


@pytest.mark.parametrize("fault", ["none", "duplicate", "missing", "changed-total", "wrong-workflow"])
def test_ci_pagination_is_complete_and_consistent(monkeypatch, fault):
    run = {"id": 1, "workflow_id": 7, "status": "completed"}
    second = dict(run, id=2)
    if fault == "duplicate":
        second["id"] = 1
    if fault == "wrong-workflow":
        second["workflow_id"] = 8
    pages = [
        {"id": 7, "path": ".github/workflows/lab4-ci.yml", "name": "Lab 4 CI", "state": "disabled_manually"},
        {"total_count": 2, "workflow_runs": [run]},
        {"total_count": 3 if fault == "changed-total" else 2, "workflow_runs": [] if fault == "missing" else [second]},
    ]
    monkeypatch.setattr(preview.requests, "get", Mock(side_effect=lambda *a, **k:
                        SimpleNamespace(status_code=200, json=lambda: pages.pop(0))))
    if fault == "none":
        preview.check_ci_quiet()
    else:
        with pytest.raises(ValueError):
            preview.check_ci_quiet()


@pytest.mark.parametrize("status", [302, 403, 404, 429, 500])
def test_ci_http_errors_never_allow_deletion(cfg, monkeypatch, status):
    monkeypatch.setattr(preview.requests, "get", Mock(return_value=SimpleNamespace(status_code=status)))
    with pytest.raises(RuntimeError):
        preview.teardown(cfg, preview.CONFIRM)
    scope.AzureCliCredential.assert_not_called()
    preview.requests.delete.assert_not_called()


@pytest.fixture
def arm(monkeypatch):
    monkeypatch.setattr(scope, "access_token", Mock(return_value="offline-placeholder"))
    monkeypatch.setattr(preview.time, "sleep", Mock())
    return preview.DeletionArm(preview.CONFIRM)


def test_delete_scope_and_confirmation_fail_before_write(monkeypatch):
    with pytest.raises(ValueError):
        preview.DeletionArm("yes")
    scope.AzureCliCredential.assert_not_called()
    monkeypatch.setattr(scope, "access_token", Mock(return_value="offline-placeholder"))
    arm = preview.DeletionArm(preview.CONFIRM)
    for rid in [scope.WORKSPACE, scope.ACR, scope.SCOPE, scope.APP + "/../other", scope.APP + "?other=true"]:
        with pytest.raises(ValueError):
            arm.delete(rid)
    preview.requests.delete.assert_not_called()
    preview.requests.get.assert_not_called()


@pytest.mark.parametrize("status", [200, 202, 204])
def test_delete_waits_for_404_and_deletes_underlying_compute(arm, reader, monkeypatch, status):
    item = reader.resources[scope.COMPUTE]
    responses = [SimpleNamespace(status_code=200, json=lambda: item),
                 SimpleNamespace(status_code=200, json=lambda: item), SimpleNamespace(status_code=404)]
    get = Mock(side_effect=responses)
    delete = Mock(return_value=SimpleNamespace(status_code=status))
    monkeypatch.setattr(preview.requests, "get", get)
    monkeypatch.setattr(preview.requests, "delete", delete)
    arm.delete(scope.COMPUTE)
    delete.assert_called_once()
    assert delete.call_args.args[0] == f"https://management.azure.com{scope.COMPUTE}?api-version=2025-09-01&underlyingResourceAction=Delete"
    assert delete.call_args.kwargs["allow_redirects"] is False
    assert get.call_count == 3
    preview.time.sleep.assert_called_once()


@pytest.mark.parametrize("status", [302, 403, 404, 429, 500])
def test_delete_http_error_does_not_retry(arm, reader, monkeypatch, status):
    arm.get = Mock(return_value=reader.resources[scope.APP])
    delete = Mock(return_value=SimpleNamespace(status_code=status))
    monkeypatch.setattr(preview.requests, "delete", delete)
    with pytest.raises(RuntimeError):
        arm.delete(scope.APP)
    delete.assert_called_once()
    arm.get.assert_called_once()
    preview.time.sleep.assert_not_called()


def test_already_absent_target_skips_delete(arm):
    arm.get = Mock(return_value=None)
    arm.delete(scope.APP)
    preview.requests.delete.assert_not_called()


def test_delete_timeout_does_not_claim_success_or_retry(arm, reader, monkeypatch):
    arm.get = Mock(return_value=reader.resources[scope.APP])
    monkeypatch.setattr(preview.requests, "delete", Mock(return_value=SimpleNamespace(status_code=202)))
    monkeypatch.setattr(preview.time, "monotonic", Mock(side_effect=[0, 601]))
    with pytest.raises(TimeoutError):
        arm.delete(scope.APP)
    preview.requests.delete.assert_called_once()


def test_poll_http_error_stops_without_retry(arm, reader, monkeypatch):
    arm.get = Mock(side_effect=[reader.resources[scope.APP], RuntimeError("HTTP 403")])
    monkeypatch.setattr(preview.requests, "delete", Mock(return_value=SimpleNamespace(status_code=202)))
    with pytest.raises(RuntimeError):
        arm.delete(scope.APP)
    preview.requests.delete.assert_called_once()


@pytest.mark.parametrize("fault", ["none", "pagination", "count", "item", "status", "scope"])
def test_identity_query_is_fixed_read_only_and_requires_complete_result(arm, monkeypatch, fault):
    payload = {"totalCount": 1, "value": [{"id": scope.APP}]}
    if fault == "pagination":
        payload["nextLink"] = "https://unreviewed.invalid"
    elif fault == "count":
        payload["totalCount"] = 2
    elif fault == "item":
        payload["value"] = [{}]
    post = Mock(return_value=SimpleNamespace(status_code=403 if fault == "status" else 200, json=lambda: payload))
    monkeypatch.setattr(preview.requests, "post", post)
    if fault == "none":
        assert arm.associated(scope.RUNTIME) == [{"id": scope.APP}]
        assert post.call_args.args[0].startswith(f"https://management.azure.com{scope.RUNTIME}/listAssociatedResources?")
    else:
        with pytest.raises((ValueError, RuntimeError)):
            arm.associated(scope.ACR if fault == "scope" else scope.RUNTIME)
    if fault == "scope":
        post.assert_not_called()
    preview.requests.delete.assert_not_called()


@pytest.mark.parametrize("confirmation,code", [("", 0), (preview.CONFIRM, 0), (preview.CONFIRM, 7), ('bad"; exit 9; "', 0)])
def test_make_quotes_confirmation_as_one_argument(confirmation, code):
    runner = f"import json,sys; print(json.dumps(sys.argv[1:])); sys.exit({code})"
    python = f"{shlex.quote(sys.executable)} -c {shlex.quote(runner)}"
    result = subprocess.run(["make", "--silent", "teardown", f"PYTHON={python}", f"CONFIRM={confirmation}"],
                            cwd=preview.config.REPO_ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=20)
    assert (result.returncode == 0) == (code == 0)
    assert json.loads(result.stdout) == ["-m", "cloudlayer.teardown_lab4", "--confirm", confirmation]
