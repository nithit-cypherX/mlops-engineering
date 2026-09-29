"""Download the existing public Lab 4 dataset without cloud credentials or DVC.

data/raw.dvc pins directory 1c886b512c8a5c9bf723da1cd119fc80.dir; its manifest
maps sensors.csv to MD5 63ec074c360e4e75d5ac2acb431feaca (the URL below).
The SHA-256 also pins the exact CSV bytes, not just its remote location.
"""
from __future__ import annotations

import hashlib
from http.client import HTTPException
from pathlib import Path
import sys
import tempfile
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA_URL = (
    "https://itcs355u6688124.blob.core.windows.net/itcs355/itcs355/dvc/"
    "files/md5/63/ec074c360e4e75d5ac2acb431feaca"
)
DATA_SHA256 = "422cccb9136e814061b83e4f041cff21ba7372c6f7fb0feb2c321555c6a6341f"
DATA_SIZE = 321437
# ponytail: one fixed data version; ceiling: sensors.csv only;
# revisit when: data/raw.dvc changes; upgrade: update the verified URL and hash together.


def pull_data(destination: Path) -> str:
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != DATA_SHA256:
            raise ValueError(
                "Existing sensors.csv has a different SHA-256; not overwriting it. "
                "Review or move that file before downloading again."
            )
        return "DATA READY: existing sensors.csv matches the pinned SHA-256."

    # This is a public HTTPS GET: no Azure SDK, token, SAS or cloud.env is used.
    with urlopen(DATA_URL, timeout=30) as response:
        content = response.read(DATA_SIZE + 1)
    if len(content) != DATA_SIZE or hashlib.sha256(content).hexdigest() != DATA_SHA256:
        raise ValueError("Downloaded data has the wrong size or SHA-256; no CSV was saved.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            temp.write(content)
            temp.close()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return "DATA READY: downloaded sensors.csv and verified its SHA-256."


def main() -> int:
    try:
        print(pull_data(ROOT / "data" / "raw" / "sensors.csv"))
    except (OSError, URLError, HTTPException, ValueError) as error:
        print(f"DATA PULL FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
