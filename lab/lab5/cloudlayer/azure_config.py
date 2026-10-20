"""Read Azure staging settings; no cloud calls or import-time environment changes.

Reuses the Lab 4 convention: cloud.env values are overridden by exported variables.
Deployment settings stay blank until explicitly configured; no cloud lookup occurs here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "cloud.env"
STAGING_SLOTS = (
    "CLOUD_PROVIDER",
    "PROJECT_ID",
    "REGION",
    "AZURE_SUBSCRIPTION_ID",
    "ENDPOINT_NAME",
    "AZURE_CONTAINERAPPS_ENVIRONMENT",
    "AZURE_MANAGED_IDENTITY_ID",
)


@dataclass(frozen=True)
class StagingConfig:
    provider: str
    project_id: str
    region: str
    azure_subscription_id: str
    endpoint_name: str
    azure_containerapps_environment: str
    azure_managed_identity_id: str
    serving_instance: str = "0.5cpu-1Gi"
    serving_image: str = ""
    serving_allowed_ip: str = ""
    mlflow_tracking_uri: str = ""


def load_staging(env_file: Path = ENV_FILE) -> StagingConfig:
    """Read plain KEY=value settings, without executing or exporting file contents."""
    values = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    values.update(os.environ)
    missing = [key for key in STAGING_SLOTS if not values.get(key, "").strip()]
    if missing:
        raise RuntimeError("Missing staging configuration: " + ", ".join(missing))
    return StagingConfig(
        provider=values["CLOUD_PROVIDER"].strip(),
        project_id=values["PROJECT_ID"].strip(),
        region=values["REGION"].strip(),
        azure_subscription_id=values["AZURE_SUBSCRIPTION_ID"].strip(),
        endpoint_name=values["ENDPOINT_NAME"].strip(),
        azure_containerapps_environment=values["AZURE_CONTAINERAPPS_ENVIRONMENT"].strip(),
        azure_managed_identity_id=values["AZURE_MANAGED_IDENTITY_ID"].strip(),
        serving_instance=values.get("SERVING_INSTANCE", "0.5cpu-1Gi").strip(),
        serving_image=values.get("SERVING_IMAGE", "").strip(),
        serving_allowed_ip=values.get("SERVING_ALLOWED_IP", "").strip(),
        mlflow_tracking_uri=values.get("MLFLOW_TRACKING_URI", "").strip(),
    )
