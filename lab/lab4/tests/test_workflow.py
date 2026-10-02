"""Offline checks for the staging handoff; never run Docker or cloud commands."""
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/lab4-ci.yml"


@pytest.fixture
def workflow():
    # BaseLoader keeps YAML's "on" key and GitHub expressions as strings.
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_staging_is_gated_by_green_main_and_uses_pushed_digest(workflow):
    jobs = workflow["jobs"]
    publisher = jobs["registry-main"]
    staging = jobs["deploy-staging"]
    assert publisher["needs"] == "checks"
    assert publisher["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert publisher["outputs"]["image"] == "${{ steps.push-image.outputs.image }}"
    assert staging["needs"] == "registry-main"
    assert staging["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main' && "
        "needs.registry-main.result == 'success'"
    )
    assert staging["environment"] == "lab4-staging"
    assert staging["env"]["SERVING_IMAGE"] == "${{ needs.registry-main.outputs.image }}"
    assert staging["env"]["SERVING_ALLOWED_IP"] == "${{ secrets.LAB4_SERVING_ALLOWED_IP }}"
    assert staging["permissions"] == {"contents": "read", "id-token": "write"}
    assert staging["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    runs = [step["run"] for step in staging["steps"] if "run" in step]
    assert "make deploy" in runs and "make smoke" in runs
    assert not any("serve-image" in run or "image-push" in run or "data-pull" in run for run in runs)
    assert not any("deploy" in step.get("run", "") or "image-push" in step.get("run", "")
                   for step in jobs["registry-pr"]["steps"])


def test_cleanup_runs_after_deploy_even_if_smoke_fails_and_is_not_silenced(workflow):
    staging = workflow["jobs"]["deploy-staging"]
    steps = staging["steps"]
    deploy = next(i for i, step in enumerate(steps) if step.get("id") == "deploy")
    smoke = next(i for i, step in enumerate(steps) if step.get("run") == "make smoke")
    cleanup = next(i for i, step in enumerate(steps) if step.get("run") == "make restore-access")
    assert deploy < smoke < cleanup
    assert steps[cleanup]["if"] == (
        "always() && steps.azure-login.outcome == 'success' && "
        "(steps.deploy.outcome == 'success' || steps.deploy.outcome == 'failure' ||\n"
        " steps.deploy.outcome == 'cancelled')"
    )
    assert "continue-on-error" not in staging
    assert all("continue-on-error" not in step for step in steps)
    assert staging["concurrency"] == {"group": "lab4-staging", "cancel-in-progress": "false"}
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert next(step for step in steps if step.get("id") == "azure-login")["with"]["client-id"] == (
        "${{ vars.LAB4_AZURE_DEPLOY_CLIENT_ID }}"
    )


def test_workflow_shell_and_embedded_python_syntax(workflow):
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" not in step:
                continue
            script = step["run"]
            result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=False)
            assert result.returncode == 0, result.stderr
            for code in re.findall(r"<<'PY'\n(.*?)\nPY", script, re.DOTALL):
                compile(code, str(WORKFLOW), "exec")


@pytest.mark.parametrize("reference,push_status,expected_ok", [
    ("registry.example/test@sha256:" + "a" * 64, 0, True),
    ("registry.example/other@sha256:" + "a" * 64, 0, False),
    ("registry.example/test@sha256:xyz", 0, False),
    ("registry.example/test:latest", 0, False),
    ("registry.example/test@sha256:" + "a" * 64, 1, False),
])
def test_push_output_accepts_only_successful_exact_repository_digest(
    workflow, tmp_path, reference, push_status, expected_ok,
):
    step = next(step for step in workflow["jobs"]["registry-main"]["steps"] if step.get("id") == "push-image")
    output = tmp_path / "outputs"
    env = {
        **os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(output),
        "GITHUB_SHA": "test-commit", "CONTAINER_REGISTRY": "registry.example/test",
        "FAKE_REFERENCE": reference, "FAKE_PUSH_STATUS": str(push_status),
    }
    # A shell function replaces make. The actual workflow script cannot push anything.
    stub = 'make() { printf "push log\\n%s\\n" "$FAKE_REFERENCE"; return "$FAKE_PUSH_STATUS"; }\n'
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", stub + step["run"]],
        env=env, text=True, capture_output=True, check=False,
    )
    assert (result.returncode == 0) is expected_ok
    assert (output.read_text() if output.exists() else "") == (
        f"image={reference}\n" if expected_ok else ""
    )
