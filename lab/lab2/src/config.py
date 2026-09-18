"""Configuration. The ONLY module in src/ allowed to know about the environment.

Course rule: nothing under src/ may contain a bucket name, a provider hostname, or an
absolute path from your machine. Everything arrives through here, which reads cloud.env.
`make portability-audit` enforces this, and Lab 5 grades it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / "cloud.env"
# Set by the image build; never copy the Git directory into the image.
IMAGE_GIT_COMMIT = os.environ.get("GIT_COMMIT", "")

# The eight capability slots every lab depends on. scripts/cloud_check.py resolves each.
CAPABILITY_SLOTS = (
    "CLOUD_PROVIDER",
    "PROJECT_ID",
    "REGION",
    "BLOB_URI",
    "CONTAINER_REGISTRY",
    "MLFLOW_TRACKING_URI",
    "MODEL_REGISTRY_NAME",
    "IDENTITY_REF",
)

# Lab 2's Azure adapter also needs the managed-training target.
AZURE_TRAINING_SLOTS = (
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_ML_WORKSPACE",
    "AZURE_ML_COMPUTE",
)


def _load_env_file(path: Path = ENV_FILE) -> None:
    """Minimal .env loader. An already-exported variable wins, so CI can override cloud.env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file()


@dataclass(frozen=True)
class Config:
    provider: str
    project_id: str
    region: str
    blob_uri: str
    container_registry: str
    mlflow_tracking_uri: str
    model_registry_name: str
    identity_ref: str
    data_dir: Path = field(default=REPO_ROOT / "data")
    reports_dir: Path = field(default=REPO_ROOT / "reports")
    azure_subscription_id: str = ""
    azure_ml_workspace: str = ""
    azure_ml_compute: str = ""

    @property
    def raw_path(self) -> Path:
        return self.data_dir / "raw" / "sensors.csv"

    @property
    def dvc_metadata_path(self) -> Path:
        return self.data_dir / "raw.dvc"

    def tags(self, lab: int) -> dict[str, str]:
        """Every cloud resource carries these. `make teardown` finds resources by tag."""
        return {"course": "itcs355", "student": self.project_id, "lab": str(lab)}


def load(strict: bool = True) -> Config:
    required = CAPABILITY_SLOTS
    if os.environ.get("CLOUD_PROVIDER", "").lower() == "azure":
        required += AZURE_TRAINING_SLOTS
    missing = [s for s in required if not os.environ.get(s)]
    if missing and strict:
        raise RuntimeError(
            "Missing configuration: " + ", ".join(missing)
            + "\nCopy cloud.env.example to cloud.env, fill it in, then run `make cloud-check`."
        )
    get = os.environ.get
    return Config(
        provider=get("CLOUD_PROVIDER", "local"),
        project_id=get("PROJECT_ID", "unset"),
        region=get("REGION", "unset"),
        blob_uri=get("BLOB_URI", ""),
        container_registry=get("CONTAINER_REGISTRY", ""),
        mlflow_tracking_uri=get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"),
        model_registry_name=get("MODEL_REGISTRY_NAME", "itcs355"),
        identity_ref=get("IDENTITY_REF", ""),
        azure_subscription_id=get("AZURE_SUBSCRIPTION_ID", ""),
        azure_ml_workspace=get("AZURE_ML_WORKSPACE", ""),
        azure_ml_compute=get("AZURE_ML_COMPUTE", ""),
    )
