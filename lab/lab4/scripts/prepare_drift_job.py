"""Prepare a local-only, allowlisted Lab 4 code snapshot; never submit a job.

Usage: python -m scripts.prepare_drift_job --output <new-directory-outside-lab-folder>
The parent directory must exist. Existing output is never replaced.
Only the listed code and pinned CSV enter the snapshot, not cloud.env or credentials.
The manifest records exact bytes because the working tree may be uncommitted.
Review/rebuild before upload if any source changes. No Docker pull/build occurs here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.pull_data import DATA_SHA256

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "src/__init__.py", "src/config.py", "src/data.py",
    "cloudlayer/__init__.py", "cloudlayer/base.py", "cloudlayer/factory.py", "cloudlayer/azure.py",
    "monitoring/drift.py", "monitoring/run_drift.py", "scripts/pull_data.py",
    "data/raw/sensors.csv",
)
COMMAND = "PYTHONPATH=. python -m monitoring.run_drift"
# Clear the old image's training ENTRYPOINT, leaving the platform launcher intact.
# Azure ML JobResourceConfiguration.docker_args accepts Docker run arguments:
# https://learn.microsoft.com/python/api/azure-ai-ml/azure.ai.ml.entities.jobresourceconfiguration
# Local Docker verification does not prove the managed launcher until the live job.
DOCKER_ARGS = '--entrypoint=""'


def prepare(destination: Path, *, source: Path = ROOT) -> dict:
    source = source.resolve()
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Output already exists; refusing to replace it.")
    if destination.resolve().is_relative_to(source):
        raise ValueError("Keep the generated snapshot outside the Lab 4 source folder.")
    content = {}
    for name in FILES:
        path = source / name
        if path.resolve() != path or not path.is_file():
            raise ValueError("Snapshot input is missing, non-regular or symlinked.")
        content[name] = path.read_bytes()
    if hashlib.sha256(content["data/raw/sensors.csv"]).hexdigest() != DATA_SHA256:
        raise ValueError("Reference CSV does not match the pinned training dataset.")
    manifest = {
        "purpose": "Lab 4 one-shot drift job; local preparation only",
        "command": COMMAND, "docker_args": DOCKER_ARGS,
        "reference_sha256": DATA_SHA256,
        "files": {name: {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
                  for name, blob in content.items()},
    }
    # Only non-secret allowlisted files live here; the image's runner must read them.
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    for name, blob in content.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        for parent in target.parents:
            if parent == destination:
                break
            parent.chmod(0o755)  # Also work when the host has a restrictive umask.
        target.write_bytes(blob)
        target.chmod(0o644)
    manifest_path = destination / "snapshot-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o644)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = prepare(args.output)
    except (OSError, ValueError) as error:
        print(f"SNAPSHOT FAILED: {type(error).__name__}; check output path and pinned inputs.")
        return 1
    print(json.dumps({"snapshot": str(args.output.absolute()), "files": len(manifest["files"]),
                      "bytes": sum(item["bytes"] for item in manifest["files"].values()),
                      "command": COMMAND, "docker_args": DOCKER_ARGS,
                      "uploaded": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
