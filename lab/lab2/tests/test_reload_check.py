"""Offline reload checks: real splitting, fake registry loader, no model training."""
import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from scripts import reload_check
from src import config, data


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    cfg = SimpleNamespace(raw_path=Path("dataset.csv"), mlflow_tracking_uri="https://tracking.invalid")
    load_config = Mock(return_value=cfg)
    monkeypatch.setattr(reload_check.config, "load", load_config)
    df = pd.DataFrame({
        **{feature: np.arange(36, dtype=float) + n for n, feature in enumerate(data.FEATURES)},
        data.ID: np.arange(36), data.GROUP: np.repeat(np.arange(12), 3),
        data.TARGET: np.arange(36) % 2,
    })
    load_raw = Mock(return_value=df)
    split = Mock(wraps=data.split)
    monkeypatch.setattr(reload_check.data, "load_raw", load_raw)
    monkeypatch.setattr(reload_check.data, "split", split)
    probabilities = np.array([[1, 0], [.9, .1], [.75, .25], [.5, .5], [0, 1]])
    model = SimpleNamespace(classes_=np.array([0, 1]),
                            predict_proba=Mock(return_value=probabilities),
                            fit=Mock(side_effect=AssertionError("Do not train")))
    loader = Mock(return_value=model)
    monkeypatch.setattr(reload_check.mlflow.sklearn, "load_model", loader)
    tracking = Mock()
    registry = Mock()
    monkeypatch.setattr(reload_check.mlflow, "set_tracking_uri", tracking)
    monkeypatch.setattr(reload_check.mlflow, "set_registry_uri", registry)
    argv = ["reload_check", "--name", "course-model", "--version", "1"]
    monkeypatch.setattr(sys, "argv", argv)
    return SimpleNamespace(cfg=cfg, load_config=load_config, df=df, load_raw=load_raw,
                           split=split, model=model, loader=loader, tracking=tracking,
                           registry=registry, argv=argv)


@pytest.mark.parametrize("explicit_rows", [False, True])
def test_loads_numeric_registry_version_and_scores_only_five_test_rows(setup, capsys, explicit_rows):
    if explicit_rows:
        setup.argv.extend(["--rows", "5"])
    assert reload_check.main() == 0
    setup.load_config.assert_called_once_with(strict=False)
    setup.load_raw.assert_called_once_with(setup.cfg.raw_path)
    assert setup.split.call_args.args[0] is setup.df
    assert setup.split.call_args.kwargs == {"seed": 20260101}
    setup.tracking.assert_called_once_with(setup.cfg.mlflow_tracking_uri)
    setup.registry.assert_called_once_with(setup.cfg.mlflow_tracking_uri)
    setup.loader.assert_called_once_with("models:/course-model/1")
    # With 12 groups and seed 20260101, the test groups are 3 and 6.
    expected = setup.df.iloc[[9, 10, 11, 18, 19]][data.FEATURES].reset_index(drop=True)
    setup.model.predict_proba.assert_called_once()
    pd.testing.assert_frame_equal(setup.model.predict_proba.call_args.args[0], expected)
    setup.model.fit.assert_not_called()
    output = capsys.readouterr().out
    rows = [line.strip() for line in output.splitlines() if line.startswith("  reading ")]
    assert rows == [
        "reading 9: p(failure)=0.0000", "reading 10: p(failure)=0.1000",
        "reading 11: p(failure)=0.2500", "reading 18: p(failure)=0.5000",
        "reading 19: p(failure)=1.0000",
    ]
    assert output.count("PASS") == 1


@pytest.mark.parametrize("options", [
    [], ["--name", "course-model"], ["--version", "1"],
    ["--name", "", "--version", "1"], ["--name", "../model.pkl", "--version", "1"],
    *[["--name", "course-model", "--version", value]
      for value in ("latest", "Staging", "@champion", "0", "-1", "1/model.pkl", "1.5")],
    *[["--name", "course-model", "--version", "1", "--rows", value]
      for value in ("0", "-1", "6")],
])
def test_invalid_arguments_stop_before_configuration_or_loading(setup, capsys, options):
    setup.argv[:] = ["reload_check", *options]
    with pytest.raises(SystemExit) as caught:
        reload_check.main()
    assert caught.value.code == 2
    setup.load_config.assert_not_called()
    setup.loader.assert_not_called()
    assert "PASS" not in capsys.readouterr().out


@pytest.mark.parametrize("rows", [0, 4])
def test_short_test_split_stops_before_model_download(setup, capsys, rows):
    setup.split.side_effect = None
    setup.split.return_value = (setup.df, setup.df, setup.df.head(rows))
    with pytest.raises(ValueError, match="five held-out rows"):
        reload_check.main()
    setup.loader.assert_not_called()
    assert "PASS" not in capsys.readouterr().out


@pytest.mark.parametrize("fault", ["missing-data", "missing-feature"])
def test_missing_data_or_feature_stops_before_model_download(setup, capsys, fault):
    if fault == "missing-data":
        setup.load_raw.side_effect = FileNotFoundError("Dataset missing")
        error = FileNotFoundError
    else:
        setup.load_raw.return_value = setup.df.drop(columns=data.FEATURES[0])
        error = KeyError
    with pytest.raises(error):
        reload_check.main()
    setup.loader.assert_not_called()
    assert "PASS" not in capsys.readouterr().out


@pytest.mark.parametrize("probabilities", [
    np.zeros((4, 2)), np.zeros(5), np.zeros((5, 3)),
    np.tile([float("nan"), .5], (5, 1)), np.tile([float("inf"), .5], (5, 1)),
    np.tile([-.1, 1.1], (5, 1)), np.tile([.1, .2], (5, 1)),
])
def test_bad_predictions_never_print_pass(setup, capsys, probabilities):
    setup.model.predict_proba.return_value = probabilities
    with pytest.raises(ValueError, match="class probabilities"):
        reload_check.main()
    assert "PASS" not in capsys.readouterr().out


@pytest.mark.parametrize("classes", [[1, 0], [0], [1, 2]])
def test_class_meaning_is_checked_before_prediction(setup, capsys, classes):
    setup.model.classes_ = np.array(classes)
    with pytest.raises(ValueError, match="binary classes"):
        reload_check.main()
    setup.model.predict_proba.assert_not_called()
    assert "PASS" not in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["load", "predict"])
def test_errors_propagate_without_local_fallback_or_retry(setup, capsys, stage):
    operation = setup.loader if stage == "load" else setup.model.predict_proba
    operation.side_effect = RuntimeError("Check failed")
    with pytest.raises(RuntimeError, match="Check failed"):
        reload_check.main()
    operation.assert_called_once()
    setup.loader.assert_called_once_with("models:/course-model/1")
    setup.model.fit.assert_not_called()
    assert "PASS" not in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["success", "missing-name", "missing-version", "reload-failure"])
def test_make_reload_only_calls_the_existing_script(mode):
    runner = "import json,sys; print('LAUNCHED='+json.dumps(sys.argv[1:])); "
    runner += f"sys.exit({7 if mode == 'reload-failure' else 0})"
    python = f"{shlex.quote(sys.executable)} -c {shlex.quote(runner)}"
    name = "" if mode == "missing-name" else "course-model"
    version = "" if mode == "missing-version" else "2"
    result = subprocess.run(
        ["make", "--silent", "reload-check", f"PYTHON={python}",
         f"MODEL_REGISTRY_NAME={name}", f"VERSION={version}"],
        cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=30,
        env=dict(os.environ),
    )
    if mode.startswith("missing-"):
        assert result.returncode != 0
        assert f"Set {'MODEL_REGISTRY_NAME' if not name else 'VERSION'}" in result.stdout
        assert "LAUNCHED=" not in result.stdout
    else:
        assert (result.returncode == 0) == (mode == "success"), result.stderr
        assert json.loads(result.stdout.strip().removeprefix("LAUNCHED=")) == [
            "scripts/reload_check.py", "--name", name, "--version", version,
        ]


def test_reload_exports_do_not_override_configuration_for_other_make_targets():
    runner = "import json,os; print(json.dumps([os.getenv('MODEL_REGISTRY_NAME'),os.getenv('VERSION')]))"
    python = f"{shlex.quote(sys.executable)} -c {shlex.quote(runner)}"
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"MODEL_REGISTRY_NAME", "VERSION"}}
    result = subprocess.run(
        ["make", "--silent", "portability-audit", f"PYTHON={python}"],
        cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=30, env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [None, None]
