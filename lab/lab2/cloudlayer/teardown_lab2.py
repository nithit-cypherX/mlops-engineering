"""Lab 2 compute-only cleanup, not Lab 5's general tag-based teardown.

Jobs are deliberately retained with the run artifacts needed by Lab 3.
Do not submit new jobs while this command is checking/deleting the compute.
"""
from __future__ import annotations

import argparse

from azure.ai.ml import MLClient
from azure.ai.ml.constants import ListViewType
from azure.ai.ml.entities import AmlCompute
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential

from src import config


# Destructive scope reviewed on 2026-09-19. A changed cloud.env must not widen it.
APPROVED_SCOPE = {
    "provider": "azure",
    "azure_subscription_id": "d9385e82-8bee-4612-8a76-967c241112c8",
    "project_id": "itcs355-u6688124",
    "azure_ml_workspace": "mlw-itcs355-u6688124",
    "azure_ml_compute": "cpu-lab2",
}
COMPUTE_ID = (
    f"/subscriptions/{APPROVED_SCOPE['azure_subscription_id']}"
    f"/resourceGroups/{APPROVED_SCOPE['project_id']}"
    "/providers/Microsoft.MachineLearningServices"
    f"/workspaces/{APPROVED_SCOPE['azure_ml_workspace']}"
    f"/computes/{APPROVED_SCOPE['azure_ml_compute']}"
)
EXPECTED_TAGS = {"course": "itcs355", "student": "itcs355-u6688124", "lab": "2"}


def teardown_compute(cfg: config.Config) -> list[str]:
    """Delete only the reviewed compute, returning its ID after confirmed absence."""
    if any(getattr(cfg, field) != value for field, value in APPROVED_SCOPE.items()):
        raise ValueError("Configuration differs from the reviewed Lab 2 teardown scope")
    client = MLClient(
        DefaultAzureCredential(), cfg.azure_subscription_id,
        cfg.project_id, cfg.azure_ml_workspace,
    )
    try:
        compute = client.compute.get(cfg.azure_ml_compute)
    except ResourceNotFoundError:
        return []  # Already absent: no deletion was performed.
    if (not isinstance(compute, AmlCompute)
            or compute.name != cfg.azure_ml_compute
            or str(compute.id).lower() != COMPUTE_ID.lower()
            or any((compute.tags or {}).get(k) != v for k, v in EXPECTED_TAGS.items())
            or compute.provisioning_state != "Succeeded"):
        raise ValueError("Compute identity, type, tags or provisioning state did not match")

    # This workspace has one reviewed compute. Block every unfinished job, including
    # pipelines whose children may use it; do not rely on job tags to infer safety.
    for job in client.jobs.list(list_view_type=ListViewType.ALL):
        if getattr(job, "status", None) not in {"Completed", "Failed", "Canceled"}:
            raise RuntimeError(
                f"Workspace has an unfinished or unknown-status job: {job.name}; nothing deleted"
            )

    poller = client.compute.begin_delete(cfg.azure_ml_compute, action="Delete")
    poller.result(timeout=600)
    if not poller.done():
        raise TimeoutError("Compute deletion is still pending; completion is not confirmed")
    try:
        client.compute.get(cfg.azure_ml_compute)
    except ResourceNotFoundError:
        return [COMPUTE_ID]
    raise RuntimeError("Delete returned, but the compute still exists; completion is not confirmed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete only the reviewed cpu-lab2 compute. Keep all jobs and model artifacts.",
    )
    parser.parse_args()
    deleted = teardown_compute(config.load())
    print(f"Deleted and confirmed absent: {deleted[0]}" if deleted
          else "Compute was not found; nothing deleted.")
    print("Jobs, workspace, models, artifacts, Storage and ACR are retained. "
          "Retained services may still incur costs.")


if __name__ == "__main__":
    main()
