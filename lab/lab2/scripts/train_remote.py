"""Submit one managed training job through the adapter, then report its result."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config, seeds


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit and wait for one Lab 2 training job")
    parser.add_argument("--image-uri", required=True, help="Digest-pinned training image reference")
    parser.add_argument("--n-estimators", type=int, default=200)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--min-samples-leaf", type=int, default=5)
    parser.add_argument("--seed", type=int, default=seeds.DEFAULT_SEED)
    args = parser.parse_args()

    adapter = get_adapter(config.load())
    job_id = adapter.submit_training(args.image_uri, {
        "n_estimators": args.n_estimators,
        "max_depth": args.max_depth,
        "min_samples_leaf": args.min_samples_leaf,
        "seed": args.seed,
    })
    print(f"Submitted job: {job_id}", flush=True)
    result = adapter.wait_training(job_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
