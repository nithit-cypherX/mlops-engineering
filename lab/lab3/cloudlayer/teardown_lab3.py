"""Fixed-scope Lab 3 cleanup; preview unless the exact app name is confirmed.

Do not deploy or change role assignments while teardown is running. Shared
services, models, images, logs and the custom role definition are retained.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

from cloudlayer.azure import AzureAdapter
from src import config

# Inventory reviewed on 2026-09-24. Config/tags cannot widen this deletion list.
SUBSCRIPTION = "d9385e82-8bee-4612-8a76-967c241112c8"
GROUP = "itcs355-u6688124"
SCOPE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
APP_NAME = "ca-itcs355-u6688124-lab3"
APP = f"{SCOPE}/providers/Microsoft.App/containerApps/{APP_NAME}"
ENVIRONMENT = f"{SCOPE}/providers/Microsoft.App/managedEnvironments/cae-itcs355-u6688124-lab3"
IDENTITY = f"{SCOPE}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-itcs355-u6688124-lab3"
PRINCIPAL = "14012060-3ce7-4438-a324-844f522e5ad7"
ACR = f"{SCOPE}/providers/Microsoft.ContainerRegistry/registries/itcs355u6688124"
WORKSPACE = f"{SCOPE}/providers/Microsoft.MachineLearningServices/workspaces/mlw-itcs355-u6688124"
ROLES = {
    f"{ACR}/providers/Microsoft.Authorization/roleAssignments/3c3c14a6-1761-460a-97aa-a557106a1838":
        (ACR, "7f951dda-4ed3-4680-a7ca-43fe172d538d"),
    f"{WORKSPACE}/providers/Microsoft.Authorization/roleAssignments/5b1c77e8-276b-4097-ba93-6f1ef950175c":
        (WORKSPACE, "9dd7081e-5f6b-493b-a1d2-fa48cee2a82f"),
}
APP_API = "2025-07-01"
IDENTITY_API = "2023-01-31"
ROLE_API = "2022-04-01"
# Deletion order: app, empty environment, two exact grants, then identity.
TARGETS = {APP: APP_API, ENVIRONMENT: APP_API,
           **{rid: ROLE_API for rid in ROLES}, IDENTITY: IDENTITY_API}
APPROVED_SCOPE = {
    "provider": "azure", "azure_subscription_id": SUBSCRIPTION, "project_id": GROUP,
    "region": "malaysiawest", "endpoint_name": APP_NAME,
    "azure_containerapps_environment": ENVIRONMENT.rsplit("/", 1)[1],
    "azure_managed_identity_id": IDENTITY, "identity_ref": PRINCIPAL,
    "azure_ml_workspace": "mlw-itcs355-u6688124",
}
TAGS = {"course": "itcs355", "student": GROUP, "lab": "3"}
RETAINED = "Shared ACR, Storage, Azure ML, models, logs and custom role definition are retained."
DELETE_TIMEOUT = 600


class Arm:
    """Existing CLI credentials, exact HTTP status checks, no automatic retries."""

    def __init__(self):
        account = AzureAdapter._az_json(["account", "show"])
        if account.get("id", "").lower() != SUBSCRIPTION or account.get("state") != "Enabled":
            raise ValueError("Active Azure subscription differs from the reviewed scope")
        self.credential = AzureCliCredential(process_timeout=30)

    def request(self, method, rid, version):
        if method not in {"GET", "DELETE"} or not rid.startswith(f"/subscriptions/{SUBSCRIPTION}/"):
            raise ValueError("Unexpected management operation")
        if method == "DELETE" and (rid not in TARGETS or version != TARGETS[rid]):
            raise ValueError("Deletion is limited to the five reviewed resource IDs")
        token = self.credential.get_token("https://management.azure.com/.default").token
        response = requests.request(
            method, f"https://management.azure.com{rid}?api-version={version}",
            headers={"Authorization": f"Bearer {token}"}, timeout=30, allow_redirects=False,
        )
        if response.status_code == 404 and rid in TARGETS:
            return None
        allowed = {200} if method == "GET" else {200, 202, 204}
        if response.status_code not in allowed:
            # Never print response bodies, credentials or pretend 403/429 means absent.
            raise RuntimeError(f"{method} {rid}: HTTP {response.status_code}; not confirmed")
        return response.json() if method == "GET" else None

    def get(self, rid, version):
        return self.request("GET", rid, version)

    def list(self, rid, version):
        data = self.get(rid, version)
        if not isinstance(data, dict) or not isinstance(data.get("value"), list) or data.get("nextLink"):
            raise ValueError("Incomplete or paged inventory requires review; refusing deletion")
        return data["value"]

    def delete(self, rid):
        self.request("DELETE", rid, TARGETS[rid])
        deadline = time.monotonic() + DELETE_TIMEOUT
        while time.monotonic() < deadline:
            if self.get(rid, TARGETS[rid]) is None:
                return
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        raise TimeoutError(f"Resource still present after delete: {rid}; not confirmed")


def checked_target(client, rid):
    resource = client.get(rid, TARGETS[rid])
    if resource is None:
        return None
    expected_type = ("Microsoft.Authorization/roleAssignments" if rid in ROLES
                     else rid.split("/providers/", 1)[1].rsplit("/", 1)[0])
    if (not isinstance(resource, dict) or resource.get("id", "").lower() != rid.lower()
            or resource.get("type", "").lower() != expected_type.lower()
            or resource.get("name") != rid.rsplit("/", 1)[1]):
        raise ValueError(f"Resource identity/type does not match: {rid}")
    props = resource.get("properties") or {}
    if rid in ROLES:
        scope, role = ROLES[rid]
        expected_role = f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{role}"
        if (props.get("principalId", "").lower() != PRINCIPAL
                or props.get("scope", "").lower() != scope.lower()
                or props.get("roleDefinitionId", "").lower() != expected_role.lower()):
            raise ValueError("Role assignment principal, scope or role changed")
    else:
        if (any((resource.get("tags") or {}).get(k) != v for k, v in TAGS.items())
                or resource.get("location", "").lower().replace(" ", "") != "malaysiawest"):
            raise ValueError(f"Resource tags/location do not match Lab 3: {rid}")
        if rid == IDENTITY:
            if props.get("principalId", "").lower() != PRINCIPAL:
                raise ValueError("Managed identity principal changed")
        elif props.get("provisioningState") != "Succeeded":
            raise ValueError("App/environment is not in a stable Succeeded state")
    if rid == APP:
        assigned = (resource.get("identity") or {}).get("userAssignedIdentities") or {}
        if (props.get("runningStatus") != "Stopped"
                or props.get("environmentId", "").lower() != ENVIRONMENT.lower()
                or {key.lower() for key in assigned} != {IDENTITY.lower()}):
            raise ValueError("Expected the stopped Lab 3 app, environment and identity")
        revisions = client.list(APP + "/revisions", APP_API)
        if not revisions or any(
            not r.get("name", "").startswith(APP_NAME + "--")
            or (r.get("properties") or {}).get("replicas") != 0
            or (r.get("properties") or {}).get("runningState") != "Stopped"
            for r in revisions
        ):
            raise ValueError("All app revisions must be stopped with zero replicas")
    return resource


def preflight(client):
    resources = {rid: checked_target(client, rid) for rid in TARGETS}
    if not any(resource is not None for resource in resources.values()):
        return resources
    # Query the whole subscription for consumers, not just lab=3 tags.
    for kind in ("containerApps", "jobs"):
        for item in client.list(f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.App/{kind}", APP_API):
            props = item.get("properties") or {}
            env = props.get("environmentId") or props.get("managedEnvironmentId") or ""
            identities = (item.get("identity") or {}).get("userAssignedIdentities") or {}
            uses = env.lower() == ENVIRONMENT.lower() or IDENTITY.lower() in {v.lower() for v in identities}
            if uses and item.get("id", "").lower() != APP.lower():
                raise ValueError("Another app/job uses the Lab 3 environment or identity")
    if resources[IDENTITY] is not None:
        associated = AzureAdapter._az_json(["identity", "list-resources", "--ids", IDENTITY])
        if not isinstance(associated, list) or any(item.get("id", "").lower() != APP.lower() for item in associated):
            raise ValueError("Another resource uses the Lab 3 identity")
        federated = AzureAdapter._az_json([
            "identity", "federated-credential", "list", "--identity-name", IDENTITY.rsplit("/", 1)[1],
            "--resource-group", GROUP, "--subscription", SUBSCRIPTION,
        ])
        if federated != []:
            raise ValueError("Identity has unreviewed federated credentials")
    grants = AzureAdapter._az_json([
        "role", "assignment", "list", "--all", "--fill-principal-name", "false",
        "--subscription", SUBSCRIPTION, "--query", f"[?principalId=='{PRINCIPAL}'].id",
    ])
    if not isinstance(grants, list) or any(not isinstance(rid, str) or rid.lower() not in
                                         {key.lower() for key in ROLES} for rid in grants):
        raise ValueError("Identity has an unreviewed role assignment")
    return resources


def teardown(cfg, confirm=""):
    if any(str(getattr(cfg, field, "")).lower() != value.lower() for field, value in APPROVED_SCOPE.items()):
        raise ValueError("Configuration differs from the reviewed Lab 3 teardown scope")
    if confirm not in {"", APP_NAME}:
        raise ValueError("Confirm the exact Lab 3 app name; nothing deleted")
    client = Arm()
    resources = preflight(client)
    present = [rid for rid, value in resources.items() if value is not None]
    if not confirm:
        return {"mode": "preview", "would_delete": present, "already_absent": [rid for rid in TARGETS if rid not in present]}
    deleted = []
    for rid in present:
        # Recheck the remaining targets/dependencies after each confirmed deletion.
        # This also allows a later explicit rerun to continue after partial cleanup.
        if deleted:
            resources = preflight(client)
        earlier = list(TARGETS)[:list(TARGETS).index(rid)]
        if any(resources[previous] is not None for previous in earlier):
            raise RuntimeError("An earlier target is still present; refusing dependent deletion")
        if resources[rid] is None:
            continue
        client.delete(rid)
        deleted.append(rid)
        print(json.dumps({"confirmed_deleted": rid, "utc": datetime.now(timezone.utc).isoformat()}), flush=True)
    if any(client.get(rid, version) is not None for rid, version in TARGETS.items()):
        raise RuntimeError("A reviewed resource is still present; teardown is not complete")
    return {"mode": "complete", "confirmed_deleted": deleted, "all_five_absent": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", default="", help=f"Delete only when set to {APP_NAME}; otherwise preview")
    args = parser.parse_args()
    try:
        result = teardown(config.load(strict=False), args.confirm)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError,
            AzureError, requests.RequestException) as exc:
        print(f"Teardown stopped: {exc}. Earlier confirmed deletions are not undone.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print(RETAINED)
    if result["mode"] == "preview":
        print("Preview only; no resources were deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
