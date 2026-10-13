"""Sensor contract and group-aware split reused from Labs 2–4; no cloud code."""
from __future__ import annotations

import numpy as np
import pandas as pd

TARGET = "failed_within_7d"
GROUP = "machine_id"
ID = "reading_id"

FEATURES = [
    "temp_c", "vibration_mm_s", "pressure_kpa",
    "hours_since_service", "load_pct", "ambient_humidity",
]

SCHEMA: dict[str, str] = {
    ID: "int64",
    GROUP: "int64",
    "temp_c": "float64",
    "vibration_mm_s": "float64",
    "pressure_kpa": "float64",
    "hours_since_service": "float64",
    "load_pct": "float64",
    "ambient_humidity": "float64",
    TARGET: "int64",
}

PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "temp_c": (-10.0, 140.0),
    "vibration_mm_s": (0.0, 60.0),
    "pressure_kpa": (0.0, 600.0),
    "hours_since_service": (0.0, 20000.0),
    "load_pct": (0.0, 100.0),
    "ambient_humidity": (0.0, 100.0),
}


def validate_contract(frame: pd.DataFrame) -> None:
    """Apply Lab 4's contract to the actual input; invalid data raises ValueError.

    Runtime checks use exceptions, not assertions that Python -O can disable.
    This does not train, split, clean, or silently repair the input.
    """
    if not frame.columns.is_unique:
        raise ValueError("duplicate column names")
    missing = set(SCHEMA) - set(frame.columns)
    unexpected = set(frame.columns) - set(SCHEMA)
    if missing or unexpected:
        raise ValueError(
            f"schema mismatch: missing columns={sorted(missing)}, "
            f"unexpected columns={sorted(unexpected)}"
        )
    if frame.empty:
        raise ValueError("dataset has no rows")
    nulls = frame.isna().sum()
    offenders = nulls[nulls > 0]
    if not offenders.empty:
        raise ValueError(f"null values found: {offenders.to_dict()}")
    for column, expected in SCHEMA.items():
        if str(frame[column].dtype) != expected:
            raise ValueError(f"{column}: expected {expected}, got {frame[column].dtype}")
    for column, (low, high) in PLAUSIBLE_RANGES.items():
        if not frame[column].between(low, high).all():
            raise ValueError(f"{column} outside plausible range [{low}, {high}]")
    values = set(frame[TARGET].unique().tolist())
    if not values <= {0, 1}:
        raise ValueError(f"target has values outside 0/1: {values}")
    rate = frame[TARGET].mean()
    if not 0.01 < rate < 0.99:
        raise ValueError(f"target is degenerate, positive rate = {rate:.4f}")
    if not frame[ID].is_unique:
        raise ValueError("reading_id is not unique")


def split(
    df: pd.DataFrame,
    seed: int,
    val_frac: float = 0.2,
    test_frac: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Keep each machine in one partition, using the unchanged Lab 2 algorithm."""
    rng = np.random.default_rng(seed)
    groups = np.sort(df[GROUP].unique())
    shuffled = rng.permutation(groups)
    n = len(shuffled)
    n_test = int(round(n * test_frac))
    n_val = int(round(n * val_frac))
    test_g = set(shuffled[:n_test].tolist())
    val_g = set(shuffled[n_test:n_test + n_val].tolist())
    train_g = set(shuffled[n_test + n_val:].tolist())
    parts = tuple(
        df[df[GROUP].isin(group)].sort_values(ID).reset_index(drop=True)
        for group in (train_g, val_g, test_g)
    )
    if any(len(part) == 0 for part in parts):
        raise ValueError("A split partition is empty — too few machines for these fractions.")
    return parts
