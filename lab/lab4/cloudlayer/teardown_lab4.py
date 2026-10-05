"""Fixed-scope Lab 4 teardown; preview unless the exact app name is confirmed.

Inventory adapted from the Lab 3 fixed-scope approach and the 5 October readback.
Before deletion, disable Lab 4 CI and let all runs finish. Do not re-enable CI,
send staging traffic, deploy or change resources/roles while teardown runs.
Checks are not an atomic lock. Shared resources and CI identities are retained.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from cloudlayer import lab4_scope as scope
from src import config

CONFIRM = "itcs355-lab4-staging"
DELETE_TIMEOUT = 600
SUB = f"/subscriptions/{scope.SUBSCRIPTION}"
WORKFLOW = "https://api.github.com/repos/nithit-cypherX/mlops-engineering/actions/workflows/lab4-ci.yml"


class ReadOnlyArm:
    def __init__(self):
        self._token = scope.access_token()

    def get(self, path, version=None):
        url = path if path.startswith("https://") else "https://management.azure.com" + path
        if version:
            url += ("&" if "?" in url else "?") + "api-version=" + version
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "management.azure.com"
                or not parsed.path.lower().startswith(f"/subscriptions/{scope.SUBSCRIPTION}/")
                or parsed.fragment):
            raise ValueError("Read outside the reviewed subscription refused")
        response = requests.get(url, headers={"Authorization": f"Bearer {self._token}"},
                                timeout=(10, 35), allow_redirects=False)
        if response.status_code == 404 and parsed.path.lower() in {
            x.lower() for x in scope.TARGETS
        }:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"Inventory GET returned HTTP {response.status_code}; not confirmed")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid inventory response")
        return payload

    def list(self, path, version):
        page = self.get(path, version)
        rows, seen = [], set()
        while True:
            if (not isinstance(page, dict) or not isinstance(page.get("value"), list)
                    or any(not isinstance(item, dict) for item in page["value"])):
                raise ValueError("Invalid inventory collection")
            rows.extend(page["value"])
            link = page.get("nextLink")
            if link is None:
                return rows
            if (not isinstance(link, str) or not link or link in seen or len(seen) >= 50
                    or urlsplit(link).path.lower() != path.split("?", 1)[0].lower()):
                raise ValueError("Invalid or repeated inventory pagination")
            seen.add(link)
            page = self.get(link)


class DeletionArm(ReadOnlyArm):
    def __init__(self, confirm):
        if confirm != CONFIRM:
            raise ValueError("Deletion requires the exact reviewed app name")
        super().__init__()

    def associated(self, resource_id):
        """Read-only POST; refuse incomplete identity-consumer inventory.

        Azure documents a one-request/second limit and incomplete AML coverage:
        https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-view-associated-resources-for-an-identity
        AML computes are therefore also listed directly in dependency_check().
        """
        if resource_id not in scope.IDENTITIES:
            raise ValueError("Unreviewed identity lookup refused")
        time.sleep(1.1)
        response = requests.post(
            f"https://management.azure.com{resource_id}/listAssociatedResources"
            "?api-version=2021-09-30-preview&%24top=100",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=(10, 35), allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Identity lookup returned HTTP {response.status_code}")
        payload = response.json()
        if (not isinstance(payload, dict) or not isinstance(payload.get("value"), list)
                or payload.get("nextLink") or type(payload.get("totalCount")) is not int
                or payload["totalCount"] != len(payload["value"])
                or any(not isinstance(item, dict) or not item.get("id") for item in payload["value"])):
            raise ValueError("Incomplete identity-consumer inventory; review required")
        return payload["value"]

    def delete(self, resource_id):
        if resource_id not in scope.TARGETS:
            raise ValueError("Deletion is limited to the 13 reviewed resource IDs")
        version = scope.TARGETS[resource_id]
        item = self.get(resource_id, version)
        checked_resource(item, resource_id)
        if item is None:
            return
        # Refresh before each deletion; a previous resource may take minutes.
        self._token = scope.access_token()
        url = f"https://management.azure.com{resource_id}?api-version={version}"
        if resource_id == scope.COMPUTE:
            # Detach would leave the underlying compute behind.
            # https://learn.microsoft.com/en-us/rest/api/azureml/compute/delete?view=rest-azureml-2025-09-01
            url += "&underlyingResourceAction=Delete"
        response = requests.delete(url, headers={"Authorization": f"Bearer {self._token}"},
                                   timeout=(10, 35), allow_redirects=False)
        if response.status_code not in {200, 202, 204}:
            raise RuntimeError(f"DELETE returned HTTP {response.status_code}; stop and inspect")
        # Accepted is not completed. Only an exact-target GET 404 confirms absence.
        deadline = time.monotonic() + DELETE_TIMEOUT
        while time.monotonic() < deadline:
            item = self.get(resource_id, version)
            if item is None:
                return
            checked_resource(item, resource_id)
            if (item.get("properties") or {}).get("provisioningState") in {"Failed", "Canceled"}:
                raise RuntimeError("Resource deletion failed; stop and inspect")
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        raise TimeoutError("Deletion not confirmed within 600 seconds; stop and inspect")


def check_ci_quiet():
    """Public GitHub GETs only. Never disable, cancel, rerun or send Azure tokens."""
    def read(suffix=""):
        response = requests.get(WORKFLOW + suffix, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        }, timeout=(10, 35), allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"CI readback returned HTTP {response.status_code}; not confirmed")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid CI readback")
        return payload

    workflow = read()
    if (workflow.get("path") != ".github/workflows/lab4-ci.yml"
            or workflow.get("name") != "Lab 4 CI" or workflow.get("state") != "disabled_manually"
            or type(workflow.get("id")) is not int):
        raise ValueError("Lab 4 CI is not confirmed manually disabled")
    seen, total = set(), None
    for page in range(1, 51):
        payload = read(f"/runs?per_page=100&page={page}")
        runs, count = payload.get("workflow_runs"), payload.get("total_count")
        if (not isinstance(runs, list) or type(count) is not int or count < 0
                or (total is not None and total != count)):
            raise ValueError("Incomplete or changing CI run inventory")
        total = count
        for run in runs:
            if (not isinstance(run, dict) or type(run.get("id")) is not int
                    or run["id"] in seen or run.get("workflow_id") != workflow["id"]):
                raise ValueError("Invalid or repeated CI run")
            if run.get("status") != "completed":
                raise ValueError("Lab 4 CI has an unfinished or unknown run; wait before teardown")
            seen.add(run["id"])
        if len(seen) == total:
            return
        if not runs or len(seen) > total:
            break
    raise ValueError("CI run inventory is incomplete; teardown refused")


def checked_resource(item, resource_id):
    if item is None:
        return {"id": resource_id, "state": "absent"}
    expected_type = ("Microsoft.Authorization/roleAssignments" if resource_id in scope.GRANTS
                     else resource_id.split("/providers/", 1)[1].rsplit("/", 1)[0])
    # Nested AML resource types omit the parent workspace name.
    expected_type = expected_type.replace("/mlw-itcs355-u6688124", "")
    # The schedule GET (2025-09-01) omits type in the observed Azure response.
    # Still require the exact ID and name; do not relax type checks on other resources.
    type_ok = (item.get("type") or "").lower() == expected_type.lower()
    if resource_id == scope.SCHEDULE and item.get("type") is None:
        type_ok = item.get("name") == "lab4-drift-schedule-check"
    if item.get("id", "").lower() != resource_id.lower() or not type_ok:
        raise ValueError(f"Returned identity/type differs for {resource_id.rsplit('/', 1)[-1]}")
    props = item.get("properties") or {}
    row = {"id": resource_id, "state": "present"}
    if resource_id in scope.GRANTS:
        assigned_scope, principal, role = scope.GRANTS[resource_id]
        if (props.get("scope", "").lower() != assigned_scope.lower()
                or props.get("principalId", "").lower() != principal
                or props.get("roleDefinitionId", "").lower()
                != f"/subscriptions/{scope.SUBSCRIPTION}/providers/microsoft.authorization/roledefinitions/{role}"):
            raise ValueError("Role assignment has changed; review before cleanup")
    else:
        tags = item.get("tags") or props.get("tags") or {}
        expected = ("ITCS355", "lab4") if resource_id in {scope.RULE, scope.ACTION_GROUP} else ("itcs355", "4")
        if (tags.get("course"), tags.get("lab")) != expected:
            raise ValueError("Reviewed resource tags have changed")
        if resource_id in scope.IDENTITIES and props.get("principalId") != scope.IDENTITIES[resource_id]:
            raise ValueError("Managed identity principal changed")
    if resource_id == scope.SCHEDULE:
        row["enabled"] = props.get("isEnabled")
    elif resource_id == scope.RULE:
        row["enabled"] = props.get("enabled")
    elif resource_id == scope.COMPUTE:
        compute = props.get("properties") or {}
        row.update(current_nodes=compute.get("currentNodeCount"), target_nodes=compute.get("targetNodeCount"))
    elif resource_id == scope.APP:
        row["running_status"] = props.get("runningStatus")
    return row


def validate_config(cfg):
    scope.resource_scope(cfg)
    if (cfg.endpoint_name != "itcs355-lab4-staging"
            or cfg.azure_ml_workspace != "mlw-itcs355-u6688124"):
        raise ValueError("Endpoint/workspace differs from the reviewed Lab 4 scope; "
                         "set ENDPOINT_NAME and AZURE_ML_WORKSPACE")


def preview(cfg, reader=None):
    validate_config(cfg)
    reader = reader if reader is not None else ReadOnlyArm()
    rows = [checked_resource(reader.get(rid, api), rid) for rid, api in scope.TARGETS.items()]
    blockers = []
    for row in rows:
        if row["state"] == "absent":
            continue
        if row["id"] in {scope.SCHEDULE, scope.RULE} and row.get("enabled") is not False:
            blockers.append("Schedule/alert is enabled or its state is unknown")
        if row["id"] == scope.COMPUTE and (row["current_nodes"] != 0 or row["target_nodes"] != 0):
            blockers.append("Drift compute is not confirmed at zero nodes")
    app_present = any(r["id"] == scope.APP and r["state"] == "present" for r in rows)
    revisions = reader.list(scope.APP + "/revisions", "2025-07-01") if app_present else []
    if app_present and (not revisions or any((r.get("properties") or {}).get("replicas") != 0 for r in revisions)):
        blockers.append("Staging replicas are not confirmed at zero")
    # Explicitly include disabled schedules; Azure's default list hides them.
    schedules = reader.list(scope.WORKSPACE + "/schedules?listViewType=All", "2025-09-01")
    if any(s.get("id", "").lower() != scope.SCHEDULE.lower() for s in schedules):
        blockers.append("Additional AML schedule found; review compute dependencies")
    jobs = reader.list(scope.WORKSPACE + "/jobs", "2025-09-01")
    terminal = {"Completed", "Failed", "Canceled", "Cancelled"}
    if any((job.get("properties") or {}).get("status") not in terminal for job in jobs):
        blockers.append("An AML job is active or has an unknown status")
    return {
        "mode": "preview_only", "checked_at": datetime.now(timezone.utc).isoformat(),
        "deletion_enabled": False, "teardown_complete": False,
        "reviewed_candidates": rows, "blockers": blockers,
        "retained": ["CI, push and deploy identities and their grants",
                     "Shared workspace, registry, storage, Key Vault, monitoring and logs",
                     "Models, images, job history, Workbook and role definitions"],
        "next_review": "Before confirmed deletion: disable Lab 4 CI, finish all runs and recheck dependencies.",
    }


def dependency_check(reader, rows):
    """Fail closed on consumers beyond the reviewed one-workspace inventory."""
    present = {r["id"] for r in rows if r["state"] == "present"}
    inventory = reader.list(SUB + "/resources", "2021-04-01")
    # Additional alert rules require a review, even if they may be unrelated.
    alert_types = {"microsoft.insights/scheduledqueryrules", "microsoft.insights/metricalerts",
                   "microsoft.insights/activitylogalerts", "microsoft.insights/alertrules",
                   "microsoft.alertsmanagement/smartdetectoralertrules",
                   "microsoft.alertsmanagement/actionrules"}
    for item in inventory:
        kind, rid = item.get("type", "").lower(), item.get("id", "").lower()
        if not kind or not rid:
            raise ValueError("Incomplete subscription inventory")
        if kind == "microsoft.machinelearningservices/workspaces" and rid != scope.WORKSPACE.lower():
            raise ValueError("Additional AML workspace requires dependency review")
        if kind in alert_types and rid != scope.RULE.lower():
            raise ValueError("Additional alert rule requires email-channel dependency review")

    consumers = list(inventory)
    for kind in ("containerApps", "jobs"):
        items = reader.list(SUB + f"/providers/Microsoft.App/{kind}", "2025-07-01")
        consumers.extend(items)
        for item in items:
            props = item.get("properties") or {}
            env = props.get("environmentId") or props.get("managedEnvironmentId") or ""
            if env.lower() == scope.ENVIRONMENT.lower() and item.get("id", "").lower() != scope.APP.lower():
                raise ValueError("Another app/job uses the Lab 4 environment")
    consumers.extend(reader.list(scope.WORKSPACE + "/computes", "2025-09-01"))
    allowed = {scope.RUNTIME.lower(): scope.APP.lower(), scope.DRIFT_IDENTITY.lower(): scope.COMPUTE.lower()}
    for item in consumers:
        assigned = (item.get("identity") or {}).get("userAssignedIdentities") or {}
        for identity in assigned:
            if identity.lower() in allowed and item.get("id", "").lower() != allowed[identity.lower()]:
                raise ValueError("Another resource uses a Lab 4 runtime/drift identity")
    for rid, identity in ((scope.APP, scope.RUNTIME), (scope.COMPUTE, scope.DRIFT_IDENTITY)):
        if rid in present:
            item = reader.get(rid, scope.TARGETS[rid])
            checked_resource(item, rid)
            if item is None:
                raise ValueError("Resource inventory changed during preflight")
            props = item.get("properties") or {}
            assigned = (item.get("identity") or {}).get("userAssignedIdentities") or {}
            if (props.get("provisioningState") != "Succeeded"
                    or {key.lower() for key in assigned} != {identity.lower()}):
                raise ValueError("Reviewed runtime/compute identity or provisioning state changed")
            if rid == scope.APP and (props.get("environmentId") or "").lower() != scope.ENVIRONMENT.lower():
                raise ValueError("Staging environment changed")
            if rid == scope.COMPUTE and (props.get("computeType") != "AmlCompute"
                                        or props.get("isAttachedCompute") is not False):
                raise ValueError("Compute is not the reviewed managed AML cluster")
    for identity, expected in ((scope.RUNTIME, scope.APP), (scope.DRIFT_IDENTITY, scope.COMPUTE)):
        if identity not in present:
            continue
        associated = reader.associated(identity)
        # A deleted resource can remain briefly in Azure's associated-resource index.
        # Only the exact reviewed consumer is allowed; its absence is checked via GET.
        if any(item.get("id", "").lower() != expected.lower() for item in associated):
            raise ValueError("Another resource is associated with a Lab 4 identity")
        if reader.list(identity + "/federatedIdentityCredentials", "2023-01-31"):
            raise ValueError("Runtime/drift identity has unreviewed federated credentials")
    grants = reader.list(SUB + "/providers/Microsoft.Authorization/roleAssignments", "2022-04-01")
    for grant in grants:
        props = grant.get("properties") or {}
        if not grant.get("id") or not props.get("principalId"):
            raise ValueError("Incomplete role-assignment inventory")
        if (props["principalId"].lower() in scope.IDENTITIES.values()
                and grant["id"].lower() not in {rid.lower() for rid in scope.GRANTS}):
            raise ValueError("Runtime/drift identity has an unreviewed role assignment")


def teardown(cfg, confirm="", reader=None):
    validate_config(cfg)
    if confirm not in {"", CONFIRM}:
        raise ValueError("Confirm the exact reviewed Lab 4 app name")
    if not confirm:
        return preview(cfg, reader)
    check_ci_quiet()
    reader = reader if reader is not None else DeletionArm(confirm)
    deleted, absent = [], []
    for index, rid in enumerate(scope.TARGETS):
        # Recheck before each operation; never continue blindly after partial cleanup.
        snapshot = preview(cfg, reader)
        if snapshot["blockers"]:
            raise ValueError("; ".join(snapshot["blockers"]))
        rows = snapshot["reviewed_candidates"]
        if any(row["state"] != "absent" for row in rows[:index]):
            raise ValueError("Earlier target reappeared; dependent deletion refused")
        dependency_check(reader, rows)
        check_ci_quiet()
        if rows[index]["state"] == "absent":
            absent.append(rid)
            continue
        reader.delete(rid)
        if reader.get(rid, scope.TARGETS[rid]) is not None:
            raise RuntimeError("Deletion not confirmed; dependent deletion refused")
        deleted.append(rid)
        print(json.dumps({"confirmed_absent_after_delete": rid,
                          "utc": datetime.now(timezone.utc).isoformat()}), flush=True)
    if any(reader.get(rid, api) is not None for rid, api in scope.TARGETS.items()):
        raise RuntimeError("A reviewed target is still present; teardown is not complete")
    return {"mode": "complete", "teardown_complete": True,
            "scope": "13 reviewed Lab 4 runtime resources only; not the shared services",
            "confirmed_absent_after_delete": deleted, "already_absent": absent,
            "retained": snapshot["retained"], "blockers": [],
            "checked_at": datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", default="", help=f"Delete only when set to {CONFIRM}; otherwise preview")
    args = parser.parse_args()
    try:
        result = teardown(config.load(strict=False), args.confirm)
    except (ValueError, RuntimeError) as exc:
        print(f"Teardown stopped: {exc}. Earlier deletions, if any, are not undone.", file=sys.stderr)
        return 1
    except (OSError, requests.RequestException) as exc:
        # Request exceptions may include private URLs/headers: print the type only.
        print(f"Teardown failed ({type(exc).__name__}); inspect partial state before rerunning.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if result["mode"] == "preview_only":
        print("PREVIEW ONLY: no resources deleted; teardown is NOT complete.")
    return 1 if result["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
