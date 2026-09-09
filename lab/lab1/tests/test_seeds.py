"""Seed checks; MLflow integration uses a test double, not a trained model."""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from argparse import Namespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from src import config, data, seeds


def sample(seed):
    seeds.set_all(seed)
    return [random.random() for _ in range(4)], np.random.random(4).tolist()


def test_same_seed_repeats_random_sequences():
    assert sample(42) == sample(42)


def test_different_seeds_change_both_sequences():
    a, b = sample(1), sample(2)
    assert a[0] != b[0]
    assert a[1] != b[1]


@pytest.mark.parametrize("seed", [-1, 2**32])
def test_invalid_seed_rejected(seed):
    with pytest.raises(ValueError, match="seed must be"):
        seeds.set_all(seed)


def test_set_all_does_not_change_hash_environment(monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "7")
    seeds.set_all(42)
    assert os.environ["PYTHONHASHSEED"] == "7"


def test_hash_seed_is_effective_in_fresh_processes():
    code = (
        "import json; from src import seeds; "
        "seeds.require_hash_seed(42); seeds.set_all(42); "
        "print(json.dumps([hash('mlod-seed-check'), hash('machine-id')]))"
    )
    env = {**os.environ, "PYTHONHASHSEED": "42"}
    def run():
        return subprocess.check_output(
            [sys.executable, "-c", code], cwd=config.REPO_ROOT, env=env, text=True,
        )
    assert json.loads(run()) == json.loads(run())


def test_mismatched_hash_seed_fails_before_training():
    result = subprocess.run(
        [sys.executable, "-c", "from src import seeds; seeds.require_hash_seed(42)"],
        cwd=config.REPO_ROOT, env={**os.environ, "PYTHONHASHSEED": "7"},
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "Set PYTHONHASHSEED before starting Python" in result.stderr


def test_training_passes_seed_and_records_it_in_mlflow(tmp_path, monkeypatch):
    from src import train

    seed = seeds.DEFAULT_SEED
    # Startup behavior is covered by the subprocess tests above. Isolate this
    # logging/wiring test from the environment used to launch pytest.
    monkeypatch.setattr(seeds, "_INITIAL_HASH_SEED", str(seed))
    monkeypatch.chdir(tmp_path)
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    cfg = Namespace(raw_path=tmp_path / "unused.csv", mlflow_tracking_uri=tracking_uri)
    args = Namespace(
        seed=seed, n_estimators=2, max_depth=2, min_samples_leaf=1,
        experiment="task23-seed-check", run_name="test-double-not-training", metrics_out=None,
    )
    frame = pd.DataFrame({name: [0.0, 1.0] for name in data.FEATURES})
    frame[data.TARGET] = [0, 1]
    split_mock = Mock(return_value=(frame, frame, frame))
    model = Mock()
    model.predict_proba.return_value = np.array([[0.9, 0.1], [0.1, 0.9]])
    model_factory = Mock(return_value=model)
    monkeypatch.setattr(train, "parse_args", lambda: args)
    monkeypatch.setattr(train.config, "load", lambda **kwargs: cfg)
    monkeypatch.setattr(train.data, "load_raw", lambda path: frame)
    monkeypatch.setattr(train.data, "data_fingerprint", lambda path: "test-double-data")
    monkeypatch.setattr(train.data, "split", split_mock)
    monkeypatch.setattr(train, "RandomForestClassifier", model_factory)
    monkeypatch.setattr(train.mlflow.sklearn, "log_model", Mock())
    monkeypatch.setattr(train, "git_commit", lambda: "test-double-code")
    old_uri = train.mlflow.get_tracking_uri()
    try:
        train.main()
        client = train.mlflow.MlflowClient(tracking_uri=tracking_uri)
        experiment = client.get_experiment_by_name(args.experiment)
        runs = client.search_runs([experiment.experiment_id])
        assert len(runs) == 1
        assert runs[0].data.params["seed"] == str(seed)
        assert runs[0].data.params["python_hash_seed"] == str(seed)
        assert split_mock.call_args.kwargs["seed"] == seed
        assert model_factory.call_args.kwargs["random_state"] == seed
    finally:
        train.mlflow.set_tracking_uri(old_uri)
