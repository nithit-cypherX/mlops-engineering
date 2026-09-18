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
import math
import re
import subprocess
import time
from functools import cached_property
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from azure.ai.ml import Input, MLClient, Output, command
from azure.ai.ml.entities import (
    AmlCompute, AzureBlobDatastore, Environment, ManagedIdentityConfiguration,
)
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from cloudlayer.base import CloudAdapter
from src import costs


class AzureAdapter(CloudAdapter):
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

    @cached_property
    def _ml_client(self):
        # Submitting uses the caller's login; the job uses the compute identity below.
        return MLClient(
            DefaultAzureCredential(), self.cfg.azure_subscription_id,
            self.cfg.project_id, self.cfg.azure_ml_workspace,
        )

    def submit_training(self, image_uri: str, args: dict[str, Any]) -> str:
        """Submit one train job, or one study job when args includes mode='tune'."""
        if not re.fullmatch(
            re.escape(self.cfg.container_registry.rstrip("/")) + r"@sha256:[0-9a-f]{64}",
            image_uri,
        ):
            raise ValueError("image_uri must be digest-pinned in CONTAINER_REGISTRY")
        tuning = args.get("mode") == "tune"
        required = ({"mode", "trials", "budget_thb", "timeout_s", "trial_estimate_s", "seed"}
                    if tuning else {"n_estimators", "max_depth", "min_samples_leaf", "seed"})
        optional = {"resume_from", "test_interruption"} if tuning else set()
        if not required <= set(args) or set(args) - required - optional:
            raise ValueError("args must contain exactly: " + ", ".join(sorted(required)))
        for key, value in args.items():
            if tuning and key == "mode":
                continue
            if tuning and key == "test_interruption":
                if type(value) is not bool:
                    raise ValueError("test_interruption must be a boolean")
                continue
            if tuning and key == "resume_from":
                if not isinstance(value, str) or not re.fullmatch(r"lab2-[0-9a-f]{32}", value):
                    raise ValueError("resume_from must be a Lab 2 managed job ID")
                continue
            if tuning and key in {"budget_thb", "trial_estimate_s"}:
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"Invalid study argument: {key}")
                continue
            if type(value) is not int or not (0 <= value < 2**32 if key == "seed" else value > 0):
                raise ValueError(f"Invalid training argument: {key}")
        if tuning:
            if args["trials"] > 12 or args["budget_thb"] > 150:
                raise ValueError("Study requires 1..12 trials and an allocation no larger than 150 THB")
            if args["timeout_s"] < args["trial_estimate_s"]:
                raise ValueError("timeout_s must cover at least one trial_estimate_s")
            if args.get("test_interruption") and (
                "resume_from" in args or args["trials"] <= 4
            ):
                raise ValueError("test_interruption requires a new study with more than four trials; "
                                 "omit it when resuming")

        blob = urlsplit(self.cfg.blob_uri)
        if (blob.scheme != "https" or blob.query or blob.fragment
                or not re.fullmatch(r"[a-z0-9]+\.blob\.core\.windows\.net", blob.netloc)):
            raise ValueError("BLOB_URI must be an HTTPS Azure Blob path without credentials")
        container, _, prefix = blob.path.lstrip("/").rstrip("/").partition("/")
        if not prefix or any(not re.fullmatch(r"[\w-]+", part) for part in prefix.split("/")):
            raise ValueError("BLOB_URI must include a folder prefix")
        datastore = self._ml_client.datastores.get_default()
        if (not isinstance(datastore, AzureBlobDatastore)
                or blob.netloc != f"{datastore.account_name}.blob.core.windows.net"
                or container != datastore.container_name):
            raise ValueError("BLOB_URI must use the workspace's default Blob datastore")
        root = f"azureml://datastores/{datastore.name}/paths/{prefix}"
        name = "lab2-" + uuid4().hex
        environment_variables = {
            "PYTHONHASHSEED": str(args["seed"]),
            "MLFLOW_TRACKING_URI": self.cfg.mlflow_tracking_uri,
        }
        timeout = 1800
        tags = self.cfg.tags(2)
        if tuning:
            compute = self._ml_client.compute.get(self.cfg.azure_ml_compute)
            # Only this Dedicated SKU/region has a verified rate in src/costs.py.
            # Fail closed if the configured target changes; do not use starter prices.
            if (not isinstance(compute, AmlCompute)
                    or str(compute.size).lower() != "standard_f2s_v2"
                    or str(compute.tier).lower() != "dedicated"
                    or str(compute.location).lower() != "malaysiawest"):
                raise ValueError("Study compute must match the verified Dedicated Standard_F2s_v2 "
                                 "rate in malaysiawest; review pricing before changing the target")
            instance = "Standard_F2s_v2"  # Canonical price key for the checked SDK size.
            rate = costs.hourly_rate("azure", instance, spot=False)
            timeout = args["timeout_s"]
            if timeout / 3600 * rate > args["budget_thb"]:
                raise ValueError("Estimated compute cost at timeout exceeds budget_thb; "
                                 "startup, idle and other services still cost extra")
            environment_variables["CLOUD_PROVIDER"] = "azure"
            environment_variables["BLOB_URI"] = self.cfg.blob_uri
            checkpoint_key = f"studies/{name}/checkpoint.json"
            checkpoint_uri = self.cfg.blob_uri.rstrip("/") + "/" + checkpoint_key
            self._blob_location(checkpoint_uri)
            tags.update(checkpoint_uri=checkpoint_uri, study_image=image_uri)
            resume_option = ""
            if "resume_from" in args:
                previous = self._ml_client.jobs.get(args["resume_from"])
                if previous.status not in {"Completed", "Failed", "Canceled"}:
                    raise ValueError("Resume source job must have stopped before submitting a successor")
                previous_uri = (self.cfg.blob_uri.rstrip("/")
                                + f"/studies/{args['resume_from']}/checkpoint.json")
                expected_tags = {**self.cfg.tags(2), "checkpoint_uri": previous_uri,
                                 "study_image": image_uri}
                if (previous.name != args["resume_from"] or previous_uri == checkpoint_uri
                        or any((previous.tags or {}).get(k) != v for k, v in expected_tags.items())):
                    raise ValueError("Resume source must be this lab's checkpointed study with the same image")
                # Each successor writes its own Blob; the stopped source stays read-only.
                resume_option = f" --resume-uri '{previous_uri}'"
                tags["resume_from"] = previous.name
            options = " ".join(f"--{key.replace('_', '-')} {args[key]}" for key in (
                "trials", "budget_thb", "trial_estimate_s", "seed",
            ))
            if args.get("test_interruption"):
                options += " --test-interruption"
            job_command = (
                "python -m src.tune --raw-path '${{inputs.raw}}' "
                "--dvc-metadata-path '${{inputs.dvc}}' "
                "--checkpoint '${{outputs.artifacts}}/tune_checkpoint.json' "
                f"--checkpoint-key {checkpoint_key} --image-uri {image_uri} "
                f"--experiment itcs355-lab2 --instance {instance} {options}{resume_option}"
            )
            # The tuner uploads checkpoints during the run, separately from job outputs.
        else:
            options = " ".join(f"--{key.replace('_', '-')} {value}" for key, value in args.items())
            job_command = (
                "python -m src.train --raw-path '${{inputs.raw}}' "
                "--dvc-metadata-path '${{inputs.dvc}}' --output-dir '${{outputs.artifacts}}' "
                f"--experiment itcs355-lab2 --run-name {name} {options}"
            )
        job = command(
            name=name, experiment_name="itcs355-lab2", tags=tags,
            command=job_command,
            environment=Environment(image=image_uri), compute=self.cfg.azure_ml_compute,
            instance_count=1, timeout=timeout,
            identity=ManagedIdentityConfiguration(object_id=self.cfg.identity_ref),
            environment_variables=environment_variables,
            inputs={
                "raw": Input(type="uri_file", path=f"{root}/data/sensors.csv", mode="download"),
                "dvc": Input(type="uri_file", path=f"{root}/data/raw.dvc", mode="download"),
            },
            outputs={"artifacts": Output(
                type="uri_folder", path=f"{root}/runs/{name}", mode="upload",
            )},
        )
        return self._ml_client.jobs.create_or_update(job).name

    def wait_training(self, job_id: str) -> dict[str, Any]:
        """Return only on Completed; job failure and cancellation are errors.

        Wait at least one hour, or the job's run limit plus 30 minutes for queue
        and finalization. This client-side deadline does not cancel the job.
        """
        started = time.monotonic()
        wait_seconds = 3600
        while True:
            job = self._ml_client.jobs.get(job_id)
            timeout = getattr(getattr(job, "limits", None), "timeout", None)
            if type(timeout) is int and timeout > 0:
                wait_seconds = max(wait_seconds, timeout + 1800)
            if job.status == "Completed":
                return {"job_id": job.name, "status": job.status,
                        "outputs": {key: value.path for key, value in (job.outputs or {}).items()}}
            if job.status in {"Failed", "Canceled"}:
                raise RuntimeError(f"Azure ML job {job_id}: {job.status}. Check its job logs.")
            if time.monotonic() >= started + wait_seconds:
                raise TimeoutError(
                    f"Stopped waiting for {job_id} (status: {job.status}). "
                    "The job may still be running; check Azure ML before submitting again."
                )
            time.sleep(10)

    # register_model                    -> Lab 2 (model registry)
    # deploy / invoke                   -> Lab 3 (managed online endpoint + deployment)
    # emit_metric                       -> Lab 4 (Azure Monitor custom metric)
    # generate                          -> Lab 5 (managed LLM endpoint; read the usage block for tokens)
    # teardown                          -> Lab 5 (resource graph query by tag)
