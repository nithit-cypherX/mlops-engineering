"""Lab 3 Azure registry, Container Apps and reused Blob/image-push operations."""
from __future__ import annotations

import json
import ipaddress
import os
import re
import subprocess
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

import requests

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from cloudlayer.base import CloudAdapter


class AzureAdapter(CloudAdapter):
    _APP_API = "2025-07-01"
    _DEPLOY_TIMEOUT = 600

    def _blob_location(self, uri: str) -> dict[str, str]:
        """Resolve a credential-free file URI strictly below this lab's Blob prefix."""
        root = re.fullmatch(
            r"https://(?P<host>[a-z0-9]{3,24}\.blob\.core\.windows\.net)/"
            r"(?P<container>[a-z0-9][a-z0-9-]{1,61}[a-z0-9])/"
            r"(?P<prefix>[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)/?",
            self.cfg.blob_uri,
        )
        if not root:
            raise ValueError("BLOB_URI must be a credential-free HTTPS Azure Blob folder")
        base = self.cfg.blob_uri.rstrip("/") + "/"
        if not uri.startswith(base):
            raise ValueError("Blob URI must stay under the configured BLOB_URI folder")
        key = uri[len(base):]
        # A small portable key syntax avoids traversal, encoded paths and SAS URLs.
        if any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part)
               for part in key.split("/")):
            raise ValueError("Blob key must contain non-empty relative file path segments")
        return {"account_url": f"https://{root['host']}",
                "container_name": root["container"], "blob_name": f"{root['prefix']}/{key}"}

    def upload(self, local_path: str, key: str) -> str:
        """Upload/replace one file. Callers own study keys and must use one writer per key."""
        uri = self.cfg.blob_uri.rstrip("/") + "/" + key
        location = self._blob_location(uri)
        with Path(local_path).open("rb") as source, DefaultAzureCredential() as credential:
            with BlobClient(**location, credential=credential) as client:
                client.upload_blob(source, overwrite=True)
        return uri

    def download(self, uri: str, local_path: str) -> None:
        """Replace the destination only after a complete download; preserve errors and old data."""
        location = self._blob_location(uri)
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="wb", dir=destination.parent,
                                    prefix=f".{destination.name}.", suffix=".tmp",
                                    delete=False) as output:
                temporary = Path(output.name)
                with DefaultAzureCredential() as credential:
                    with BlobClient(**location, credential=credential) as client:
                        client.download_blob().readinto(output)
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def push_image(self, local_tag: str) -> str:
        destination = self.cfg.container_registry.rstrip("/")
        host, separator, repository = destination.partition("/")
        if not re.fullmatch(r"[a-z0-9]+\.azurecr\.io", host) or not separator:
            raise ValueError("CONTAINER_REGISTRY must be an ACR hostname/repository")
        if not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repository):
            raise ValueError("Invalid container repository name")
        tag = local_tag.rsplit(":", 1)[-1]
        if local_tag.startswith("-") or ":" not in local_tag or not re.fullmatch(
            r"[\w][\w.-]{0,127}", tag, flags=re.ASCII,
        ):
            raise ValueError("local_tag must include an explicit valid image tag")
        remote_tag = f"{destination}:{tag}"
        registry = host.split(".", 1)[0]
        # Argument lists avoid shell interpolation; failures stop before the next step.
        subprocess.run(["az", "acr", "login", "--name", registry], check=True)
        subprocess.run(["docker", "tag", local_tag, remote_tag], check=True)
        subprocess.run(["docker", "push", remote_tag], check=True)
        result = subprocess.run(
            ["docker", "image", "inspect", remote_tag, "--format", "{{json .RepoDigests}}"],
            check=True, capture_output=True, text=True,
        )
        for reference in json.loads(result.stdout) or []:
            if re.fullmatch(re.escape(destination) + r"@sha256:[0-9a-f]{64}", reference):
                return reference
        raise RuntimeError("Push completed but Docker did not return the repository digest")

    def load_model(self, name: str, version: str):
        """Reuse Lab 2's MLflow registry-loading path; never fall back to a local file."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,254}", name):
            raise ValueError("MODEL_REGISTRY_NAME must be a model name without spaces or slashes")
        if not re.fullmatch(r"[1-9][0-9]*", version):
            raise ValueError("MODEL_VERSION must be a positive version number, not a stage or alias")
        if not self.cfg.mlflow_tracking_uri.startswith("azureml://"):
            raise ValueError("MLFLOW_TRACKING_URI must point to the Azure ML workspace registry")

        import mlflow.sklearn

        mlflow.set_tracking_uri(self.cfg.mlflow_tracking_uri)
        mlflow.set_registry_uri(self.cfg.mlflow_tracking_uri)
        return mlflow.sklearn.load_model(f"models:/{name}/{version}")

    @staticmethod
    def _az_json(arguments: list[str]):
        """Use the existing CLI login; no extension install, shell or credential export."""
        result = subprocess.run(
            ["az", *arguments, "--only-show-errors", "--output", "json"],
            check=True, capture_output=True, text=True, timeout=90,
            env={**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"},
        )
        return json.loads(result.stdout)

    def _rest(self, method: str, url: str, body: dict | None = None):
        args = ["rest", "--method", method, "--url", url]
        if body is not None:
            args += ["--headers", "Content-Type=application/json",
                     "--body", json.dumps(body, allow_nan=False)]
        return self._az_json(args)

    def _scope(self) -> str:
        UUID(self.cfg.azure_subscription_id)
        if not re.fullmatch(r"[A-Za-z0-9_()-][A-Za-z0-9_.()-]{0,88}[A-Za-z0-9_()-]",
                            self.cfg.project_id):
            raise ValueError("PROJECT_ID must be the resource-group name")
        return (f"/subscriptions/{self.cfg.azure_subscription_id}"
                f"/resourceGroups/{self.cfg.project_id}")

    def _app_url(self, endpoint: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}[a-z0-9]", endpoint) or "--" in endpoint:
            raise ValueError("endpoint must be a Container App name (2-32 characters)")
        return (f"https://management.azure.com{self._scope()}"
                f"/providers/Microsoft.App/containerApps/{endpoint}?api-version={self._APP_API}")

    def _require_lab3(self, resource: dict) -> None:
        tags = resource.get("tags") or {}
        if any(tags.get(key) != value for key, value in self.cfg.tags(3).items()):
            raise ValueError("Resource is not tagged for this student's Lab 3; refusing to use/update it")

    @staticmethod
    def _https_endpoint(endpoint: str) -> str:
        if not re.fullmatch(r"https://[a-z0-9][a-z0-9.-]*\.azurecontainerapps\.io/?", endpoint):
            raise ValueError("Expected a credential-free HTTPS Container Apps URL, without a path/query")
        return endpoint.rstrip("/")

    def deploy(self, model_ref: str, endpoint: str, instance: str) -> str:
        """Deploy one Lab 3 app; environment and tagged identity must already exist.

        model_ref is models:/name/version, endpoint is the app name, and instance is
        the approved Task 2 baseline. Image comes from SERVING_IMAGE, not model_ref.
        No builds, role assignments, environment creation or automatic deletion.
        """
        app_url = self._app_url(endpoint)
        if instance != "0.5cpu-1Gi":
            raise ValueError("Task 2 baseline requires instance=0.5cpu-1Gi")
        model = re.fullmatch(r"models:/([A-Za-z0-9][A-Za-z0-9_-]{0,254})/([1-9][0-9]*)", model_ref)
        if not model or model.groups() != (self.cfg.model_registry_name, self.cfg.model_version):
            raise ValueError("model_ref must match the configured numbered model version")
        registry = self.cfg.container_registry.rstrip("/")
        if not re.fullmatch(r"[a-z0-9]+\.azurecr\.io/[a-z0-9]+(?:[._/-][a-z0-9]+)*", registry):
            raise ValueError("CONTAINER_REGISTRY must be an ACR hostname/repository")
        if not re.fullmatch(re.escape(registry) + r"@sha256:[0-9a-f]{64}", self.cfg.serving_image):
            raise ValueError("SERVING_IMAGE must be digest-pinned in CONTAINER_REGISTRY")
        if not re.fullmatch(r"[a-z][a-z0-9]+", self.cfg.region):
            raise ValueError("REGION must be an Azure region name")
        tracking_path = (f"/mlflow/v1.0{self._scope()}"
                         f"/providers/Microsoft.MachineLearningServices/workspaces/{self.cfg.azure_ml_workspace}")
        if not self.cfg.azure_ml_workspace or not re.fullmatch(
            r"azureml://[a-z0-9.-]+\.api\.azureml\.ms" + re.escape(tracking_path),
            self.cfg.mlflow_tracking_uri, flags=re.IGNORECASE,
        ):
            raise ValueError("MLFLOW_TRACKING_URI must match the configured subscription/RG/workspace")
        network = ipaddress.ip_network(self.cfg.serving_allowed_ip, strict=True)
        if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
            raise ValueError("SERVING_ALLOWED_IP must be one public IPv4 address with /32")
        env_name = self.cfg.azure_containerapps_environment
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,58}[a-z0-9]", env_name):
            raise ValueError("AZURE_CONTAINERAPPS_ENVIRONMENT must be the prepared environment name")
        environment_id = f"{self._scope()}/providers/Microsoft.App/managedEnvironments/{env_name}"
        identity_id = self.cfg.azure_managed_identity_id
        if not re.fullmatch(
            re.escape(self._scope()) + r"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/[\w-]+",
            identity_id, flags=re.IGNORECASE,
        ):
            raise ValueError("AZURE_MANAGED_IDENTITY_ID must be a resource ID in this subscription/RG")
        UUID(self.cfg.identity_ref)

        # Only read pre-existing prerequisites. Failure never widens permissions.
        environment = self._rest("get", f"https://management.azure.com{environment_id}?api-version={self._APP_API}")
        self._require_lab3(environment)
        # ARM may return the display name ("Malaysia West") rather than the slug.
        if (environment.get("location", "").lower().replace(" ", "") != self.cfg.region
                or environment.get("properties", {}).get("provisioningState") != "Succeeded"):
            raise ValueError("The Lab 3 environment must be ready in the configured region")
        profiles = environment["properties"].get("workloadProfiles", [])
        if not any(p.get("name") == "Consumption" and p.get("workloadProfileType") == "Consumption"
                   for p in profiles):
            raise ValueError("The environment must provide the Consumption workload profile")
        identity = self._rest("get", f"https://management.azure.com{identity_id}?api-version=2023-01-31")
        self._require_lab3(identity)
        props = identity["properties"]
        if props.get("principalId", "").lower() != self.cfg.identity_ref.lower():
            raise ValueError("IDENTITY_REF does not match the managed identity principal ID")
        client_id = str(UUID(props["clientId"]))

        existing = self._az_json([
            "resource", "list", "--subscription", self.cfg.azure_subscription_id,
            "--resource-group", self.cfg.project_id, "--resource-type", "Microsoft.App/containerApps",
        ])
        tags = self.cfg.tags(3)
        if any(item["name"].lower() == endpoint.lower() for item in existing):
            current = self._rest("get", app_url)
            self._require_lab3(current)
            previous = current["properties"]
            previous_environment = previous.get("environmentId") or previous.get("managedEnvironmentId", "")
            if (previous_environment.lower() != environment_id.lower()
                    or previous.get("configuration", {}).get("activeRevisionsMode") != "Single"):
                raise ValueError("Refusing to overwrite another environment or a multi-revision deployment")
            tags = {**current["tags"], **tags}

        env = {
            "CLOUD_PROVIDER": "azure", "MLFLOW_TRACKING_URI": self.cfg.mlflow_tracking_uri,
            "MODEL_REGISTRY_NAME": model[1], "MODEL_VERSION": model[2], "AZURE_CLIENT_ID": client_id,
        }
        body = {
            "location": self.cfg.region, "tags": tags,
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity_id: {}}},
            "properties": {
                "environmentId": environment_id, "workloadProfileName": "Consumption",
                "configuration": {
                    "activeRevisionsMode": "Single",
                    "registries": [{"server": registry.split("/", 1)[0], "identity": identity_id}],
                    "ingress": {
                        "external": True, "targetPort": 8080, "transport": "auto", "allowInsecure": False,
                        "traffic": [{"latestRevision": True, "weight": 100}],
                        "ipSecurityRestrictions": [{"name": "lab3-client", "action": "Allow",
                                                    "ipAddressRange": str(network)}],
                    },
                },
                "template": {
                    "containers": [{
                        "name": "api", "image": self.cfg.serving_image,
                        "resources": {"cpu": 0.5, "memory": "1Gi"},
                        # One process means one loaded model and one readiness state.
                        "command": ["uvicorn"],
                        "args": ["service.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"],
                        "env": [{"name": key, "value": value} for key, value in env.items()],
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
        result = self._rest("put", app_url, body)
        deadline = time.monotonic() + self._DEPLOY_TIMEOUT
        while True:
            state = result.get("properties", {})
            if state.get("provisioningState") in {"Failed", "Canceled"}:
                raise RuntimeError(f"Deployment failed for {endpoint}; inspect its Azure status/logs before retrying")
            latest = state.get("latestRevisionName")
            if (state.get("provisioningState") == "Succeeded" and latest
                    and state.get("latestReadyRevisionName") == latest):
                return self._https_endpoint("https://" + state["configuration"]["ingress"]["fqdn"])
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Deployment of {endpoint} not ready in 600s; resource may still exist (no auto-delete)")
            time.sleep(5)
            result = self._rest("get", app_url)

    def invoke(self, endpoint: str, payload: dict) -> dict:
        """Accept an app name (resolve with CLI) or the HTTPS URL returned by deploy()."""
        if "://" not in endpoint:
            app = self._rest("get", self._app_url(endpoint))
            self._require_lab3(app)
            endpoint = "https://" + app["properties"]["configuration"]["ingress"]["fqdn"]
        url = self._https_endpoint(endpoint)
        # A cold start can take longer than a warm prediction. No hidden retries.
        response = requests.post(url + "/predict", json=payload, timeout=(10, 240), allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"Prediction failed with HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Prediction response must be a JSON object")
        return result
