"""Seed control. Log the seed as a parameter, never leave it as a comment."""
from __future__ import annotations

import os
import random
import sys

import numpy as np

DEFAULT_SEED = 20260101
# Capture before any training code can change the environment.
_INITIAL_HASH_SEED = os.environ.get("PYTHONHASHSEED")


def require_hash_seed(seed: int) -> int:
    """Require the launcher to set hash randomization before starting Python."""
    if sys.flags.ignore_environment or _INITIAL_HASH_SEED != str(seed):
        raise RuntimeError(
            "Set PYTHONHASHSEED before starting Python, matching --seed. "
            f"Example: PYTHONHASHSEED={seed} python -m src.train --seed {seed}. "
            "For Docker, pass -e PYTHONHASHSEED with the same value."
        )
    return seed


def set_all(seed: int = DEFAULT_SEED) -> int:
    if not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be between 0 and 2**32 - 1")
    random.seed(seed)
    np.random.seed(seed)
    # Hash randomization is configured by the launcher, not at runtime here.
    # Adding a deep-learning framework? Seed it here too, and say so in your README.
    return seed
