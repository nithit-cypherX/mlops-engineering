"""Launcher/Make integration with a fake adapter: no jobs, image builds or pushes."""
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
from scripts import tune_remote
from src import config, seeds

IMAGE = "registry.example.invalid/training@sha256:" + "a" * 64
RESULT = {"job_id": "study", "status": "Completed", "outputs": {}}


@pytest.fixture
def launcher(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    adapter = Mock(spec=CloudAdapter)
    adapter.submit_training.return_value = "study"
    adapter.wait_training.return_value = RESULT
    cfg = object()
    load = Mock(return_value=cfg)
    factory = Mock(return_value=adapter)
    monkeypatch.setattr(tune_remote.config, "load", load)
    monkeypatch.setattr(tune_remote, "get_adapter", factory)
    argv = ["tune_remote", "--image-uri", IMAGE, "--budget-thb", "20", "--timeout-s", "10800"]
    monkeypatch.setattr(sys, "argv", argv)
    return SimpleNamespace(adapter=adapter, cfg=cfg, load=load, factory=factory, argv=argv)


@pytest.mark.parametrize("options,expected", [
    ([], {"trials": 12, "trial_estimate_s": 600.0, "seed": seeds.DEFAULT_SEED}),
    (["--trials", "2", "--trial-estimate-s", "300", "--seed", "0"],
     {"trials": 2, "trial_estimate_s": 300.0, "seed": 0}),
])
def test_submit_one_study_then_wait_for_the_same_job(launcher, capsys, options, expected):
    launcher.argv.extend(options)
    tune_remote.main()
    launcher.load.assert_called_once_with()
    launcher.factory.assert_called_once_with(launcher.cfg)
    assert launcher.adapter.mock_calls == [
        call.submit_training(IMAGE, {"mode": "tune", "budget_thb": 20.0,
                                     "timeout_s": 10800, **expected}),
        call.wait_training("study"),
    ]
    first, rest = capsys.readouterr().out.split("\n", 1)
    assert first == "Submitted study job: study"
    assert json.loads(rest) == RESULT


def test_resume_passes_the_stopped_job_id_without_a_second_submission(launcher):
    source = "lab2-" + "2" * 32
    launcher.argv.extend(["--resume-from", source])
    tune_remote.main()
    assert launcher.adapter.submit_training.call_args.args[1]["resume_from"] == source
    launcher.adapter.submit_training.assert_called_once()
    launcher.adapter.wait_training.assert_called_once_with("study")


def test_interruption_switch_is_forwarded_only_when_requested(launcher):
    launcher.argv.append("--test-interruption")
    tune_remote.main()
    assert launcher.adapter.submit_training.call_args.args[1]["test_interruption"] is True
    launcher.adapter.submit_training.assert_called_once()
    launcher.adapter.wait_training.assert_called_once_with("study")


@pytest.mark.parametrize("options", [["--trials", "4"], ["--resume-from", "lab2-" + "2" * 32]])
def test_invalid_interruption_request_stops_before_configuration(launcher, options):
    launcher.argv.extend(["--test-interruption", *options])
    with pytest.raises(SystemExit) as caught:
        tune_remote.main()
    assert caught.value.code == 2
    launcher.load.assert_not_called()
    launcher.factory.assert_not_called()
    assert launcher.adapter.mock_calls == []


@pytest.mark.parametrize("missing", ["--image-uri", "--budget-thb", "--timeout-s"])
def test_required_arguments_stop_before_configuration(launcher, missing):
    position = launcher.argv.index(missing)
    del launcher.argv[position:position + 2]
    with pytest.raises(SystemExit) as caught:
        tune_remote.main()
    assert caught.value.code == 2
    launcher.load.assert_not_called()
    launcher.factory.assert_not_called()
    assert launcher.adapter.mock_calls == []


@pytest.mark.parametrize("stage,error", [
    ("submit_training", ValueError("Invalid study")),
    ("submit_training", RuntimeError("Submission failed")),
    ("wait_training", RuntimeError("Job failed")),
    ("wait_training", TimeoutError("Job may still be running")),
])
def test_errors_are_not_hidden_or_automatically_resubmitted(launcher, capsys, stage, error):
    getattr(launcher.adapter, stage).side_effect = error
    with pytest.raises(type(error), match=str(error)):
        tune_remote.main()
    launcher.adapter.submit_training.assert_called_once()
    if stage == "submit_training":
        launcher.adapter.wait_training.assert_not_called()
        assert capsys.readouterr().out == ""
    else:
        launcher.adapter.wait_training.assert_called_once_with("study")
        assert capsys.readouterr().out == "Submitted study job: study\n"


@pytest.mark.parametrize("mode", [
    "success", "resume", "interruption", "invalid-interruption", "wait-failure",
    "IMAGE_URI", "BUDGET_THB", "TUNE_TIMEOUT_S",
])
def test_make_tune_calls_the_launcher_without_build_push_or_local_training(mode):
    runner = """
import json, os, runpy, sys
from unittest.mock import Mock, patch
adapter = Mock()
adapter.submit_training.return_value = 'study'
adapter.wait_training.return_value = json.loads(os.environ['TUNE_TEST_RESULT'])
if os.environ['TUNE_TEST_MODE'] == 'wait-failure':
    adapter.wait_training.side_effect = RuntimeError('Mock job failed')
sys.argv = sys.argv[1:]
assert sys.argv[0] == 'scripts/tune_remote.py', sys.argv
with patch('socket.socket.connect', side_effect=AssertionError('No network')), \
     patch('cloudlayer.factory.get_adapter', return_value=adapter), \
     patch('src.config.load', return_value=object()):
    runpy.run_path(sys.argv[0], run_name='__main__')
expected = {'mode': 'tune', 'trials': int(os.environ['TRIALS']), 'budget_thb': 20.0,
    'timeout_s': 10800, 'trial_estimate_s': 300.0, 'seed': 0}
if os.environ['RESUME_FROM']:
    expected['resume_from'] = os.environ['RESUME_FROM']
if os.environ['TEST_INTERRUPTION'] == '1':
    expected['test_interruption'] = True
adapter.submit_training.assert_called_once_with(os.environ['IMAGE_URI'], expected)
adapter.wait_training.assert_called_once_with('study')
assert len(adapter.mock_calls) == 2
"""
    python_command = f"{shlex.quote(sys.executable)} -c {shlex.quote('exec(' + repr(runner) + ')')}"
    values = {"IMAGE_URI": IMAGE, "BUDGET_THB": "20", "TUNE_TIMEOUT_S": "10800",
              "TRIALS": "12" if mode == "interruption" else "2",
              "TRIAL_ESTIMATE_S": "300", "SEED": "0",
              "RESUME_FROM": "lab2-" + "2" * 32 if mode == "resume" else "",
              "TEST_INTERRUPTION": ("invalid" if mode == "invalid-interruption"
                                    else "1" if mode == "interruption" else "0")}
    if mode in values:
        values[mode] = ""
    result = subprocess.run([
        "make", "--silent", "tune", f"PYTHON={python_command}",
        *(f"{key}={value}" for key, value in values.items()),
    ], cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=30,
        env={**os.environ, "TUNE_TEST_MODE": mode, "TUNE_TEST_RESULT": json.dumps(RESULT)})
    if mode in {"success", "resume", "interruption"}:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.split("\n", 1)[1]) == RESULT
    elif mode == "wait-failure":
        assert result.returncode != 0
        assert "Mock job failed" in result.stderr
        assert result.stdout == "Submitted study job: study\n"
    elif mode == "invalid-interruption":
        assert result.returncode != 0
        assert "TEST_INTERRUPTION must be 0 or 1" in result.stdout
        assert "Submitted study job" not in result.stdout
    else:
        assert result.returncode != 0
        assert f"Set {mode}" in result.stdout
        assert "Submitted study job" not in result.stdout
