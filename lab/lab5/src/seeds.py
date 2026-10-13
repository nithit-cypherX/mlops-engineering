"""Seed control reused from Lab 2; hash randomization is set by the launcher."""
from __future__ import annotations

import os
import random
import sys

import numpy as np

DEFAULT_SEED = 20260101
_INITIAL_HASH_SEED = os.environ.get("PYTHONHASHSEED")


def require_hash_seed(seed: int) -> int:
    if sys.flags.ignore_environment or _INITIAL_HASH_SEED != str(seed):
        raise RuntimeError(
            "Set PYTHONHASHSEED before starting Python, matching --seed. "
            f"Example: PYTHONHASHSEED={seed} python -m src.train --seed {seed} ..."
        )
    return seed


def set_all(seed: int = DEFAULT_SEED) -> int:
    if not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be between 0 and 2**32 - 1")
    random.seed(seed)
    np.random.seed(seed)
    return seed
