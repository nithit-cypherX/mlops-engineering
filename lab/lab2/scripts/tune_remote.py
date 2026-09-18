"""Submit the whole trial loop as one managed job, never as laptop training."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config, seeds


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit and wait for one Lab 2 study job")
    parser.add_argument("--image-uri", required=True, help="Digest-pinned image containing src.tune")
    parser.add_argument("--budget-thb", type=float, required=True,
                        help="Explicit study allocation, not an Azure billing cap")
    parser.add_argument("--timeout-s", type=int, required=True,
                        help="Azure job run limit in seconds; not the whole billed VM lifetime")
    parser.add_argument("--trials", type=int, default=12)
    parser.add_argument("--trial-estimate-s", type=float, default=600.0)
    parser.add_argument("--seed", type=int, default=seeds.DEFAULT_SEED)
    parser.add_argument("--resume-from", default=None,
                        help="Stopped study job ID; read its checkpoint and write to a new job's path")
    parser.add_argument("--test-interruption", action="store_true",
                        help="Test only: fail after four checkpointed trials in a new study; "
                             "omit when resuming")
    args = parser.parse_args()
    if args.test_interruption and (args.resume_from or args.trials <= 4):
        parser.error("--test-interruption requires a new study with more than four trials; "
                     "omit it when resuming")

    adapter = get_adapter(config.load())
    study_args = {
        "mode": "tune", "trials": args.trials, "budget_thb": args.budget_thb,
        "timeout_s": args.timeout_s, "trial_estimate_s": args.trial_estimate_s,
        "seed": args.seed,
    }
    if args.resume_from:
        study_args["resume_from"] = args.resume_from
    if args.test_interruption:
        study_args["test_interruption"] = True
    job_id = adapter.submit_training(args.image_uri, study_args)
    print(f"Submitted study job: {job_id}", flush=True)
    result = adapter.wait_training(job_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
