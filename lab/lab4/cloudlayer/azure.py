"""Lab 4 baseline: reuse Lab 3 Blob, image-push and registry-loading operations.

Source: lab/lab3/cloudlayer/azure.py at 72e081d1e723d3b855b7df57937519234badaaa3.
Deployment, monitoring and cleanup are not implemented for Lab 4 yet.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from cloudlayer.base import CloudAdapter


class AzureAdapter(CloudAdapter):
    def integration_environment(self) -> dict[str, str]:
        """Prepare short-lived host-login auth for a bounded local container test.

        No run is created or changed. The token retains the user's permissions;
        callers must keep it in memory and send it through stdin, never Docker -e.
        """
        __tracebackhide__ = True  # pytest --showlocals must not expose the token.
        # ponytail: tested azureml-mlflow 1.62.0.post6 environment-token route;
        # ceiling: local tests <5 min, no refresh; revisit when CI auth is added
        # or the SDK changes; upgrade: supported workload identity for that runtime.
        if not self.cfg.mlflow_tracking_uri.startswith("azureml://"):
            raise ValueError("Integration auth requires the configured workspace registry")

        from mlflow.tracking import MlflowClient

        try:
            client = MlflowClient(tracking_uri=self.cfg.mlflow_tracking_uri,
                                  registry_uri=self.cfg.mlflow_tracking_uri)
            version = client.get_model_version(self.cfg.model_registry_name,
                                               self.cfg.model_version)
            if version.status != "READY" or not version.run_id:
                raise ValueError("Model must be READY and linked to its training run")
            run = client.get_run(version.run_id)
            command = ["az", "account", "get-access-token", "--resource",
                       "https://management.azure.com/", "--output", "json",
                       "--only-show-errors"]
            if self.cfg.azure_subscription_id:
                command += ["--subscription", self.cfg.azure_subscription_id]
            result = subprocess.run(command, check=True, capture_output=True,
                                    text=True, timeout=30)
            auth = json.loads(result.stdout)
            token = auth["accessToken"]
            expires = float(auth["expires_on"])
            if not isinstance(token, str) or not token or not expires >= time.time() + 600:
                raise ValueError("Need a token with at least ten minutes remaining")
            if not run.info.experiment_id:
                raise ValueError("The existing run must have an experiment ID")
        except Exception:
            # SDK/CLI errors can contain request details: never echo their payload.
            raise RuntimeError(
                "Cannot prepare temporary integration auth. Check host cloud login, "
                "registry access and token lifetime (at least ten minutes)."
            ) from None
        return {
            "CLOUD_PROVIDER": self.cfg.provider,
            "MLFLOW_TRACKING_URI": self.cfg.mlflow_tracking_uri,
            "MODEL_REGISTRY_NAME": self.cfg.model_registry_name,
            "MODEL_VERSION": self.cfg.model_version,
            "MLFLOW_TRACKING_TOKEN": token,
            "MLFLOW_RUN_ID": version.run_id,
            "MLFLOW_EXPERIMENT_ID": str(run.info.experiment_id),
        }

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
