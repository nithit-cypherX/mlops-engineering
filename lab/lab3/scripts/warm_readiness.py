"""Local gate for the one-replica warm diagnostic; no cloud operations here."""
import copy
import time


def warm_replica(snapshot):
    """Return the ready replica name, or None while readiness is unconfirmed."""
    if snapshot.get("configuration_unchanged") is not True:
        raise RuntimeError("App configuration changed during the diagnostic")
    state = snapshot.get("state", {})
    if state.get("provisioningState") in ("Failed", "Canceled"):
        raise RuntimeError("App provisioning failed")
    if state.get("runningStatus") != "Running" or state.get("provisioningState") != "Succeeded":
        return None
    revisions = snapshot.get("replicas", [])
    active = [revision for revision in revisions if revision.get("active") is True]
    if len(active) != 1 or any(type(revision.get("active")) is not bool for revision in revisions):
        return None
    revision = active[0]
    if not revision.get("name") or not (
        revision["name"] == state.get("latestRevisionName") == state.get("latestReadyRevisionName")
    ):
        return None
    if type(revision.get("replicas")) is not int or revision["replicas"] != 1:
        return None
    if any(type(r.get("replicas")) is not int or r["replicas"] != 0
           for r in revisions if r["active"] is False):
        return None
    replicas = revision.get("replica_details", [])
    if len(replicas) != 1 or not replicas[0].get("name"):
        return None
    properties = replicas[0].get("properties", {})
    # A terminated replica can retain ready=True in its old container fields.
    if properties.get("runningState") != "Running":
        return None
    containers = properties.get("containers", [])
    if len(containers) != 1:
        return None
    container = containers[0]
    if (container.get("ready") is not True or container.get("runningState") != "Running"
            or type(container.get("restartCount")) is not int or container["restartCount"] != 0):
        return None
    return replicas[0]["name"]


def wait_for_warm(read_snapshot, record_snapshot, *, deadline):
    """Poll within an absolute monotonic deadline shared with /ready.

    The reader must bound each I/O call by the same deadline. The controller
    records sanitized snapshots; this gate never retries a failed read or k6.
    """
    while time.monotonic() < deadline:
        try:
            current = read_snapshot(deadline)
        except Exception as exc:
            record_snapshot({"read_error": type(exc).__name__ + ": " + str(exc)})
            raise
        record_snapshot(copy.deepcopy(current))
        if time.monotonic() >= deadline:
            break
        replica = warm_replica(current)
        if replica is not None:
            return current, replica
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    raise TimeoutError("One ready/running replica was not confirmed before the readiness deadline")
