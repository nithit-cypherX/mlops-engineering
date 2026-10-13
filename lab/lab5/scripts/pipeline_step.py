"""Provider-neutral command entry for the two cloud-writing pipeline steps."""
from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("register", "deploy_staging"))
    args, remaining = parser.parse_known_args(argv)
    if os.environ.get("CLOUD_PROVIDER") != "azure":
        print("PIPELINE STEP FAIL: only the Azure cloud steps are implemented", file=sys.stderr)
        return 1
    # Provider SDKs and operations stay in Layer 3. No credentials are created here.
    if args.step == "register":
        from cloudlayer.azure_registry import main as run
    else:
        from cloudlayer.azure_staging import main as run
    return run(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
