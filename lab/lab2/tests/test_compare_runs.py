"""Comparison checks use synthetic rows and mocked reads; no Azure jobs or network."""
import itertools
import json
import os
import shlex
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from scripts import compare_runs
from src import config

STUDY_ID = "a" * 32


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))


@pytest.fixture
def tracked_runs():
    rows = []
    for i, (trees, depth, leaf) in enumerate(itertools.product([100, 300], [4, 8, 12], [1, 10])):
        rows.append({
            "run_id": f"run-{i:02d}", "status": "FINISHED",
            "tags.study_id": STUDY_ID, "tags.run_role": "trial",
            "params.n_estimators": str(trees), "params.max_depth": str(depth),
            "params.min_samples_leaf": str(leaf), "params.seed": "20260101",
            "params.instance": "test-instance", "tags.git_commit": "b" * 40,
            "tags.dvc_hash": "c" * 32 + ".dir", "tags.data_fingerprint": "d" * 16,
            "tags.image_uri": "example.invalid/training@sha256:" + "e" * 64,
            "metrics.val_roc_auc": 0.82 + i * 0.001,
            "metrics.test_roc_auc": 0.99 - i * 0.001,
            "metrics.duration_s": float(5 + i), "metrics.cost_thb": 0.001049,
        })
    return pd.DataFrame(rows)


def test_only_finished_trials_from_the_requested_study_are_ranked(tracked_runs):
    extras = []
    for changes in [{"tags.study_id": "f" * 32}, {"tags.run_role": "study"},
                    {"status": "FAILED"}, {"status": "RUNNING"}]:
        extras.append({**tracked_runs.iloc[0].to_dict(), **changes, "run_id": "excluded"})
    table = compare_runs.build_comparison(pd.concat([tracked_runs, pd.DataFrame(extras)]), STUDY_ID)
    assert len(table) == 12
    assert table["run_id"].tolist() == [f"run-{i:02d}" for i in reversed(range(12))]
    assert table.iloc[0]["test_roc_auc"] < table.iloc[-1]["test_roc_auc"]
    assert set(table["run_id"]) == set(tracked_runs["run_id"])


def test_ratios_and_total_use_full_precision_and_baseline_is_na(tracked_runs):
    tracked_runs["metrics.val_roc_auc"] = [0.8000412 + i * 0.00001 for i in range(12)]
    table = compare_runs.build_comparison(tracked_runs, STUDY_ID)
    assert table.iloc[0]["thb_per_point"] == pytest.approx(0.001049 / (0.00011 * 100))
    assert pd.isna(table.iloc[-1]["thb_per_point"])
    report = compare_runs.render_report(table, STUDY_ID, "test-study")
    assert "**0.0126 THB**" in report  # Rounding each cost first would produce 0.0120.
    assert "N/A" in report and "nan" not in report
    assert "not the Azure bill" in report
    assert "not model selection" in report
    assert all(run_id in report for run_id in tracked_runs["run_id"])


def test_tied_baseline_has_no_fake_ratio(tracked_runs):
    tracked_runs["metrics.val_roc_auc"] = 0.8
    table = compare_runs.build_comparison(tracked_runs, STUDY_ID)
    assert table["thb_per_point"].isna().all()


@pytest.mark.parametrize("count", [0, 11, 13])
def test_incomplete_or_extra_finished_trials_are_rejected(tracked_runs, count):
    frame = (tracked_runs.iloc[:count] if count <= 12 else
             pd.concat([tracked_runs, tracked_runs.iloc[:1]]))
    with pytest.raises(ValueError, match="Expected 12"):
        compare_runs.build_comparison(frame, STUDY_ID)


@pytest.mark.parametrize("column", ["metrics." + m for m in compare_runs.METRICS])
def test_missing_metrics_are_not_replaced_with_zero(tracked_runs, column):
    with pytest.raises(ValueError, match="Missing"):
        compare_runs.build_comparison(tracked_runs.drop(columns=column), STUDY_ID)


@pytest.mark.parametrize("column,value", [
    ("metrics.cost_thb", None), ("metrics.cost_thb", -1),
    ("metrics.cost_thb", float("inf")), ("metrics.val_roc_auc", float("nan")),
    ("metrics.val_roc_auc", 1.1), ("metrics.test_roc_auc", -0.1),
    ("metrics.duration_s", 0), ("params.n_estimators", "100.5"),
    ("params.max_depth", "bad"), ("params.min_samples_leaf", "0"),
    ("run_id", ""), ("params.seed", None),
])
def test_invalid_values_stop_the_report(tracked_runs, column, value):
    tracked_runs.loc[0, column] = value
    with pytest.raises(ValueError):
        compare_runs.build_comparison(tracked_runs, STUDY_ID)


@pytest.mark.parametrize("kind", ["run", "configuration", "constant-parameter", "mixed-seed",
                                  "mixed-data", "mixed-image"])
def test_duplicate_or_incomparable_rows_are_rejected(tracked_runs, kind):
    if kind == "run":
        tracked_runs.loc[1, "run_id"] = tracked_runs.loc[0, "run_id"]
    elif kind == "configuration":
        columns = ["params." + p for p in compare_runs.PARAMETERS]
        tracked_runs.loc[1, columns] = tracked_runs.loc[0, columns]
    elif kind == "constant-parameter":
        tracked_runs["params.n_estimators"] = "100"
    else:
        column = {"mixed-seed": "params.seed", "mixed-data": "tags.data_fingerprint",
                  "mixed-image": "tags.image_uri"}[kind]
        tracked_runs.loc[1, column] = "different"
    with pytest.raises(ValueError):
        compare_runs.build_comparison(tracked_runs, STUDY_ID)


@pytest.fixture
def launcher(tmp_path, monkeypatch, tracked_runs):
    out = tmp_path / "reports" / "comparison.md"
    argv = ["compare_runs", "--study-id", STUDY_ID, "--out", str(out)]
    load = Mock(return_value=SimpleNamespace(mlflow_tracking_uri="unused"))
    search = Mock(return_value=tracked_runs)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(compare_runs.config, "load", load)
    monkeypatch.setattr(compare_runs.mlflow, "set_tracking_uri", Mock())
    monkeypatch.setattr(compare_runs.mlflow, "get_experiment_by_name",
                        Mock(return_value=SimpleNamespace(experiment_id="123")))
    monkeypatch.setattr(compare_runs.mlflow, "search_runs", search)
    return SimpleNamespace(out=out, argv=argv, load=load, search=search)


def test_cli_reads_the_requested_study_and_writes_the_report(launcher):
    assert compare_runs.main() == 0
    launcher.search.assert_called_once_with(
        experiment_ids=["123"], filter_string=f"tags.study_id = '{STUDY_ID}'",
    )
    text = launcher.out.read_text()
    assert "12 finished trials" in text and "## Related evidence" in text
    assert "[model selection justification](../README.md#model-selection-justification)" in text
    assert "[five-seed results](lab2-seed-check.json)" in text
    assert "not done in this report yet" not in text


def test_invalid_evidence_does_not_overwrite_an_existing_report(launcher, tracked_runs):
    launcher.out.parent.mkdir()
    launcher.out.write_text("existing report")
    launcher.search.return_value = tracked_runs.iloc[:-1]
    with pytest.raises(ValueError, match="Expected 12"):
        compare_runs.main()
    assert launcher.out.read_text() == "existing report"


@pytest.mark.parametrize("value", ["", "not-a-study", "a' OR '1'='1"])
def test_invalid_study_id_is_rejected_before_any_reads(launcher, value):
    launcher.argv[2] = value
    with pytest.raises(SystemExit) as error:
        compare_runs.main()
    assert error.value.code == 2
    launcher.load.assert_not_called()
    launcher.search.assert_not_called()


def test_missing_experiment_does_not_create_a_report(launcher, monkeypatch):
    monkeypatch.setattr(compare_runs.mlflow, "get_experiment_by_name", Mock(return_value=None))
    with pytest.raises(ValueError, match="Experiment not found"):
        compare_runs.main()
    launcher.search.assert_not_called()
    assert not launcher.out.exists()


@pytest.mark.parametrize("study_id", [STUDY_ID, ""])
def test_make_compare_only_calls_the_comparison_script(study_id):
    runner = "import json,sys; print('LAUNCHED='+json.dumps(sys.argv[1:]))"
    python = f"{shlex.quote(sys.executable)} -c {shlex.quote(runner)}"
    result = subprocess.run(
        ["make", "--silent", "compare", f"PYTHON={python}", f"STUDY_ID={study_id}"],
        cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=30, env=dict(os.environ),
    )
    if study_id:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().removeprefix("LAUNCHED=")) == [
            "scripts/compare_runs.py", "--study-id", STUDY_ID,
        ]
    else:
        assert result.returncode != 0 and "Set STUDY_ID" in result.stdout
        assert "LAUNCHED=" not in result.stdout
