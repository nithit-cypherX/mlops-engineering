"""Compile the Lab 5 neutral DAG into an Azure ML job, locally only.

No MLClient, credentials, resource lookups, image builds, uploads or submission.
Step code must already be in the supplied digest-pinned image.
"""
import argparse
import io
import re
import shlex
import sys
from pathlib import Path
from uuid import UUID

import yaml

from cloudlayer import lab5_scope as scope
from cloudlayer.azure_staging import ACR_SERVER, validate_settings
from scripts.evaluation_gate import MIN_ABSOLUTE, checked_auc
from src.config import ENV_FILE, StagingConfig, load_staging

SPEC = Path(__file__).resolve().parents[1] / "pipeline" / "pipeline.yaml"
TYPES = {"file": "uri_file", "folder": "uri_folder", "string": "string", "number": "number"}
TOKEN = re.compile(r"\$\{(inputs|outputs)\.([a-z_][a-z0-9_]*)\}")
HANDOFFS = {
    "ingest": {"raw": "inputs.raw_data"},
    "validate": {"raw": "ingest.raw_data"},
    "train": {"validated": "validate.validated", "dvc_metadata": "inputs.dvc_metadata",
              "source_commit": "inputs.source_commit"},
    "evaluate": {"training": "train.bundle", "minimum": "inputs.gate_minimum"},
    "register": {"training": "train.bundle", "gate": "evaluate.decision"},
    "deploy_staging": {"registered": "register.registered"},
}


def load_spec(path: Path = SPEC) -> dict:
    """Keep runtime values separate: never expand the host environment into YAML."""
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_spec(spec)
    return spec


def validate_spec(spec: object) -> None:
    if not isinstance(spec, dict) or set(spec) != {"name", "inputs", "outputs", "steps"}:
        raise ValueError("Expected the Lab 5 DAG fields only; scheduling is separate")
    if spec["name"] != "itcs355-lab5-training" or spec["inputs"] != {
        "raw_data": "file", "dvc_metadata": "file", "source_commit": "string", "gate_minimum": "number",
    }:
        raise ValueError("Unexpected pipeline name or input contract")
    steps = spec["steps"]
    if (not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps)
            or [step.get("name") for step in steps] != list(HANDOFFS)):
        raise ValueError("Expected the six Task 1 steps in dependency order")
    outputs = {"inputs": spec["inputs"]}
    for step in steps:
        name = step["name"]
        if set(step) != {"name", "inputs", "outputs", "depends_on", "command"}:
            raise ValueError(f"Unsupported DAG fields for {name}")
        if step["inputs"] != HANDOFFS[name]:
            raise ValueError(f"Missing or changed required file handoff for {name}")
        parents = set()
        for reference in step["inputs"].values():
            parent, output = reference.split(".")
            if parent not in outputs or output not in outputs[parent]:
                raise ValueError(f"Unknown or forward input reference: {reference}")
            if parent != "inputs":
                parents.add(parent)
        dependencies = step["depends_on"]
        if (not isinstance(dependencies, list) or set(dependencies) != parents
                or len(dependencies) != len(parents)):
            raise ValueError(f"depends_on must match actual data dependencies for {name}")
        if (not isinstance(step["outputs"], dict) or not step["outputs"]
                or any(not re.fullmatch(r"[a-z_][a-z0-9_]*", key) or kind != "folder"
                       for key, kind in step["outputs"].items())):
            raise ValueError(f"Expected named output folders for {name}")
        tokens = step["command"]
        if not isinstance(tokens, list) or not tokens or any(not isinstance(t, str) or not t for t in tokens):
            raise ValueError(f"Expected a command argument list for {name}")
        used = {(direction, key) for token in tokens for direction, key in TOKEN.findall(token)}
        expected = {(direction, key) for direction in ("inputs", "outputs") for key in step[direction]}
        if used != expected or any("${" in TOKEN.sub("", token) for token in tokens):
            raise ValueError(f"Command must consume the declared inputs and outputs for {name}")
        outputs[name] = step["outputs"]
    if spec["outputs"] != {
        "training": "train.bundle", "gate": "evaluate.decision",
        "registered_model": "register.registered", "deployment": "deploy_staging.deployment",
    }:
        raise ValueError("Unexpected pipeline output contract")
    for reference in spec["outputs"].values():
        parent, output = reference.split(".")
        if output not in outputs[parent]:
            raise ValueError(f"Unknown pipeline output: {reference}")


def compile_azure(
    spec: dict, *, raw_data: Path, dvc_metadata: Path, source_commit: str,
    image: str, compute: str, training_client_id: str, release_client_id: str,
    staging: StagingConfig, gate_minimum: float = MIN_ABSOLUTE,
):
    validate_spec(spec)
    validate_settings(staging)
    for path in (raw_data, dvc_metadata):
        if not path.is_file():
            raise ValueError(f"Pipeline input must be an existing file: {path}")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Pipeline source_commit must be a full clean Git SHA")
    if not re.fullmatch(
        re.escape(ACR_SERVER) + r"/[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}", image,
    ):
        raise ValueError("Pipeline image must be digest-pinned in the reviewed ACR")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{1,62}", compute):
        raise ValueError("Compute must be an explicit existing AML compute name, not a VM size")
    for client_id in (training_client_id, release_client_id):
        if not isinstance(client_id, str) or UUID(client_id).int == 0:
            raise ValueError("Explicit nonzero managed identity client IDs are required")
    gate_minimum = checked_auc(gate_minimum, "gate minimum")
    if gate_minimum < MIN_ABSOLUTE:
        raise ValueError("Pipeline gate cannot lower the normal 0.80 floor")

    # SDK construction/serialization only. Imports do not create an Azure client.
    from azure.ai.ml import Input, Output, command, dsl
    from azure.ai.ml.entities import Environment, ManagedIdentityConfiguration

    definitions = {"inputs": spec["inputs"]}
    components = {}
    for step in spec["steps"]:
        name = step["name"]
        declared = {}
        for key, reference in step["inputs"].items():
            parent, output = reference.split(".")
            kind = TYPES[definitions[parent][output]]
            declared[key] = Input(type=kind, **({"mode": "download"} if kind.startswith("uri_") else {}))
        client_id = release_client_id if name == "deploy_staging" else training_client_id
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "20260101"}
        if name in {"register", "deploy_staging"}:
            env.update(CLOUD_PROVIDER="azure", AZURE_CLIENT_ID=client_id)
        if name == "deploy_staging":
            env.update({
                "PROJECT_ID": staging.project_id, "REGION": staging.region,
                "AZURE_SUBSCRIPTION_ID": staging.azure_subscription_id,
                "ENDPOINT_NAME": staging.endpoint_name,
                "AZURE_CONTAINERAPPS_ENVIRONMENT": staging.azure_containerapps_environment,
                "AZURE_MANAGED_IDENTITY_ID": staging.azure_managed_identity_id,
                "SERVING_INSTANCE": staging.serving_instance, "SERVING_IMAGE": staging.serving_image,
                "SERVING_ALLOWED_IP": staging.serving_allowed_ip,
                "MLFLOW_TRACKING_URI": staging.mlflow_tracking_uri,
            })
        tokens = [
            TOKEN.sub(lambda m: "${{" + m[1] + "." + m[2] + "}}", token)
            for token in step["command"]
        ]
        components[name] = command(
            name="lab5_" + name, command=shlex.join(tokens),
            environment=Environment(image=image), code=None,
            inputs=declared,
            outputs={key: Output(type="uri_folder", mode="upload") for key in step["outputs"]},
            compute="azureml:" + compute, instance_count=1,
            identity=ManagedIdentityConfiguration(client_id=client_id),
            environment_variables=env, is_deterministic=False,
        )
        definitions[name] = step["outputs"]

    @dsl.pipeline(display_name=spec["name"], experiment_name=spec["name"])
    def graph(raw_data: Input(type="uri_file"), dvc_metadata: Input(type="uri_file"),
              source_commit: str, gate_minimum: float):
        roots = {"raw_data": raw_data, "dvc_metadata": dvc_metadata,
                 "source_commit": source_commit, "gate_minimum": gate_minimum}
        nodes = {}
        for step in spec["steps"]:
            values = {}
            for key, reference in step["inputs"].items():
                parent, output = reference.split(".")
                values[key] = roots[output] if parent == "inputs" else nodes[parent].outputs[output]
            node = components[step["name"]](**values)
            node.name = step["name"]
            nodes[step["name"]] = node
        return {
            name: nodes[reference.split(".")[0]].outputs[reference.split(".")[1]]
            for name, reference in spec["outputs"].items()
        }

    job = graph(
        raw_data=Input(type="uri_file", path=str(raw_data.resolve()), mode="download"),
        dvc_metadata=Input(type="uri_file", path=str(dvc_metadata.resolve()), mode="download"),
        source_commit=source_commit, gate_minimum=gate_minimum,
    )
    job.tags = dict(scope.TAGS)
    job.settings.continue_on_step_failure = False
    job.settings.force_rerun = True
    return job


def dump_azure(job, output: Path) -> None:
    """Export SDK YAML while preserving the two local input file paths."""
    stream = io.StringIO()
    job.dump(stream)
    document = yaml.safe_load(stream.getvalue())
    # Installed SDK serializes local strings as azureml:<path>. Keep these
    # explicitly local inputs local; do not alter step bindings or cloud assets.
    for name in ("raw_data", "dvc_metadata"):
        document["inputs"][name]["path"] = str(Path(job.inputs[name].path).resolve())
    with output.open("x", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC)
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument("--raw-data", type=Path, required=True)
    parser.add_argument("--dvc-metadata", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--compute", required=True)
    parser.add_argument("--training-client-id", required=True)
    parser.add_argument("--release-client-id", required=True)
    parser.add_argument("--gate-minimum", type=float, default=MIN_ABSOLUTE)
    parser.add_argument("--output", type=Path, required=True, help="new local YAML file; never submitted")
    args = vars(parser.parse_args(argv))
    output = args.pop("output")
    spec_path = args.pop("spec")
    env_file = args.pop("env_file")
    try:
        job = compile_azure(load_spec(spec_path), staging=load_staging(env_file), **args)
        dump_azure(job, output)  # Exclusive file creation; no upload or submission.
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"COMPILE FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"Compiled local Azure ML YAML: {output}. No upload or submission was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
