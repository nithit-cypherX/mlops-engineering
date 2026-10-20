"""Reviewed Lab 5 staging targets; pure checks, not a deployment or teardown tool.

Adapted from Lab 4's exact-resource and ownership checks. The shared environment
is an existing prerequisite, never a Lab 5-owned update/deletion target.
"""
from __future__ import annotations

from uuid import UUID

from cloudlayer.azure_config import StagingConfig

SUBSCRIPTION = "d9385e82-8bee-4612-8a76-967c241112c8"
GROUP = "itcs355-u6688124"
REGION = "malaysiawest"
SCOPE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
APP_NAME = "lab5-staging"
ENVIRONMENT_NAME = "feedbackpulse-env"
APP = f"{SCOPE}/providers/Microsoft.App/containerApps/{APP_NAME}"
ENVIRONMENT = f"{SCOPE}/providers/Microsoft.App/managedEnvironments/{ENVIRONMENT_NAME}"
RUNTIME = (f"{SCOPE}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
           "id-itcs355-u6688124-lab5-runtime")
TAGS = {"course": "itcs355", "student": GROUP, "lab": "5"}


def _same_id(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and actual.casefold() == expected.casefold()


def validate_config(cfg: StagingConfig) -> None:
    """Refuse configuration that redirects staging to a different resource."""
    if (
        cfg.provider != "azure"
        or not _same_id(cfg.azure_subscription_id, SUBSCRIPTION)
        or not _same_id(cfg.project_id, GROUP)
        or cfg.region != REGION
        or cfg.endpoint_name != APP_NAME
        or cfg.azure_containerapps_environment != ENVIRONMENT_NAME
        or not _same_id(cfg.azure_managed_identity_id, RUNTIME)
        or cfg.serving_instance != "0.5cpu-1Gi"
    ):
        raise ValueError("Configuration differs from the reviewed Lab 5 staging scope")


def require_owned_target(resource_id: str) -> None:
    """Exact allowlist only; passing is not permission to perform a cloud action."""
    if not any(_same_id(resource_id, expected) for expected in (APP, RUNTIME)):
        raise ValueError("Target is not a reviewed Lab 5-owned resource")


def _require_owned(resource: dict, expected_id: str) -> None:
    require_owned_target(resource.get("id"))
    if not _same_id(resource["id"], expected_id):
        raise ValueError("Unexpected Lab 5 resource type or ID")
    tags = resource.get("tags") or {}
    if not isinstance(tags, dict) or any(tags.get(key) != value for key, value in TAGS.items()):
        raise ValueError("Resource does not have the required Lab 5 ownership tags")


def require_shared_environment(resource: dict) -> None:
    """Accept the exact ready shared environment without retagging or modifying it."""
    if not _same_id(resource.get("id"), ENVIRONMENT):
        raise ValueError("Environment is not the reviewed shared environment")
    props = resource.get("properties") or {}
    if (
        resource.get("location", "").lower().replace(" ", "") != REGION
        or props.get("provisioningState") != "Succeeded"
    ):
        raise ValueError("Shared environment must be ready in Malaysia West")
    if not any(
        profile.get("name") == "Consumption"
        and profile.get("workloadProfileType") == "Consumption"
        for profile in (props.get("workloadProfiles") or [])
    ):
        raise ValueError("Shared environment must provide the Consumption profile")


def require_lab5_app(resource: dict) -> None:
    """Check an existing app before any future update or cleanup."""
    _require_owned(resource, APP)
    props = resource.get("properties") or {}
    environment_ids = [
        props[key] for key in ("environmentId", "managedEnvironmentId") if key in props
    ]
    if not environment_ids or any(
        not _same_id(value, ENVIRONMENT) for value in environment_ids
    ):
        raise ValueError("Lab 5 app must use the reviewed shared environment")
    if (props.get("configuration") or {}).get("activeRevisionsMode") != "Single":
        raise ValueError("Lab 5 staging must use Single revision mode")


def require_runtime_identity(resource: dict) -> None:
    """Check ownership and identity metadata; this does not prove RBAC access."""
    _require_owned(resource, RUNTIME)
    if resource.get("location", "").lower().replace(" ", "") != REGION:
        raise ValueError("Runtime identity must be in Malaysia West")
    props = resource.get("properties") or {}
    for key in ("principalId", "clientId"):
        value = props.get(key)
        if not isinstance(value, str):
            raise ValueError(f"Runtime identity is missing {key}")
        try:
            identifier = UUID(value)
        except ValueError:
            raise ValueError(f"Runtime identity has invalid {key}") from None
        if identifier.int == 0:
            raise ValueError(f"Runtime identity has invalid {key}")
