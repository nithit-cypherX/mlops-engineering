"""Local snapshot boundaries; no cloud SDK calls, Docker or uploads."""
import hashlib
import json
import os
import shutil
import socket
from pathlib import Path

import pytest

from scripts import prepare_drift_job as snapshot


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Snapshot preparation must not use the network")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in snapshot.FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snapshot.ROOT / name, target)
    for name in ("cloud.env", ".env", ".git/config", ".venv/credential",
                 "src/train.py", "reports/private.txt", "scripts/unrelated.py"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("DO-NOT-COPY")
    return root


def test_only_allowlisted_inputs_and_exact_hashes(source, tmp_path):
    dest = tmp_path / "bundle"
    result = snapshot.prepare(dest, source=source)
    assert {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()} == (
        set(snapshot.FILES) | {"snapshot-manifest.json"}
    )
    assert result == json.loads((dest / "snapshot-manifest.json").read_text())
    for name, entry in result["files"].items():
        content = (dest / name).read_bytes()
        assert content == (source / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert len(content) == entry["bytes"]
        assert "DO-NOT-COPY" not in content.decode()
        assert (dest / name).stat().st_mode & 0o444 == 0o444
    assert result["command"] == "PYTHONPATH=. python -m monitoring.run_drift"
    assert result["docker_args"] == '--entrypoint=""'
    assert "src.train" not in result["command"]


def test_refuse_existing_output_without_changing_it(source, tmp_path):
    dest = tmp_path / "bundle"
    dest.mkdir()
    keep = dest / "keep"
    keep.write_text("original")
    with pytest.raises(ValueError, match="already exists"):
        snapshot.prepare(dest, source=source)
    assert list(dest.iterdir()) == [keep] and keep.read_text() == "original"


def test_nonroot_container_can_read_with_restrictive_host_umask(source, tmp_path):
    dest = tmp_path / "bundle"
    previous = os.umask(0o077)
    try:
        snapshot.prepare(dest, source=source)
    finally:
        os.umask(previous)
    for path in [dest, *dest.rglob("*")]:
        if path.is_dir():
            assert path.stat().st_mode & 0o555 == 0o555
        else:
            assert path.stat().st_mode & 0o444 == 0o444


def test_refuse_output_under_source(source):
    with pytest.raises(ValueError, match="outside"):
        snapshot.prepare(source / "bundle", source=source)
    assert not (source / "bundle").exists()


@pytest.mark.parametrize("mode", ["missing", "changed", "symlink"])
def test_bad_reference_leaves_no_output(mode, source, tmp_path):
    data = source / "data/raw/sensors.csv"
    if mode == "changed":
        data.write_bytes(b"wrong dataset")
    else:
        data.unlink()
        if mode == "symlink":
            data.symlink_to(snapshot.ROOT / "data/raw/sensors.csv")
    dest = tmp_path / "bundle"
    with pytest.raises(ValueError):
        snapshot.prepare(dest, source=source)
    assert not dest.exists()


def test_symlinked_code_directory_is_rejected(source, tmp_path):
    original = source / "src"
    moved = tmp_path / "moved-src"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        snapshot.prepare(tmp_path / "bundle", source=source)
    assert not (tmp_path / "bundle").exists()


def test_cli_reports_preparation_not_upload(monkeypatch, tmp_path, capsys):
    # Use actual pinned local inputs; this operation must stay offline.
    dest = tmp_path / "bundle"
    assert snapshot.main(["--output", str(dest)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["uploaded"] is False and result["files"] == len(snapshot.FILES)
    assert Path(result["snapshot"]) == dest
    assert snapshot.main(["--output", str(dest)]) == 1
    assert "SNAPSHOT FAILED" in capsys.readouterr().out
