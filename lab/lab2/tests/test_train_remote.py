"""Check the launcher and Make target with a fake adapter, never a cloud job."""
import json
import os
import shlex
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from cloudlayer.base import CloudAdapter
from scripts import train_remote
from src import config, seeds


IMAGE_URI = "registry.example.invalid/training@sha256:" + "a" * 64
RESULT = {
    "job_id": "mock-job", "status": "Completed",
    "outputs": {"artifacts": "mock://lab2/runs/mock-job"},
}
REAL_CONFIG_LOAD = config.load


@pytest.fixture
def launcher(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    adapter = Mock(spec=CloudAdapter)
    adapter.submit_training.return_value = "mock-job"
    adapter.wait_training.return_value = RESULT
    cfg = object()
    load = Mock(return_value=cfg)
    factory = Mock(return_value=adapter)
    monkeypatch.setattr(train_remote.config, "load", load)
    monkeypatch.setattr(train_remote, "get_adapter", factory)
    argv = ["train_remote", "--image-uri", IMAGE_URI]
    monkeypatch.setattr(sys, "argv", argv)
    return SimpleNamespace(adapter=adapter, cfg=cfg, load=load, factory=factory, argv=argv)


@pytest.mark.parametrize("overrides,expected", [
    ([], {"n_estimators": 200, "max_depth": 8, "min_samples_leaf": 5,
          "seed": seeds.DEFAULT_SEED}),
    (["--n-estimators", "10", "--max-depth", "3", "--min-samples-leaf", "2", "--seed", "0"],
     {"n_estimators": 10, "max_depth": 3, "min_samples_leaf": 2, "seed": 0}),
])
def test_submit_once_then_wait_for_the_same_job(launcher, capsys, overrides, expected):
    launcher.argv.extend(overrides)
    train_remote.main()

    launcher.load.assert_called_once_with()
    launcher.factory.assert_called_once_with(launcher.cfg)
    assert launcher.adapter.mock_calls == [
        call.submit_training(IMAGE_URI, expected), call.wait_training("mock-job"),
    ]
    first_line, result = capsys.readouterr().out.split("\n", 1)
    assert first_line == "Submitted job: mock-job"
    assert json.loads(result) == RESULT


@pytest.mark.parametrize("args", [
    [], ["--image-uri"], ["--image-uri", IMAGE_URI, "--n-estimators", "not-an-integer"],
])
def test_invalid_cli_stops_before_configuration_or_submission(launcher, args):
    launcher.argv[:] = ["train_remote", *args]
    with pytest.raises(SystemExit) as error:
        train_remote.main()
    assert error.value.code == 2
    launcher.load.assert_not_called()
    launcher.factory.assert_not_called()
    assert launcher.adapter.mock_calls == []


def test_missing_configuration_stops_before_adapter(launcher, monkeypatch, capsys):
    # Use the real configuration validator; the module loaded cloud.env at import.
    monkeypatch.setenv("CLOUD_PROVIDER", "azure")
    monkeypatch.delenv("AZURE_ML_COMPUTE", raising=False)
    monkeypatch.setattr(train_remote.config, "load", REAL_CONFIG_LOAD)
    with pytest.raises(RuntimeError, match="AZURE_ML_COMPUTE"):
        train_remote.main()
    launcher.factory.assert_not_called()
    assert launcher.adapter.mock_calls == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("stage,error", [
    ("submit_training", ValueError("Invalid image")),
    ("submit_training", RuntimeError("Submission failed")),
    ("wait_training", RuntimeError("Job failed")),
    ("wait_training", TimeoutError("Job may still be running")),
])
def test_errors_propagate_without_resubmission(launcher, capsys, stage, error):
    getattr(launcher.adapter, stage).side_effect = error
    with pytest.raises(type(error), match=str(error)):
        train_remote.main()
    launcher.adapter.submit_training.assert_called_once()
    if stage == "submit_training":
        launcher.adapter.wait_training.assert_not_called()
        assert capsys.readouterr().out == ""
    else:
        launcher.adapter.wait_training.assert_called_once_with("mock-job")
        assert capsys.readouterr().out == "Submitted job: mock-job\n"


@pytest.mark.parametrize("mode", ["success", "wait-failure", "missing-image"])
def test_make_target_runs_launcher_without_build_or_push(mode):
    # This replaces only the adapter/config in the child process, not the launcher.
    runner = """
import json, os, runpy, sys
from unittest.mock import Mock, patch
adapter = Mock()
adapter.submit_training.return_value = 'mock-job'
adapter.wait_training.return_value = json.loads(os.environ['REMOTE_TEST_RESULT'])
if os.environ['REMOTE_TEST_MODE'] == 'wait-failure':
    adapter.wait_training.side_effect = RuntimeError('Mock job failed')
sys.argv = sys.argv[1:]
with patch('socket.socket.connect', side_effect=AssertionError('No network')), \
     patch('cloudlayer.factory.get_adapter', return_value=adapter), \
     patch('src.config.load', return_value=object()):
    runpy.run_path(sys.argv[0], run_name='__main__')
adapter.submit_training.assert_called_once_with(
    os.environ['IMAGE_URI'],
    {'n_estimators': 10, 'max_depth': 3, 'min_samples_leaf': 2, 'seed': 0})
adapter.wait_training.assert_called_once_with('mock-job')
assert len(adapter.mock_calls) == 2
"""
    python_command = f"{shlex.quote(sys.executable)} -c {shlex.quote('exec(' + repr(runner) + ')')}"
    result = subprocess.run([
        "make", "--silent", "train-remote",
        f"PYTHON={python_command}",
        f"IMAGE_URI={'' if mode == 'missing-image' else IMAGE_URI}",
        "N_ESTIMATORS=10", "MAX_DEPTH=3", "MIN_SAMPLES_LEAF=2", "SEED=0",
    ], cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=30,
        env={**os.environ, "REMOTE_TEST_MODE": mode, "REMOTE_TEST_RESULT": json.dumps(RESULT)})
    if mode == "success":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.split("\n", 1)[1]) == RESULT
    elif mode == "wait-failure":
        assert result.returncode != 0
        assert "Mock job failed" in result.stderr
        assert result.stdout == "Submitted job: mock-job\n"
    else:
        assert result.returncode != 0
        assert "Set IMAGE_URI" in result.stdout
        assert "Submitted job" not in result.stdout
