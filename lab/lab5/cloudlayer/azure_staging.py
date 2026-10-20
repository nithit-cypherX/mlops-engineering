"""Lab 5 staging deployment, adapted from Lab 4's Container Apps deployment.

Uses the existing serving-image contract. No build, grants, environment/identity
creation, cleanup, or automatic write retries. The CLI is a cloud-write command.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import time
from pathlib import Path

import requests

from cloudlayer import lab5_scope as scope
from cloudlayer.azure_registry import MODEL_NAME, MODEL_ROOT, WORKSPACE
from cloudlayer.azure_config import ENV_FILE, StagingConfig, load_staging

APP_API = "2025-07-01"
ACR_SERVER = "itcs355u6688124.azurecr.io"
DEPLOY_TIMEOUT = 600


def read_registration(path: Path) -> dict:
    """Consume the exact version returned by registration, never latest or an alias."""
    result = json.loads(path.read_bytes())
    if not isinstance(result, dict):
        raise ValueError("Registration result must be an object")
    version = result.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*", version):
        raise ValueError("Registration must contain a numbered model version")
    expected = {
        "name": MODEL_NAME, "version": version,
        "id": f"{MODEL_ROOT}/versions/{version}",
        "model_uri": f"azureml:{MODEL_NAME}:{version}",
    }
    if (set(result) != set(expected)
            or any(not isinstance(result[key], str) for key in expected)
            or result["id"].casefold() != expected["id"].casefold()
            or any(result[key] != expected[key] for key in ("name", "version", "model_uri"))):
        raise ValueError("Registration result differs from the Lab 5 model scope")
    return result


def validate_settings(cfg: StagingConfig) -> str:
    scope.validate_config(cfg)
    if not re.fullmatch(
        re.escape(ACR_SERVER) + r"/[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}",
        cfg.serving_image,
    ):
        raise ValueError("SERVING_IMAGE must be digest-pinned in the reviewed ACR")
    tracking_path = (
        f"/mlflow/v1.0{scope.SCOPE}"
        f"/providers/Microsoft.MachineLearningServices/workspaces/{WORKSPACE}"
    )
    if not re.fullmatch(
        r"azureml://[a-z0-9.-]+\.api\.azureml\.ms" + re.escape(tracking_path),
        cfg.mlflow_tracking_uri, flags=re.IGNORECASE,
    ):
        raise ValueError("MLFLOW_TRACKING_URI must point to the reviewed workspace")
    try:
        address = ipaddress.ip_network(cfg.serving_allowed_ip, strict=True)
    except ValueError:
        raise ValueError("SERVING_ALLOWED_IP must be one public IPv4 address with /32") from None
    if address.version != 4 or address.prefixlen != 32 or not address.network_address.is_global:
        raise ValueError("SERVING_ALLOWED_IP must be one public IPv4 address with /32")
    return str(address)


def _request(credential, method, resource_id, *, api=APP_API, body=None, deadline=None):
    """ARM only; never follow a redirect with a bearer token or retry a PUT."""
    timeout = 90 if deadline is None else min(90, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("Staging readiness deadline reached; no automatic cleanup")
    token = credential.get_token("https://management.azure.com/.default").token
    response = requests.request(
        method, f"https://management.azure.com{resource_id}?api-version={api}",
        headers={"Authorization": f"Bearer {token}"}, json=body,
        timeout=timeout, allow_redirects=False,
    )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        raise RuntimeError(f"ARM {method} returned invalid JSON") from None
    if (method == "GET" and resource_id == scope.APP and response.status_code == 404
            and isinstance(payload, dict)
            and isinstance(payload.get("error"), dict)
            and payload["error"].get("code") == "ResourceNotFound"):
        return None
    allowed = (200, 201, 202) if method == "PUT" else (200,)
    if response.status_code not in allowed:
        raise RuntimeError(f"ARM {method} failed with HTTP {response.status_code}; inspect before retrying")
    if not isinstance(payload, dict):
        raise ValueError("ARM response must be an object")
    return payload


def deploy_staging(registration_path: Path, cfg: StagingConfig, *, credential) -> dict:
    """Return control-plane evidence, not an HTTP prediction/smoke-test claim."""
    registered = read_registration(registration_path)
    allowed_ip = validate_settings(cfg)

    environment = _request(credential, "GET", scope.ENVIRONMENT)
    scope.require_shared_environment(environment)
    identity = _request(credential, "GET", scope.RUNTIME, api="2023-01-31")
    scope.require_runtime_identity(identity)
    current = _request(credential, "GET", scope.APP)
    tags = dict(scope.TAGS)
    if current is not None:
        scope.require_lab5_app(current)
        if current.get("properties", {}).get("provisioningState") not in {"Succeeded", "Failed", "Canceled"}:
            raise ValueError("Existing staging app has an unfinished operation; inspect it before retrying")
        tags.update(current["tags"])

    model_env = {
        "CLOUD_PROVIDER": "azure", "MLFLOW_TRACKING_URI": cfg.mlflow_tracking_uri,
        "MODEL_REGISTRY_NAME": registered["name"], "MODEL_VERSION": registered["version"],
        "AZURE_CLIENT_ID": identity["properties"]["clientId"],
    }
    ip_rules = [{"name": "lab5-client", "action": "Allow", "ipAddressRange": allowed_ip}]
    body = {
        "location": scope.REGION, "tags": tags,
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {scope.RUNTIME: {}}},
        "properties": {
            "environmentId": scope.ENVIRONMENT, "workloadProfileName": "Consumption",
            "configuration": {
                "activeRevisionsMode": "Single",
                "registries": [{"server": ACR_SERVER, "identity": scope.RUNTIME}],
                "ingress": {
                    "external": True, "targetPort": 8080, "transport": "auto", "allowInsecure": False,
                    "traffic": [{"latestRevision": True, "weight": 100}],
                    "ipSecurityRestrictions": ip_rules,
                },
            },
            "template": {
                "containers": [{
                    "name": "api", "image": cfg.serving_image,
                    "resources": {"cpu": 0.5, "memory": "1Gi"},
                    "command": ["uvicorn"],
                    "args": ["service.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"],
                    "env": [{"name": key, "value": value} for key, value in model_env.items()],
                    "probes": [
                        {"type": "Startup", "httpGet": {"path": "/ready", "port": 8080},
                         "initialDelaySeconds": 1, "periodSeconds": 30, "timeoutSeconds": 5,
                         "failureThreshold": 10},
                        {"type": "Liveness", "httpGet": {"path": "/health", "port": 8080},
                         "periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 3},
                        {"type": "Readiness", "httpGet": {"path": "/ready", "port": 8080},
                         "periodSeconds": 5, "timeoutSeconds": 5, "failureThreshold": 3},
                    ],
                }],
                "scale": {"minReplicas": 0, "maxReplicas": 1,
                          "rules": [{"name": "http", "http": {"metadata": {"concurrentRequests": "10"}}}]},
            },
        },
    }
    # Exactly one write to the exact Lab 5 app. Shared prerequisites are read-only.
    try:
        _request(credential, "PUT", scope.APP, body=body)
    except requests.RequestException as exc:
        raise RuntimeError(
            "Staging write was not confirmed; the app may exist. Inspect before retrying."
        ) from exc
    deadline = time.monotonic() + DEPLOY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            app = _request(credential, "GET", scope.APP, deadline=deadline)
            if app is None:
                raise RuntimeError("Staging app disappeared after deployment; inspect before retrying")
            scope.require_lab5_app(app)
            state = app["properties"]
            if state.get("provisioningState") in {"Failed", "Canceled"}:
                raise RuntimeError("Staging deployment failed; inspect Azure status/logs before retrying")
            latest = state.get("latestRevisionName")
            if state.get("provisioningState") == "Succeeded" and latest:
                if not isinstance(latest, str) or not re.fullmatch(r"lab5-staging--[a-z0-9][a-z0-9-]*", latest):
                    raise ValueError("Unexpected staging revision name")
                revision = _request(
                    credential, "GET", f"{scope.APP}/revisions/{latest}", deadline=deadline,
                )
                props = revision.get("properties", {})
                if (props.get("provisioningState") == "Failed"
                        or props.get("runningState") in {"Failed", "ActivationFailed"}):
                    raise RuntimeError("Staging revision failed; inspect Azure status/logs before retrying")
                containers = props.get("template", {}).get("containers", [])
                api = containers[0] if len(containers) == 1 else {}
                actual_env = {entry["name"]: entry.get("value") for entry in api.get("env", [])}
                healthy = (props.get("healthState") == "Healthy"
                           and props.get("runningState") in {"Running", "Processing", "RunningAtMaxScale"})
                idle = (props.get("runningState") == "ScaledToZero" and props.get("replicas") == 0
                        and props.get("healthState") in {None, "None", "Healthy"})
                ingress = state.get("configuration", {}).get("ingress", {})
                actual_rules = [
                    {key: rule.get(key) for key in ("name", "action", "ipAddressRange")}
                    for rule in ingress.get("ipSecurityRestrictions", [])
                ]
                assigned = (app.get("identity") or {}).get("userAssignedIdentities") or {}
                registries = state.get("configuration", {}).get("registries", [])
                identity_ready = any(key.casefold() == scope.RUNTIME.casefold() for key in assigned)
                registry_ready = any(
                    item.get("server") == ACR_SERVER
                    and str(item.get("identity", "")).casefold() == scope.RUNTIME.casefold()
                    for item in registries
                )
                if (revision.get("name") == latest and props.get("active") is True
                        and props.get("provisioningState") == "Provisioned"
                        and props.get("trafficWeight") == 100 and (healthy or idle)
                        and api.get("name") == "api" and api.get("image") == cfg.serving_image
                        and all(actual_env.get(key) == value for key, value in model_env.items())
                        and actual_rules == ip_rules and ingress.get("allowInsecure") is False
                        and ingress.get("external") is True and identity_ready and registry_ready
                        and ingress.get("targetPort") == 8080 and time.monotonic() < deadline):
                    fqdn = ingress.get("fqdn", "")
                    if not isinstance(fqdn, str) or not re.fullmatch(
                        r"lab5-staging\.[a-z0-9.-]+\.azurecontainerapps\.io", fqdn,
                    ):
                        raise ValueError("Unexpected staging hostname")
                    return {
                        "status": "control_plane_verified", "app_id": scope.APP,
                        "endpoint": "https://" + fqdn, "revision": latest,
                        "serving_image": cfg.serving_image, "model": registered,
                        "revision_state": "healthy" if healthy else "scaled_to_zero",
                        "http_smoke_tested": False,
                    }
        except requests.Timeout:
            # Readiness GET only: never repeat the PUT or an authorization failure.
            pass
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise TimeoutError(
        f"Staging not ready within {DEPLOY_TIMEOUT}s; resource may exist (no automatic delete or redeploy)"
    )


def main(argv: list[str] | None = None, *, credential=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        cfg = load_staging(args.env_file)
        read_registration(args.registration)
        validate_settings(cfg)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if any(args.output_dir.iterdir()):
            raise FileExistsError("Deployment output directory must be empty; use a new run directory")
        if credential is None:
            from azure.identity import DefaultAzureCredential
            credential = DefaultAzureCredential()
        result = deploy_staging(args.registration, cfg, credential=credential)
        try:
            with (args.output_dir / "deployment.json").open("x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2, allow_nan=False)
                stream.write("\n")
        except OSError as exc:
            raise RuntimeError("Deployment was verified but output was not saved; inspect before retrying") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"DEPLOY FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
