"""Scope checks use ARM-shaped fixtures only; no cloud calls."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from cloudlayer import lab5_scope as scope
from cloudlayer.azure_config import STAGING_SLOTS, load_staging

EXAMPLE = Path(__file__).resolve().parents[2] / "cloud.env.example"


@pytest.fixture
def cfg(monkeypatch):
    for key in (*STAGING_SLOTS, "SERVING_INSTANCE"):
        monkeypatch.delenv(key, raising=False)
    return load_staging(EXAMPLE)


@pytest.fixture
def environment():
    return {
        "id": scope.ENVIRONMENT,
        "location": "Malaysia West",
        "tags": {"project": "feedbackpulse"},
        "properties": {
            "provisioningState": "Succeeded",
            "workloadProfiles": [
                {"name": "Consumption", "workloadProfileType": "Consumption"}
            ],
        },
    }


@pytest.fixture
def app():
    return {
        "id": scope.APP,
        "tags": dict(scope.TAGS),
        "properties": {
            "environmentId": scope.ENVIRONMENT,
            "configuration": {"activeRevisionsMode": "Single"},
        },
    }


@pytest.fixture
def identity():
    return {
        "id": scope.RUNTIME,
        "location": "malaysiawest",
        "tags": dict(scope.TAGS),
        "properties": {
            "principalId": "11111111-1111-4111-8111-111111111111",
            "clientId": "22222222-2222-4222-8222-222222222222",
        },
    }


def test_example_matches_the_reviewed_targets(cfg):
    scope.validate_config(cfg)
    assert scope.APP.endswith("/containerApps/lab5-staging")
    assert scope.ENVIRONMENT.endswith("/managedEnvironments/feedbackpulse-env")
    assert scope.RUNTIME.endswith("/id-itcs355-u6688124-lab5-runtime")


@pytest.mark.parametrize("field,value", [
    ("provider", "local"),
    ("project_id", "another-group"),
    ("region", "eastasia"),
    ("azure_subscription_id", "11111111-1111-4111-8111-111111111111"),
    ("endpoint_name", "feedbackpulse-api"),
    ("endpoint_name", "lab5-staging/../feedbackpulse-api"),
    ("azure_containerapps_environment", "another-environment"),
    ("azure_managed_identity_id", scope.SCOPE + "/providers/Microsoft.ManagedIdentity/"
     "userAssignedIdentities/id-feedbackpulse-pull"),
    ("azure_managed_identity_id", scope.RUNTIME + "/child"),
    ("serving_instance", "1cpu-2Gi"),
    ("serving_instance", ""),
])
def test_config_rejects_unapproved_targets_and_size(cfg, field, value):
    with pytest.raises(ValueError):
        scope.validate_config(replace(cfg, **{field: value}))


def test_guard_rejects_exported_override(cfg, monkeypatch):
    monkeypatch.setenv("ENDPOINT_NAME", "feedbackpulse-api")
    with pytest.raises(ValueError, match="staging scope"):
        scope.validate_config(load_staging(EXAMPLE))


def test_arm_id_case_is_not_a_different_resource(cfg, app, environment, identity):
    scope.validate_config(replace(
        cfg, azure_subscription_id=cfg.azure_subscription_id.upper(),
        project_id=cfg.project_id.upper(),
        azure_managed_identity_id=cfg.azure_managed_identity_id.upper(),
    ))
    for resource in (app, environment, identity):
        resource["id"] = resource["id"].upper()
    scope.require_lab5_app(app)
    scope.require_shared_environment(environment)
    scope.require_runtime_identity(identity)


def test_shared_environment_keeps_its_original_tags(environment):
    before = deepcopy(environment)
    scope.require_shared_environment(environment)
    assert environment == before


@pytest.mark.parametrize("change", [
    "wrong_id", "wrong_region", "failed", "no_consumption", "wrong_profile_type",
])
def test_environment_must_be_exact_and_ready(environment, change):
    if change == "wrong_id":
        environment["id"] = scope.ENVIRONMENT + "-other"
    elif change == "wrong_region":
        environment["location"] = "eastasia"
    elif change == "failed":
        environment["properties"]["provisioningState"] = "Failed"
    elif change == "wrong_profile_type":
        environment["properties"]["workloadProfiles"][0]["workloadProfileType"] = "D4"
    else:
        environment["properties"]["workloadProfiles"] = []
    with pytest.raises(ValueError):
        scope.require_shared_environment(environment)


def test_owned_resources_are_read_without_mutation(app, identity):
    originals = deepcopy((app, identity))
    scope.require_lab5_app(app)
    scope.require_runtime_identity(identity)
    assert (app, identity) == originals


@pytest.mark.parametrize("change", [
    "feedbackpulse", "missing_id", "wrong_environment", "conflicting_environment",
    "missing_environment", "multiple_revisions", "missing_tags", "wrong_lab",
    "wrong_student", "wrong_course", "identity_instead_of_app",
])
def test_app_guard_rejects_other_ownership_or_environment(app, change):
    if change == "feedbackpulse":
        app["id"] = scope.SCOPE + "/providers/Microsoft.App/containerApps/feedbackpulse-api"
    elif change == "identity_instead_of_app":
        app["id"] = scope.RUNTIME
    elif change == "missing_id":
        del app["id"]
    elif change == "wrong_environment":
        app["properties"]["environmentId"] = scope.ENVIRONMENT + "-other"
    elif change == "conflicting_environment":
        app["properties"]["managedEnvironmentId"] = scope.ENVIRONMENT + "-other"
    elif change == "missing_environment":
        del app["properties"]["environmentId"]
    elif change == "multiple_revisions":
        app["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
    elif change == "missing_tags":
        del app["tags"]
    else:
        key = {"wrong_lab": "lab", "wrong_student": "student", "wrong_course": "course"}[change]
        app["tags"][key] = "other"
    with pytest.raises(ValueError):
        scope.require_lab5_app(app)


def test_legacy_environment_field_is_checked(app):
    app["properties"]["managedEnvironmentId"] = app["properties"].pop("environmentId")
    scope.require_lab5_app(app)


@pytest.mark.parametrize("change", [
    "wrong_id", "wrong_region", "wrong_lab", "principal", "client", "missing_principal", "zero_client",
])
def test_runtime_identity_must_belong_to_lab5(identity, change):
    if change == "wrong_id":
        identity["id"] = scope.RUNTIME.replace("lab5-runtime", "lab4-runtime")
    elif change == "wrong_region":
        identity["location"] = "eastasia"
    elif change == "wrong_lab":
        identity["tags"]["lab"] = "4"
    elif change == "missing_principal":
        del identity["properties"]["principalId"]
    elif change == "zero_client":
        identity["properties"]["clientId"] = "00000000-0000-0000-0000-000000000000"
    else:
        identity["properties"]["principalId" if change == "principal" else "clientId"] = ""
    with pytest.raises(ValueError):
        scope.require_runtime_identity(identity)


@pytest.mark.parametrize("target", [scope.APP, scope.RUNTIME, scope.APP.upper()])
def test_only_exact_owned_targets_are_eligible(target):
    scope.require_owned_target(target)


@pytest.mark.parametrize("target", [
    scope.SCOPE,
    scope.ENVIRONMENT,
    scope.APP + "/revisions/example",
    scope.APP + "-other",
    scope.APP + "?redirect=another-target",
    scope.APP.replace(scope.SUBSCRIPTION, "11111111-1111-4111-8111-111111111111"),
    scope.APP.replace(scope.GROUP, "another-group"),
    scope.APP.replace("lab5-staging", "feedbackpulse-api"),
    scope.SCOPE + "/providers/Microsoft.ContainerRegistry/registries/itcs355u6688124",
    scope.SCOPE + "/providers/Microsoft.Storage/storageAccounts/itcs355u6688124",
    scope.SCOPE + "/providers/Microsoft.OperationalInsights/workspaces/shared-logs",
    scope.RUNTIME.replace("lab5-runtime", "lab4-runtime"),
    "", None,
])
def test_shared_or_unreviewed_targets_are_never_owned(target):
    with pytest.raises(ValueError, match="owned"):
        scope.require_owned_target(target)
