"""Local ingest/validate commands for later managed-pipeline components.

Both commands require explicit input/output paths. No data is generated or
downloaded. Each output directory must be empty to avoid replacing earlier work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from io import BytesIO
from pathlib import Path

import pandas as pd

from src.data import validate_contract


def _publish(output: Path, content: bytes, report_name: str, report: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise FileExistsError("Output directory must be empty; use a new run directory")
    # Exclusive creation also refuses existing files and symlinks.
    with (output / "sensors.csv").open("xb") as stream:
        stream.write(content)
    with (output / report_name).open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")


def ingest(source: Path, output: Path) -> dict:
    """Copy the supplied file byte-for-byte and record its full SHA-256."""
    content = source.read_bytes()
    report = {
        "stage": "ingest",
        "source_file": source.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }
    _publish(output, content, "ingest.json", report)
    return report


def validate(source: Path, output: Path) -> dict:
    """Publish a validated snapshot only after the supplied CSV passes the contract."""
    # Parse, fingerprint, and publish the same bytes; never read a fallback dataset.
    content = source.read_bytes()
    frame = pd.read_csv(BytesIO(content))
    validate_contract(frame)
    report = {
        "stage": "validate",
        "status": "pass",
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "rows": len(frame),
    }
    _publish(output, content, "validation.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("ingest", "validate"))
    parser.add_argument("--input", type=Path, required=True, help="Local input file")
    parser.add_argument("--output", type=Path, required=True, help="Empty output directory")
    args = parser.parse_args(argv)
    try:
        report = (ingest if args.step == "ingest" else validate)(args.input, args.output)
    except (OSError, ValueError) as exc:
        print(f"{args.step.upper()} FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
