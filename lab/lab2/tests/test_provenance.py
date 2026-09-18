"""Provenance checks use temporary metadata and mocked Git, never training."""
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config, train


def test_dvc_hash_valid(tmp_path):
    path = tmp_path / "raw.dvc"
    path.write_text("outs:\n- path: raw\n  hash: md5\n  md5: " + "a" * 32 + ".dir\n")
    assert train.dvc_hash(path) == "a" * 32 + ".dir"


def test_dvc_metadata_missing(tmp_path):
    with pytest.raises(RuntimeError, match="Cannot read DVC metadata"):
        train.dvc_hash(tmp_path / "missing.dvc")


@pytest.mark.parametrize("content", [
    "outs: [", "", "[]", "outs: []", "outs: [null]",
    "outs:\n- path: raw\n  hash: md5\n  md5: not-a-hash\n",
    "outs:\n- path: other\n  hash: md5\n  md5: " + "a" * 32 + ".dir\n",
])
def test_dvc_metadata_invalid(tmp_path, content):
    path = tmp_path / "raw.dvc"
    path.write_text(content)
    with pytest.raises(RuntimeError):
        train.dvc_hash(path)


@pytest.mark.parametrize("suffix", ["", "-dirty"])
def test_image_revision_without_git(monkeypatch, suffix):
    revision = "b" * 40 + suffix
    monkeypatch.setattr(config, "IMAGE_GIT_COMMIT", revision)
    run = Mock(side_effect=AssertionError("Image must not invoke Git"))
    monkeypatch.setattr(train.subprocess, "run", run)
    assert train.git_commit() == revision
    run.assert_not_called()


def test_invalid_image_revision(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_GIT_COMMIT", "unknown")
    with pytest.raises(RuntimeError, match="Invalid image Git revision"):
        train.git_commit()


@pytest.mark.parametrize("status,suffix", [("", ""), (" M src/train.py\n", "-dirty")])
def test_local_revision(monkeypatch, status, suffix):
    monkeypatch.setattr(config, "IMAGE_GIT_COMMIT", "")
    run = Mock(side_effect=[SimpleNamespace(stdout="b" * 40 + "\n"),
                            SimpleNamespace(stdout=status)])
    monkeypatch.setattr(train.subprocess, "run", run)
    assert train.git_commit() == "b" * 40 + suffix
    assert run.call_args_list[1].args[0] == [
        "git", "status", "--porcelain", "--untracked-files=normal",
    ]


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.CalledProcessError(1, "git")])
def test_local_git_unavailable(monkeypatch, error):
    monkeypatch.setattr(config, "IMAGE_GIT_COMMIT", "")
    monkeypatch.setattr(train.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(RuntimeError, match="Git revision unavailable"):
        train.git_commit()
