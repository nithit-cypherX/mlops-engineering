"""Test push orchestration without contacting Azure or Docker."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cloudlayer.azure import AzureAdapter


def test_push_returns_digest(monkeypatch):
    destination = "example.azurecr.io/course"
    reference = destination + "@sha256:" + "a" * 64
    run = Mock(return_value=SimpleNamespace(stdout=json.dumps([reference])))
    monkeypatch.setattr(subprocess, "run", run)
    adapter = AzureAdapter(SimpleNamespace(container_registry=destination))
    assert adapter.push_image("lab1:check") == reference
    assert [call.args[0] for call in run.call_args_list][:3] == [
        ["az", "acr", "login", "--name", "example"],
        ["docker", "tag", "lab1:check", destination + ":check"],
        ["docker", "push", destination + ":check"],
    ]


def test_login_failure_stops_push(monkeypatch):
    run = Mock(side_effect=subprocess.CalledProcessError(1, "az"))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        AzureAdapter(SimpleNamespace(container_registry="example.azurecr.io/course")).push_image(
            "lab1:check"
        )
    assert run.call_count == 1


def test_invalid_destination_rejected():
    with pytest.raises(ValueError):
        AzureAdapter(SimpleNamespace(container_registry="not-a-registry")).push_image("lab1:check")


def test_missing_digest_rejected(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(return_value=SimpleNamespace(stdout="[]")))
    with pytest.raises(RuntimeError, match="digest"):
        AzureAdapter(SimpleNamespace(container_registry="example.azurecr.io/course")).push_image(
            "lab1:check"
        )
