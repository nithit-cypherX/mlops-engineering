"""Reviewed Lab 4 resource IDs (5 October 2026), not a tag-based deletion policy.

Shared by the guarded teardown and read-only billing report.
The subscription/group cannot be widened through environment variables.
"""
from __future__ import annotations

import os
from datetime import date
from tempfile import TemporaryDirectory

from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

SUBSCRIPTION = "d9385e82-8bee-4612-8a76-967c241112c8"
GROUP = "itcs355-u6688124"
SCOPE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
START = date(2026, 9, 29)  # First Lab 4 CI/image work: commits 1cbb420 and b6bf4c6.


def rid(suffix):
    return f"{SCOPE}/providers/{suffix}"


WORKSPACE = rid("Microsoft.MachineLearningServices/workspaces/mlw-itcs355-u6688124")
APP = rid("Microsoft.App/containerApps/itcs355-lab4-staging")
ENVIRONMENT = rid("Microsoft.App/managedEnvironments/cae-itcs355-u6688124-lab4")
COMPUTE = WORKSPACE + "/computes/cpu-lab4-drift"
SCHEDULE = WORKSPACE + "/schedules/lab4-drift-schedule-check"
RULE = rid("Microsoft.Insights/scheduledQueryRules/lab4-drift-load-pct-psi")
ACTION_GROUP = rid("Microsoft.Insights/actionGroups/lab4-drift-email")
RUNTIME = rid("Microsoft.ManagedIdentity/userAssignedIdentities/id-itcs355-u6688124-lab4-runtime")
DRIFT_IDENTITY = rid("Microsoft.ManagedIdentity/userAssignedIdentities/id-itcs355-u6688124-lab4-drift")
IDENTITIES = {RUNTIME: "8e01a2b5-b534-4d7e-a5fb-2540df310284",
              DRIFT_IDENTITY: "6d336efb-333f-40ee-a575-da752a1a7e4a"}
ACR = rid("Microsoft.ContainerRegistry/registries/itcs355u6688124")
LOGS = rid("Microsoft.OperationalInsights/workspaces/mlwitcs3logalytif5574ef1")
INSIGHTS = rid("Microsoft.Insights/components/mlwitcs3insights48346e2c")
# Exact assignment ID -> (scope, principal, role definition ID).
GRANTS = {
    ACR + "/providers/Microsoft.Authorization/roleAssignments/33335bfb-ce53-5a8d-b766-7d5615d8981f":
        (ACR, IDENTITIES[RUNTIME], "7f951dda-4ed3-4680-a7ca-43fe172d538d"),
    WORKSPACE + "/providers/Microsoft.Authorization/roleAssignments/c1c59a9a-ab0c-5fce-a73a-2a130b9507be":
        (WORKSPACE, IDENTITIES[RUNTIME], "9dd7081e-5f6b-493b-a1d2-fa48cee2a82f"),
    LOGS + "/providers/Microsoft.Authorization/roleAssignments/8682f6fb-a1eb-53be-988a-6d9a1793bd67":
        (LOGS, IDENTITIES[DRIFT_IDENTITY], "73c42c96-874c-492b-b04d-ab87d138a893"),
    INSIGHTS + "/providers/Microsoft.Authorization/roleAssignments/e1972e28-b105-50cb-b8a2-6a4b1fb5c0d7":
        (INSIGHTS, IDENTITIES[DRIFT_IDENTITY], "3913510d-42f4-4e42-8a64-420c390055eb"),
    ACR + "/providers/Microsoft.Authorization/roleAssignments/9bed574c-924a-5340-9bad-c16ddf730a00":
        (ACR, IDENTITIES[DRIFT_IDENTITY], "7f951dda-4ed3-4680-a7ca-43fe172d538d"),
}
TARGETS = {
    SCHEDULE: "2025-09-01", RULE: "2023-12-01", ACTION_GROUP: "2023-01-01",
    APP: "2025-07-01", ENVIRONMENT: "2025-07-01", COMPUTE: "2025-09-01",
    **{x: "2022-04-01" for x in GRANTS},
    **{x: "2023-01-31" for x in IDENTITIES},
}


def resource_scope(cfg):
    if (cfg.provider != "azure" or cfg.azure_subscription_id.lower() != SUBSCRIPTION
            or cfg.project_id.lower() != GROUP):
        raise ValueError("Configuration differs from the reviewed Lab 4 scope; "
                         "set CLOUD_PROVIDER, PROJECT_ID and AZURE_SUBSCRIPTION_ID")
    return SCOPE


def reviewed_resources(cfg):
    """Exact billing IDs; no prefix matching or assigning shared spend to Lab 4."""
    resource_scope(cfg)
    entries = {
        APP: ("Staging app", "Lab 4"), ENVIRONMENT: ("Staging environment", "Lab 4"),
        COMPUTE: ("Drift compute", "Lab 4"), SCHEDULE: ("Drift schedule", "Lab 4"),
        RULE: ("Drift alert", "Lab 4"), ACTION_GROUP: ("Drift email channel", "Lab 4"),
        ACR: ("ACR", "Shared"), LOGS: ("Log Analytics", "Shared"),
        INSIGHTS: ("Application Insights", "Shared"),
        WORKSPACE: ("Azure ML workspace", "Shared"),
        rid("Microsoft.Storage/storageAccounts/itcs355u6688124"): ("Storage", "Shared"),
        rid("Microsoft.KeyVault/vaults/mlwitcs3keyvault8263948e"): ("Key Vault", "Shared"),
        rid("microsoft.insights/actiongroups/Application Insights Smart Detection"):
            ("Existing Insights action group", "Shared"),
        rid("microsoft.insights/workbooks/43B25150-A9E4-42DA-949B-C51CCF7BA2B2"):
            ("Lab 4 Workbook (retained)", "Lab 4"),
    }
    for name in ("ci", "push", "deploy", "runtime", "drift"):
        entries[rid(f"Microsoft.ManagedIdentity/userAssignedIdentities/id-itcs355-u6688124-lab4-{name}")] = (
            f"Lab 4 {name} identity", "Lab 4")
    return {key.lower(): value for key, value in entries.items()}


def access_token():
    """Use existing login; isolate CLI extensions for this call, then restore env.

The installed ML extension previously broke core CLI commands. Core token lookup
needs no extensions; this does not install, remove or persist any CLI setting.
"""
    keys = ("AZURE_EXTENSION_DIR", "AZURE_EXTENSION_USE_DYNAMIC_INSTALL")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        with TemporaryDirectory(prefix="lab4-cli-") as directory:
            os.environ.update(AZURE_EXTENSION_DIR=directory, AZURE_EXTENSION_USE_DYNAMIC_INSTALL="no")
            with AzureCliCredential(process_timeout=30) as credential:
                return credential.get_token("https://management.azure.com/.default").token
    except AzureError:
        raise RuntimeError("Azure CLI authentication failed; check az login") from None
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
