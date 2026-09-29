"""Data tests.

Two kinds live here, and Lab 4 asks you to tell them apart:

  * schema / contract tests — assertions about the DATA. They fail when an upstream
    producer changes something, even though your code is untouched.
  * property tests — assertions about your splitting LOGIC. They fail when you change
    the code.

test_no_machine_leaks_across_splits is the one that matters most. It is the single
most common silent error in student projects: readings from one machine appearing in
both train and validation, producing a score that never survives contact with
production.
"""
from __future__ import annotations

import pytest

from src import config, data

RAW = config.REPO_ROOT / "data" / "raw" / "sensors.csv"


@pytest.fixture(scope="module")
def df():
    if not RAW.exists():
        pytest.fail(
            "Missing data/raw/sensors.csv: restore the dataset version recorded in data/raw.dvc.",
            pytrace=False,
        )
    return data.load_raw(RAW)


# --- contract tests: about the data -------------------------------------------------

def test_schema_columns_present_and_typed(df):
    missing = set(data.SCHEMA) - set(df.columns)
    assert not missing, f"missing columns: {sorted(missing)}"
    unexpected = set(df.columns) - set(data.SCHEMA)
    assert not unexpected, f"unexpected columns: {sorted(unexpected)}"
    for col, expected in data.SCHEMA.items():
        assert str(df[col].dtype) == expected, f"{col}: expected {expected}, got {df[col].dtype}"


def test_no_nulls_in_required_columns(df):
    nulls = df[list(data.SCHEMA)].isna().sum()
    offenders = nulls[nulls > 0]
    assert offenders.empty, f"null values found: {offenders.to_dict()}"


def test_features_within_plausible_ranges(df):
    for col, (lo, hi) in data.PLAUSIBLE_RANGES.items():
        assert df[col].min() >= lo, f"{col} below plausible floor: {df[col].min()}"
        assert df[col].max() <= hi, f"{col} above plausible ceiling: {df[col].max()}"


def test_target_is_binary_and_not_degenerate(df):
    values = set(df[data.TARGET].unique().tolist())
    assert values <= {0, 1}, f"target has values outside 0/1: {values}"
    rate = df[data.TARGET].mean()
    assert 0.01 < rate < 0.99, f"target is degenerate, positive rate = {rate:.4f}"


def test_identifier_is_unique(df):
    assert df[data.ID].is_unique, "reading_id is not unique"


# --- property tests: about the splitting logic --------------------------------------

def test_no_machine_leaks_across_splits(df):
    train, val, test = data.split(df, seed=42)
    tr, va, te = (set(p[data.GROUP]) for p in (train, val, test))
    assert not tr & va, f"machines in both train and val: {sorted(tr & va)[:5]}"
    assert not tr & te, f"machines in both train and test: {sorted(tr & te)[:5]}"
    assert not va & te, f"machines in both val and test: {sorted(va & te)[:5]}"


def test_split_is_deterministic_given_seed(df):
    a = data.split(df, seed=7)
    b = data.split(df, seed=7)
    for pa, pb in zip(a, b):
        assert pa[data.ID].tolist() == pb[data.ID].tolist()


def test_split_changes_with_seed(df):
    a, _, _ = data.split(df, seed=1)
    b, _, _ = data.split(df, seed=2)
    assert a[data.ID].tolist() != b[data.ID].tolist(), "seed has no effect — split is not random"


def test_every_row_lands_in_exactly_one_split(df):
    train, val, test = data.split(df, seed=13)
    total = len(train) + len(val) + len(test)
    assert total == len(df), f"rows lost or duplicated: {total} vs {len(df)}"


def test_data_fingerprint_matches_content_and_changes(tmp_path):
    """Known SHA-256 prefixes catch a constant hash and stale file contents."""
    sample = tmp_path / "sample.txt"
    sample.write_bytes(b"abc")
    assert data.data_fingerprint(sample) == "ba7816bf8f01cfea"
    assert data.data_fingerprint(sample) == "ba7816bf8f01cfea"
    sample.write_bytes(b"")
    assert data.data_fingerprint(sample) == "e3b0c44298fc1c14"


@pytest.mark.parametrize("contract_test, corrupt, message", [
    pytest.param(
        test_schema_columns_present_and_typed,
        lambda frame: frame.drop(columns=["temp_c"]),
        "missing columns", id="missing-column",
    ),
    pytest.param(
        test_schema_columns_present_and_typed,
        lambda frame: frame.assign(unexpected_sensor=0),
        "unexpected columns", id="unexpected-column",
    ),
    pytest.param(
        test_schema_columns_present_and_typed,
        lambda frame: frame.assign(temp_c=frame["temp_c"].astype(str)),
        "temp_c: expected float64", id="wrong-type",
    ),
    pytest.param(
        test_no_nulls_in_required_columns,
        lambda frame: frame.assign(temp_c=float("nan")),
        "null values found", id="null-feature",
    ),
    pytest.param(
        test_features_within_plausible_ranges,
        lambda frame: frame.assign(load_pct=250.0),
        "load_pct above plausible ceiling", id="out-of-range",
    ),
    pytest.param(
        test_target_is_binary_and_not_degenerate,
        lambda frame: frame.assign(failed_within_7d=2),
        "target has values outside 0/1", id="invalid-label",
    ),
    pytest.param(
        test_target_is_binary_and_not_degenerate,
        lambda frame: frame.assign(failed_within_7d=0),
        "target is degenerate", id="single-class-labels",
    ),
    pytest.param(
        test_identifier_is_unique,
        lambda frame: frame.assign(reading_id=frame["reading_id"].iloc[0]),
        "reading_id is not unique", id="duplicate-id",
    ),
])
def test_data_contract_rejects_corrupted_copy(df, contract_test, corrupt, message):
    """Exercise the real assertions on a copy; never change the reference CSV."""
    bad = corrupt(df.copy(deep=True))
    with pytest.raises(AssertionError, match=message):
        contract_test(bad)
