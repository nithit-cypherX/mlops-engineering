"""Offline checks for the downloader; live data-contract tests stay separate."""
import hashlib
from http.client import IncompleteRead
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from scripts import pull_data


@pytest.fixture
def download(tmp_path, monkeypatch):
    content = b"known dataset\n"
    monkeypatch.setattr(pull_data, "DATA_SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(pull_data, "DATA_SIZE", len(content))
    fetch = Mock(return_value=BytesIO(content))
    monkeypatch.setattr(pull_data, "urlopen", fetch)
    monkeypatch.setattr(pull_data, "ROOT", tmp_path)
    return tmp_path / "data" / "raw" / "sensors.csv", content, fetch


def test_download_verifies_content_and_leaves_only_the_csv(download):
    destination, content, fetch = download
    assert "downloaded" in pull_data.pull_data(destination)
    fetch.assert_called_once_with(pull_data.DATA_URL, timeout=30)
    assert destination.read_bytes() == content
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("bad_content", [b"short", b"known dataset!", b"known dataset\nextra"])
def test_bad_download_fails_without_saving_csv(download, bad_content, capsys):
    destination, _, fetch = download
    fetch.return_value = BytesIO(bad_content)
    assert pull_data.main() == 1
    assert "wrong size or SHA-256" in capsys.readouterr().err
    assert not destination.exists()


@pytest.mark.parametrize("error", [
    HTTPError("https://example.invalid/data", 403, "Forbidden", {}, None),
    HTTPError("https://example.invalid/data", 404, "Not Found", {}, None),
    TimeoutError("download timed out"),
    IncompleteRead(b"partial", 20),
])
def test_network_failure_returns_nonzero_without_saving_csv(download, error, capsys):
    destination, _, fetch = download
    fetch.side_effect = error
    assert pull_data.main() == 1
    assert "DATA PULL FAILED" in capsys.readouterr().err
    assert not destination.exists()


def test_verified_existing_file_needs_no_network(download):
    destination, content, fetch = download
    destination.parent.mkdir(parents=True)
    destination.write_bytes(content)
    assert "existing" in pull_data.pull_data(destination)
    fetch.assert_not_called()
    assert destination.read_bytes() == content


def test_different_existing_file_is_not_overwritten(download, capsys):
    destination, _, fetch = download
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"local changes")
    assert pull_data.main() == 1
    assert "not overwriting" in capsys.readouterr().err
    fetch.assert_not_called()
    assert destination.read_bytes() == b"local changes"
