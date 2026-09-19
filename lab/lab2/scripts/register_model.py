"""Create one model version from a saved run; no training or stage change."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create one new model version with lineage; does not promote to Staging",
    )
    parser.add_argument("--model-uri", required=True, help="Saved model: runs:/<run-id>/model")
    args = parser.parse_args()
    cfg = config.load()
    version = get_adapter(cfg).register_model(args.model_uri, cfg.model_registry_name)
    print(json.dumps({"name": cfg.model_registry_name, "version": version,
                      "model_uri": f"models:/{cfg.model_registry_name}/{version}"}, indent=2))


if __name__ == "__main__":
    main()
