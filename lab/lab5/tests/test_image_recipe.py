"""Check image preparation files only; these tests do not build or run Docker."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
INSTRUCTIONS = [line.strip() for line in DOCKERFILE.replace("\\\n", "").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
BASE = "python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534"


def test_digest_pinned_base_and_locked_install():
    assert [line for line in INSTRUCTIONS if line.startswith("FROM ")] == [
        f"FROM {BASE} AS builder", f"FROM {BASE} AS runtime",
    ]
    assert "RUN pip install --require-hashes --prefix=/install -r requirements.txt" in INSTRUCTIONS
    assert "RUN python -m pip uninstall -y packaging setuptools" in INSTRUCTIONS
    assert "RUN python -m pip check" in INSTRUCTIONS


def test_lock_pins_every_package_and_has_artifact_hashes():
    lines = (ROOT / "requirements.txt").read_text().replace("\\\n", "").splitlines()
    packages = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([a-z0-9_.-]+)==([^\s]+)\s+", line)
        assert match is not None, line
        name, version = match.groups()
        assert name not in packages
        packages[name] = version
        hashes = re.findall(r"--hash=sha256:([0-9a-f]+)", line)
        assert hashes and all(len(value) == 64 for value in hashes), name
    direct = re.findall(r"^([\w.-]+)(?:\[[\w,-]+\])?==([^\s]+)$",
                        (ROOT / "requirements.in").read_text(), flags=re.MULTILINE)
    assert direct
    for name, version in direct:
        assert packages[name.lower()] == version
    assert {name: packages[name] for name in ("mlflow", "scikit-learn", "numpy", "pandas",
                                            "azure-ai-ml", "azure-identity")} == {
        "mlflow": "2.22.5", "scikit-learn": "1.8.0", "numpy": "2.4.4", "pandas": "2.3.3",
        "azure-ai-ml": "1.35.0", "azure-identity": "1.25.3",
    }


def test_runtime_defaults_do_not_launch_training_or_cloud_operations():
    assert [line for line in INSTRUCTIONS if line.startswith("ENTRYPOINT")] == ["ENTRYPOINT []"]
    assert [line for line in INSTRUCTIONS if line.startswith("CMD")] == ['CMD ["python", "--help"]']
    assert [line for line in INSTRUCTIONS if line.startswith("USER ")] == ["USER runner"]
    assert "RUN useradd --create-home --uid 10001 runner" in INSTRUCTIONS
    assert "WORKDIR /app" in INSTRUCTIONS
    assert "PYTHONPATH=/app" in DOCKERFILE
    assert "PYTHONHASHSEED=20260101" in DOCKERFILE


def test_copy_scope_contains_only_lock_installed_packages_and_step_code():
    copies = [line for line in INSTRUCTIONS if line.startswith("COPY ")]
    assert copies == [
        "COPY requirements.txt ./", "COPY --from=builder /install /usr/local",
        "COPY --chown=runner:runner src/ ./src/",
        "COPY --chown=runner:runner cloudlayer/ ./cloudlayer/",
        "COPY --chown=runner:runner scripts/ ./scripts/",
    ]
    spec = yaml.safe_load((ROOT / "pipeline" / "pipeline.yaml").read_text())
    for step in spec["steps"]:
        command = step["command"]
        assert command[:2] == ["python", "-m"]
        module = command[2]
        assert module.split(".")[0] in {"src", "scripts", "cloudlayer"}
        assert (ROOT / (module.replace(".", "/") + ".py")).is_file()
    assert (ROOT / "cloudlayer" / "azure_registry.py").is_file()
    assert (ROOT / "cloudlayer" / "azure_staging.py").is_file()


def test_dockerignore_declares_secret_and_generated_file_exclusions():
    # Static guard only, not a substitute for inspecting the built image/context.
    patterns = set((ROOT / ".dockerignore").read_text().splitlines())
    assert {".git", ".venv", "cloud.env", ".env", ".env.*", "cloud.env.*",
            ".azure/", ".aws/", ".ssh/", "data/", "reports/", "mlruns/", "mlartifacts/",
            "**/__pycache__/", "**/*.pyc", "**/.env*", "**/cloud.env*",
            "tests/", "cloudlayer/tests/"} <= patterns


@pytest.mark.parametrize("revision,accepted", [
    ("a" * 40, True), ("", False), ("unknown", False),
    ("a" * 40 + "-dirty", False), ("A" * 40, False),
])
def test_build_revision_check_without_building(revision, accepted):
    assert [line for line in INSTRUCTIONS if line.startswith("ARG ")] == ["ARG GIT_COMMIT"]
    assert "LABEL org.opencontainers.image.revision=${GIT_COMMIT}" in INSTRUCTIONS
    guard = next(line for line in INSTRUCTIONS if line.startswith('RUN python -c "import os, re;'))
    tokens = shlex.split(guard.removeprefix("RUN "))
    assert tokens[:2] == ["python", "-c"]
    # Execute only this small local validation snippet, using the existing venv.
    result = subprocess.run(
        [sys.executable, "-c", tokens[2]],
        env={"GIT_COMMIT": revision, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert (result.returncode == 0) is accepted
    if not accepted:
        assert "GIT_COMMIT must be a full lowercase Git SHA" in result.stderr
