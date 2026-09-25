"""Lab 3 Azure registry, Container Apps and reused Blob/image-push operations."""
from __future__ import annotations

import json
import copy
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

    def _canary_request(self, method, path="", body=None, deadline=None):
        """Scoped REST for the drill; PATCH/POST can return an empty body."""
        root, query = self._app_url(self.cfg.endpoint_name).split("?", 1)
        timeout = 30 if deadline is None else min(30, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Canary operation reached its deadline")
        args = ["az", "rest", "--method", method, "--url", root + path + "?" + query]
        if body is not None:
            args += ["--headers", "Content-Type=application/json", "--body", json.dumps(body)]
        result = subprocess.run(
            [*args, "--only-show-errors", "--output", "json"], check=True,
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"},
        )
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def canary_state(self, deadline=None):
        app = self._canary_request("get", deadline=deadline)
        self._require_lab3(app)
        expected = (f"{self._scope()}/providers/Microsoft.App/managedEnvironments/"
                    f"{self.cfg.azure_containerapps_environment}")
        if app["properties"].get("environmentId", "").lower() != expected.lower():
            raise ValueError("Unexpected serving environment")
        result = self._canary_request("get", "/revisions", deadline=deadline)
        if result.get("nextLink"):
            raise ValueError("Unexpected revision pagination; inspect before proceeding")
        return app, result["value"]

    def _canary_template(self, template, version):
        containers = template.get("containers", [])
        if len(containers) != 1 or template.get("initContainers"):
            raise ValueError("Expected only the existing API container")
        container = containers[0]
        env = {item["name"]: item.get("value") for item in container.get("env", [])}
        expected = {"CLOUD_PROVIDER": "azure", "MLFLOW_TRACKING_URI": self.cfg.mlflow_tracking_uri,
                    "MODEL_REGISTRY_NAME": self.cfg.model_registry_name, "MODEL_VERSION": version}
        if (any(env.get(k) != v for k, v in expected.items())
                or set(env) != set(expected) | {"AZURE_CLIENT_ID"}
                or len(env) != len(container.get("env", []))
                or any("secretRef" in item for item in container.get("env", []))):
            raise ValueError("Unexpected model configuration or secret environment variable")
        UUID(env["AZURE_CLIENT_ID"])
        if (container.get("image") != self.cfg.serving_image
                or not re.fullmatch(re.escape(self.cfg.container_registry.rstrip("/"))
                                    + r"@sha256:[0-9a-f]{64}", self.cfg.serving_image)
                or container.get("resources", {}).get("cpu") != 0.5
                or container.get("resources", {}).get("memory") != "1Gi"
                or container.get("command") != ["uvicorn"]
                or container.get("args") != ["service.app:app", "--host", "0.0.0.0",
                                             "--port", "8080", "--workers", "1"]
                or template.get("scale", {}).get("minReplicas") != 0
                or template.get("scale", {}).get("maxReplicas") != 1):
            raise ValueError("Expected pinned image, one worker and the 0.5 CPU / 1 GiB baseline")

    def canary_plan(self, baseline, candidate_version, suffix):
        """Read-only preflight. No registration, download, build or app start."""
        if (not re.fullmatch(re.escape(self.cfg.endpoint_name) + r"--[a-z0-9-]+", baseline)
                or not re.fullmatch(r"cny-[a-z0-9]{6,20}", suffix)
                or not re.fullmatch(r"[1-9][0-9]*", candidate_version)
                or candidate_version == self.cfg.model_version):
            raise ValueError("Use an explicit baseline, fresh cny- suffix and different numbered version")
        app, revisions = self.canary_state()
        props = app["properties"]
        configuration = props["configuration"]
        ingress = configuration["ingress"]
        active = [r["name"] for r in revisions if r["properties"].get("active")]
        if (props.get("runningStatus") != "Stopped" or props.get("provisioningState") != "Succeeded"
                or configuration.get("activeRevisionsMode") != "Single"
                or props.get("latestRevisionName") != baseline or active != [baseline]
                or any(r["properties"].get("replicas") != 0 for r in revisions)):
            raise ValueError("Expected stopped Single-mode app, one active baseline and zero replicas")
        if ingress.get("traffic") not in ([{"latestRevision": True, "weight": 100}],
                                          [{"revisionName": baseline, "weight": 100}]):
            raise ValueError("Unexpected initial traffic configuration")
        rules = ingress.get("ipSecurityRestrictions", [])
        if (len(rules) != 1 or rules[0].get("action") != "Allow"
                or rules[0].get("ipAddressRange") != self.cfg.serving_allowed_ip
                or ingress.get("allowInsecure") or ingress.get("stickySessions")
                or configuration.get("secrets")):
            raise ValueError("Expected existing restricted ingress without secrets or sticky sessions")
        candidate = self.cfg.endpoint_name + "--" + suffix
        if any(r["name"] == candidate for r in revisions):
            raise ValueError("Candidate revision already exists; do not reuse a drill")
        original = next(r for r in revisions if r["name"] == baseline)["properties"]["template"]
        self._canary_template(original, self.cfg.model_version)
        template = copy.deepcopy(original)
        template["revisionSuffix"] = suffix
        for item in template["containers"][0]["env"]:
            if item["name"] == "MODEL_VERSION":
                item["value"] = candidate_version
        from mlflow.tracking import MlflowClient
        client = MlflowClient(tracking_uri=self.cfg.mlflow_tracking_uri,
                              registry_uri=self.cfg.mlflow_tracking_uri)
        models = {}
        for version in (self.cfg.model_version, candidate_version):
            model = client.get_model_version(self.cfg.model_registry_name, version)
            if model.status != "READY":
                raise ValueError("Both registered versions must be READY")
            models[version] = {"run_id": model.run_id, "status": model.status}
        return {"baseline": baseline, "candidate": candidate, "candidate_version": candidate_version,
                "baseline_version": self.cfg.model_version, "template": template,
                "location": app["location"], "models": models,
                "endpoint": self._https_endpoint("https://" + ingress["fqdn"])}

    def _canary_guard(self, plan, revisions):
        if (plan["baseline_version"] != self.cfg.model_version
                or not re.fullmatch(re.escape(self.cfg.endpoint_name) + r"--[a-z0-9-]+", plan["baseline"])
                or not re.fullmatch(re.escape(self.cfg.endpoint_name) + r"--cny-[a-z0-9]{6,20}", plan["candidate"])):
            raise ValueError("Plan does not belong to this app")
        by_name = {r["name"]: r["properties"] for r in revisions}
        if plan["baseline"] not in by_name or not by_name[plan["baseline"]].get("active"):
            raise ValueError("Baseline is missing or inactive")
        for key in ("baseline", "candidate"):
            if plan[key] in by_name:
                self._canary_template(by_name[plan[key]]["template"], plan[key + "_version"])
        if any(r.get("active") and name not in (plan["baseline"], plan["candidate"])
               for name, r in by_name.items()):
            raise ValueError("An unrelated revision is active")
        return by_name

    def canary_route(self, plan, weight, deadline=None):
        """Only 90/10 or an explicit baseline-only rollback, never latestRevision."""
        if weight not in (0, 10):
            raise ValueError("Only candidate weights 0 and 10 are allowed")
        deadline = deadline if deadline is not None else time.monotonic() + 60
        app, revisions = self.canary_state(deadline)
        by_name = self._canary_guard(plan, revisions)
        if weight and not by_name.get(plan["candidate"], {}).get("active"):
            raise ValueError("Candidate revision is not active")
        traffic = [{"revisionName": plan["baseline"], "weight": 100 - weight}]
        if weight:
            traffic.append({"revisionName": plan["candidate"], "weight": weight})
        self._canary_request("patch", body={"location": app["location"], "properties": {
            "configuration": {"activeRevisionsMode": "Multiple", "ingress": {"traffic": traffic}}}},
            deadline=deadline)
        while time.monotonic() < deadline:
            current, _ = self.canary_state(deadline)
            config = current["properties"]["configuration"]
            actual = config["ingress"].get("traffic", [])
            weights = {r.get("revisionName"): r["weight"] for r in actual if r["weight"]}
            if (current["properties"].get("provisioningState") == "Succeeded"
                    and config.get("activeRevisionsMode") == "Multiple"
                    and not any(r.get("latestRevision") for r in actual)
                    and weights == {r["revisionName"]: r["weight"] for r in traffic}):
                return traffic
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise TimeoutError("Traffic change was not confirmed")

    def canary_prepare(self, plan):
        # Validate the outgoing revision before even changing traffic.
        self._canary_template(plan["template"], plan["candidate_version"])
        if self.cfg.endpoint_name + "--" + plan["template"].get("revisionSuffix", "") != plan["candidate"]:
            raise ValueError("Candidate template suffix does not match the preflight plan")
        deadline = time.monotonic() + 300
        self.canary_route(plan, 0, deadline)
        self._canary_request("patch", body={"location": plan["location"],
                             "properties": {"template": plan["template"]}}, deadline=deadline)
        while time.monotonic() < deadline:
            app, revisions = self.canary_state(deadline)
            by_name = self._canary_guard(plan, revisions)
            if app["properties"].get("provisioningState") in ("Failed", "Canceled"):
                raise RuntimeError("Candidate deployment failed")
            if (plan["candidate"] in by_name
                    and app["properties"].get("provisioningState") == "Succeeded"):
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        else:
            raise TimeoutError("Candidate revision was not created")
        self._canary_request("post", "/start", deadline=deadline)
        ready = set()
        while time.monotonic() < deadline:
            _, revisions = self.canary_state(deadline)
            by_name = self._canary_guard(plan, revisions)
            for key in ("baseline", "candidate"):
                if key in ready:
                    continue
                fqdn = by_name.get(plan[key], {}).get("fqdn")
                if not fqdn:
                    continue
                endpoint = self._https_endpoint("https://" + fqdn)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    response = requests.get(endpoint + "/ready", timeout=min(5, remaining),
                                            allow_redirects=False, headers={"Connection": "close"})
                except (requests.Timeout, requests.ConnectionError):
                    continue  # Readiness polling only, not a rerun of the experiment.
                if response.status_code == 200:
                    if response.json().get("model_version") != plan[key + "_version"]:
                        raise RuntimeError("Ready endpoint returned the wrong model")
                    ready.add(key)
                elif response.status_code not in (502, 503, 504):
                    raise RuntimeError("Readiness failed; check ingress/access before retrying")
            if len(ready) == 2 and time.monotonic() < deadline:
                return
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise TimeoutError("Both revisions did not become ready within five minutes")

    def invoke_batch(self, endpoint, rows, timeout=10):
        endpoint = self._https_endpoint(endpoint)
        response = requests.post(endpoint + "/predict/batch", json={"rows": rows},
                                 timeout=timeout, allow_redirects=False,
                                 headers={"Connection": "close"})
        if response.status_code != 200:
            raise RuntimeError(f"Batch returned HTTP {response.status_code}")
        return response.json(), response.headers.get("x-request-id")

    def canary_cleanup(self, plan):
        """Attempt every cleanup stage; a failed check is never a pass."""
        errors = []
        try:
            self.canary_route(plan, 0)
        except Exception as exc:
            errors.append("restore_traffic:" + type(exc).__name__)
        try:
            _, revisions = self.canary_state()
            by_name = self._canary_guard(plan, revisions)
            if plan["candidate"] in by_name and by_name[plan["candidate"]].get("active"):
                self._canary_request("post", f"/revisions/{plan['candidate']}/deactivate")
        except Exception as exc:
            errors.append("deactivate_candidate:" + type(exc).__name__)
        stopped = False
        candidate_inactive = False
        try:
            self.canary_state()  # Recheck ownership before stopping this app.
            self._canary_request("post", "/stop")
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                app, revisions = self.canary_state(deadline)
                stopped = (app["properties"].get("runningStatus") == "Stopped"
                           and all(r["properties"].get("replicas") == 0 for r in revisions))
                candidate_inactive = all(not r["properties"].get("active") for r in revisions
                                         if r["name"] == plan["candidate"])
                if stopped and candidate_inactive:
                    break
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        except Exception as exc:
            errors.append("stop_or_check:" + type(exc).__name__)
        return {"stopped_zero_replicas": stopped, "candidate_inactive": candidate_inactive,
                "errors": errors, "ok": stopped and candidate_inactive and not errors}

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
