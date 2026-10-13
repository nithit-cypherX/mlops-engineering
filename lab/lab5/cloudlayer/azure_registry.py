"""Register a gated Lab 5 export with an explicitly supplied Azure ML client.

The callable accepts a client; the CLI creates one only after local preflight.
The CLI is a cloud-write command, not an offline check. It requires a matching
gate report and never promotes or deploys a model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

from cloudlayer.lab5_scope import GROUP, SUBSCRIPTION
from cloudlayer.azure_storage import ARTIFACTS_DATASTORE, validate_datastore
from scripts.evaluation_gate import METRIC, MIN_ABSOLUTE, checked_auc, evaluate, read_pass_report

WORKSPACE = "mlw-itcs355-u6688124"
MODEL_NAME = "itcs355-u6688124-lab5"
MODEL_ROOT = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
    f"/providers/Microsoft.MachineLearningServices/workspaces/{WORKSPACE}"
    f"/models/{MODEL_NAME}"
)
MODEL_FILES = {"MLmodel", "model.pkl", "conda.yaml", "python_env.yaml", "requirements.txt"}
SCORES = ("val_roc_auc", "val_pr_auc", "test_roc_auc", "test_pr_auc")


def read_candidate(
    training_dir: Path, gate_report: Path | None = None,
) -> tuple[Path, dict, dict[str, str]]:
    """Check one completed export without unpickling model code."""
    report = training_dir / "metrics.json"
    if report.is_symlink():
        raise ValueError("Training metrics must not be a symlink")
    content = report.read_bytes()
    payload = json.loads(content)
    evaluate(payload)  # Reuse the normal 0.80 floor; no promotion-time override.
    gate = read_pass_report(gate_report, content) if gate_report is not None else None
    for key, pattern in (
        ("git_commit", r"[0-9a-f]{40}(?:-dirty)?"),
        ("data_fingerprint", r"[0-9a-f]{64}"),
        ("dvc_hash", r"[0-9a-f]{32}\.dir"),
    ):
        if not isinstance(payload.get(key), str) or not re.fullmatch(pattern, payload[key]):
            raise ValueError(f"Missing or invalid training lineage: {key}")
    seed = payload.get("seed")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("Missing or invalid training seed")
    if not isinstance(payload.get("params"), dict) or not payload["params"]:
        raise ValueError("Missing training parameters")
    for key in SCORES:
        checked_auc(payload.get(key), key)

    model_dir = training_dir / "model"
    if model_dir.is_symlink():
        raise ValueError("Model directory must not be a symlink")
    files = list(model_dir.iterdir())
    if {path.name for path in files} != MODEL_FILES or any(
        path.is_symlink() or not path.is_file() or path.stat().st_size == 0 for path in files
    ):
        raise ValueError("Expected exactly the five non-empty MLflow model files")
    recorded = payload.get("model_files_sha256")
    actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    if not isinstance(recorded, dict) or recorded != actual:
        raise ValueError("Model file hashes are missing or do not match the training result")

    try:
        model_doc = yaml.safe_load((model_dir / "MLmodel").read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("Invalid MLmodel metadata") from exc
    if not isinstance(model_doc, dict):
        raise ValueError("MLmodel must be a mapping")
    metadata = model_doc.get("metadata")
    expected = {key: value for key, value in payload.items() if key != "model_files_sha256"}
    if json.dumps(metadata, sort_keys=True, allow_nan=False) != json.dumps(
        expected, sort_keys=True, allow_nan=False,
    ):
        raise ValueError("Model metadata and training metrics/lineage do not match")
    flavors = model_doc.get("flavors")
    if (not isinstance(flavors, dict) or not isinstance(flavors.get("sklearn"), dict)
            or not isinstance(flavors.get("python_function"), dict)
            or flavors["sklearn"].get("pickled_model") != "model.pkl"
            or flavors["python_function"].get("model_path") != "model.pkl"):
        raise ValueError("Expected the exported sklearn model.pkl in both MLflow flavors")

    tags = {
        "course": "itcs355", "student": GROUP, "lab": "5",
        "gate_metric": METRIC,
        "gate_minimum": str(gate["minimum"] if gate is not None else MIN_ABSOLUTE),
        "seed": str(seed), "training_record_sha256": hashlib.sha256(content).hexdigest(),
        "model_sha256": actual["model.pkl"],
        **{key: payload[key] for key in ("git_commit", "data_fingerprint", "dvc_hash")},
        **{key: str(payload[key]) for key in SCORES},
    }
    return model_dir, payload, tags


def register_training(
    training_dir: Path, *, client, gate_report: Path | None = None,
) -> dict[str, str]:
    """Return a verified version reference; failures never return a success result.

    Use an isolated training output while registering. Hashes detect accidental
    mixing/changes, not a malicious rewrite of both model and metadata.
    """
    model_dir, _, tags = read_candidate(training_dir, gate_report)
    for attribute, expected in (
        ("subscription_id", SUBSCRIPTION),
        ("resource_group_name", GROUP),
        ("workspace_name", WORKSPACE),
    ):
        value = getattr(client, attribute, None)
        if not isinstance(value, str) or value.casefold() != expected.casefold():
            raise ValueError(f"Registry client has an unexpected {attribute}")

    # Imported after local checks. Tests pass a mock client, never real credentials.
    from azure.ai.ml.entities import Model
    from azure.core.exceptions import AzureError

    try:
        datastore = client.datastores.get(name=ARTIFACTS_DATASTORE)
    except AzureError as exc:
        raise RuntimeError("Cannot verify Lab 5 model storage; no registration was attempted") from exc
    validate_datastore(datastore, name=ARTIFACTS_DATASTORE)

    model = Model(
        name=MODEL_NAME, datastore=ARTIFACTS_DATASTORE,
        path=str(model_dir.resolve()), type="mlflow_model",
        description="Lab 5 model accepted by the validation ROC-AUC gate.",
        tags=tags,
    )  # Keep SDK-managed version allocation; never select an old production version.
    try:
        created = client.models.create_or_update(model)
    except AzureError as exc:
        raise RuntimeError(
            "Registration was not confirmed. Check the registry before retrying; "
            "the request may have created a version."
        ) from exc
    version = getattr(created, "version", None)
    if (getattr(created, "name", None) != MODEL_NAME
            or not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*", version)):
        raise RuntimeError("Unexpected registration response; inspect the registry before retrying")
    try:
        saved = client.models.get(name=MODEL_NAME, version=version)
    except AzureError as exc:
        raise RuntimeError(
            f"Version {version} was returned, but read-back failed; inspect it before retrying"
        ) from exc
    expected_id = f"{MODEL_ROOT}/versions/{version}"
    saved_id = getattr(saved, "id", None)
    saved_tags = getattr(saved, "tags", None)
    if (getattr(saved, "name", None) != MODEL_NAME
            or getattr(saved, "version", None) != version
            or getattr(saved, "type", None) != "mlflow_model"
            or not isinstance(saved_id, str) or saved_id.casefold() != expected_id.casefold()
            or not isinstance(saved_tags, dict)
            or any(saved_tags.get(key) != value for key, value in tags.items())):
        raise RuntimeError(
            f"Version {version} failed read-back verification; inspect it before retrying"
        )
    return {
        "name": MODEL_NAME, "version": version, "id": saved_id,
        "model_uri": f"azureml:{MODEL_NAME}:{version}",
    }


def main(argv: list[str] | None = None, *, client=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-dir", type=Path, required=True)
    parser.add_argument("--gate-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        # Fail before constructing credentials or making registry calls.
        read_candidate(args.training_dir, args.gate_report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if any(args.output_dir.iterdir()):
            raise FileExistsError("Registration output directory must be empty; use a new run directory")
        if client is None:
            from azure.ai.ml import MLClient
            from azure.identity import DefaultAzureCredential

            client = MLClient(DefaultAzureCredential(), SUBSCRIPTION, GROUP, WORKSPACE)
        result = register_training(
            args.training_dir, client=client, gate_report=args.gate_report,
        )
        try:
            with (args.output_dir / "registration.json").open("x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2, allow_nan=False)
                stream.write("\n")
        except OSError as exc:
            raise RuntimeError(
                f"Version {result['version']} was verified but its output could not be saved; "
                "inspect the registry before retrying"
            ) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"REGISTER FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
