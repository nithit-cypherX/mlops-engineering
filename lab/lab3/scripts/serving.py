"""Task 2 entry points. Run from the lab directory: python -m scripts.serving deploy|smoke.

Deployment needs a prepared environment and identity; it never grants permissions.
Smoke checks the response contract and version, not accuracy or load-test performance.
"""
from __future__ import annotations

import argparse
import json
import math

from cloudlayer.factory import get_adapter
from service.schemas import PredictRequest
from src import config

# Reuse the instructor's valid example, with two explicit feature variations.
BASE_PAYLOAD = {
    "temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
    "hours_since_service": 4200.0, "load_pct": 68.0, "ambient_humidity": 55.0,
}
SMOKE_PAYLOADS = (
    BASE_PAYLOAD,
    {**BASE_PAYLOAD, "temp_c": 92.0},
    {**BASE_PAYLOAD, "load_pct": 90.0},
)


def smoke(adapter, endpoint: str, version: str) -> list[dict]:
    results = []
    for index, payload in enumerate(SMOKE_PAYLOADS, start=1):
        PredictRequest.model_validate(payload)
        result = adapter.invoke(endpoint, payload)
        probability = result.get("probability")
        if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                or not math.isfinite(probability) or not 0 <= probability <= 1):
            raise RuntimeError(f"Smoke payload {index}: expected a finite probability in [0, 1]")
        if result.get("model_version") != version:
            raise RuntimeError(f"Smoke payload {index}: unexpected model_version")
        results.append({"payload": index, "probability": probability, "model_version": version})
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "smoke"))
    args = parser.parse_args(argv)
    cfg = config.load_deployment() if args.command == "deploy" else config.load_serving()
    if not cfg.endpoint_name:
        raise RuntimeError("Missing configuration: ENDPOINT_NAME")
    adapter = get_adapter(cfg)
    if args.command == "deploy":
        endpoint = adapter.deploy(
            f"models:/{cfg.model_registry_name}/{cfg.model_version}",
            cfg.endpoint_name, cfg.serving_instance,
        )
        print(json.dumps({"endpoint": endpoint, "model_version": cfg.model_version}))
    else:
        print(json.dumps({"smoke": "PASS", "results": smoke(
            adapter, cfg.endpoint_name, cfg.model_version,
        )}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
