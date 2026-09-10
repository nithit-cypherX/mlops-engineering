"""Azure adapter. Implement upload/download/push_image for Lab 1.

SDK:  pip install azure-storage-blob azure-identity azure-containerregistry
Docs: BlobServiceClient for storage; ACR push goes through `docker push` after
      `az acr login --name <registry>`.

Hints for Lab 1:
  * BLOB_URI is either abfss://container@account.dfs.core.windows.net/prefix or
    https://account.blob.core.windows.net/container/prefix. Pick one form and parse
    it here, never in src/.
  * Use DefaultAzureCredential rather than a connection string. It picks up your CLI
    login locally and your managed identity in CI, which is what Lab 4 needs.
  * push_image must return the digest reference: registry.azurecr.io/repo@sha256:...
  * Azure tags live on the resource, not the blob. Tag the storage account, the
    registry, and later the workspace with cfg.tags(1).
"""
from __future__ import annotations

import json
import re
import subprocess

from cloudlayer.base import CloudAdapter


class AzureAdapter(CloudAdapter):
    def upload(self, local_path: str, key: str) -> str:
        raise NotImplementedError("TODO Lab 1: upload_blob into BLOB_URI, return the full URI")

    def download(self, uri: str, local_path: str) -> None:
        raise NotImplementedError("TODO Lab 1: download_blob, creating parent directories")

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

    # submit_training / register_model  -> Lab 2 (Azure ML command job + model registry)
    # deploy / invoke                   -> Lab 3 (managed online endpoint + deployment)
    # emit_metric                       -> Lab 4 (Azure Monitor custom metric)
    # generate                          -> Lab 5 (managed LLM endpoint; read the usage block for tokens)
    # teardown                          -> Lab 5 (resource graph query by tag)
