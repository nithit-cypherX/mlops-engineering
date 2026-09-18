"""Blob I/O with the installed SDK's signatures, fake storage and no network access."""
import socket
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient, StorageStreamDownloader

from cloudlayer import azure

ROOT = "https://example.blob.core.windows.net/private-container/lab2"
KEY = "studies/study-a/checkpoint.json"
URI = ROOT + "/" + KEY
CONTENT = b'{"study_id": "study-a", "completed": [], "spent_thb": 0.5}\n'


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("No network")))
    credential = create_autospec(DefaultAzureCredential, instance=True)
    credential.__enter__.return_value = credential
    credential.get_token.side_effect = AssertionError("No real authentication")
    credentials = Mock(return_value=credential)
    monkeypatch.setattr(azure, "DefaultAzureCredential", credentials)
    client = create_autospec(BlobClient, instance=True)
    client.__enter__.return_value = client
    factory = create_autospec(BlobClient, return_value=client)
    monkeypatch.setattr(azure, "BlobClient", factory)
    blobs = {}

    def upload(source, **kwargs):
        assert kwargs == {"overwrite": True}
        blobs[factory.call_args.kwargs["blob_name"]] = source.read()

    def download():
        key = factory.call_args.kwargs["blob_name"]
        if key not in blobs:
            raise ResourceNotFoundError(message="Blob not found")
        stream = create_autospec(StorageStreamDownloader, instance=True)
        stream.readinto.side_effect = lambda target: target.write(blobs[key])
        return stream

    client.upload_blob.side_effect = upload
    client.download_blob.side_effect = download
    adapter = azure.AzureAdapter(SimpleNamespace(blob_uri=ROOT))
    return SimpleNamespace(adapter=adapter, client=client, factory=factory, blobs=blobs,
                           credential=credential, credentials=credentials)


@pytest.mark.parametrize("content", [CONTENT, b""])
def test_upload_returns_scoped_uri_and_passes_exact_file_bytes(storage, tmp_path, content):
    source = tmp_path / "checkpoint.json"
    source.write_bytes(content)
    assert storage.adapter.upload(str(source), KEY) == URI
    storage.factory.assert_called_once_with(
        account_url="https://example.blob.core.windows.net", container_name="private-container",
        blob_name="lab2/" + KEY, credential=storage.credential,
    )
    assert storage.blobs == {"lab2/" + KEY: content}
    assert source.read_bytes() == content
    storage.client.upload_blob.assert_called_once()
    assert storage.client.upload_blob.call_args.args[0].closed
    storage.client.__exit__.assert_called_once()
    storage.credential.__exit__.assert_called_once()
    storage.credential.get_token.assert_not_called()
    # Check the real SDK constructs the same URL; construction itself performs no I/O.
    with BlobClient(**storage.factory.call_args.kwargs) as real_client:
        assert real_client.url == URI


def test_study_keys_are_separate_and_reupload_replaces_only_the_selected_blob(storage, tmp_path):
    source = tmp_path / "checkpoint.json"
    source.write_bytes(b"first-a")
    uri_a = storage.adapter.upload(str(source), KEY)
    source.write_bytes(b"first-b")
    uri_b = storage.adapter.upload(str(source), "studies/study-b/checkpoint.json")
    source.write_bytes(CONTENT)
    assert storage.adapter.upload(str(source), KEY) == uri_a
    assert storage.blobs == {
        "lab2/" + KEY: CONTENT, "lab2/studies/study-b/checkpoint.json": b"first-b",
    }
    for uri, expected in [(uri_a, CONTENT), (uri_b, b"first-b")]:
        target = tmp_path / "retrieved" / "checkpoint.json"
        assert storage.adapter.download(uri, str(target)) is None
        assert target.read_bytes() == expected
        assert list(target.parent.iterdir()) == [target]
    assert source.read_bytes() == CONTENT


def test_trailing_slash_and_nested_configured_prefix_are_resolved_once(storage, tmp_path):
    storage.adapter.cfg = SimpleNamespace(blob_uri=ROOT + "/nested/")
    source = tmp_path / "source.json"
    source.write_bytes(CONTENT)
    uri = storage.adapter.upload(str(source), KEY)
    assert uri == ROOT + "/nested/" + KEY
    target = tmp_path / "result.json"
    storage.adapter.download(uri, str(target))
    assert target.read_bytes() == CONTENT
    assert set(storage.blobs) == {"lab2/nested/" + KEY}


@pytest.mark.parametrize("key", [
    "", "/checkpoint.json", "../lab1/checkpoint.json", "a/../checkpoint.json",
    "a/./checkpoint.json", "a//checkpoint.json", "a/", "a\\checkpoint.json",
    "a/%2e%2e/checkpoint.json", "a/%252e%252e/checkpoint.json", "a%2fcheckpoint.json",
    "a/checkpoint.json?sig=not-a-real-token", "a/checkpoint.json#fragment",
    "https://other.example/checkpoint.json", "a/checkpoint name.json", "a/file\n.json",
])
def test_invalid_upload_key_stops_before_sdk_or_auth(storage, tmp_path, key):
    with pytest.raises(ValueError):
        storage.adapter.upload(str(tmp_path / "not-needed.json"), key)
    storage.factory.assert_not_called()
    storage.credentials.assert_not_called()
    assert storage.blobs == {}


@pytest.mark.parametrize("root", [
    "", ROOT.replace("https://", "http://"), ROOT.replace("https://", "abfss://"),
    ROOT.replace("example.blob", "example.evil"), ROOT.rsplit("/", 1)[0],
    ROOT + "?sig=not-a-real-token", ROOT + "#fragment", ROOT + "/../lab1",
    ROOT + "/%2e%2e/lab1", ROOT + "//", ROOT.replace("https://", "https://user:pass@"),
    ROOT.replace("https://", "https://\n"),
])
def test_invalid_configuration_is_rejected_for_both_operations(storage, tmp_path, root):
    storage.adapter.cfg = SimpleNamespace(blob_uri=root)
    with pytest.raises(ValueError, match="BLOB_URI"):
        storage.adapter.upload(str(tmp_path / "not-needed.json"), KEY)
    with pytest.raises(ValueError, match="BLOB_URI"):
        storage.adapter.download(URI, str(tmp_path / "not-created" / "checkpoint.json"))
    storage.factory.assert_not_called()
    storage.credentials.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("uri", [
    URI.replace("example.blob", "other.blob"), URI.replace("private-container", "other-container"),
    URI.replace("/lab2/", "/lab1/"), URI.replace("/lab2/", "/lab20/"),
    URI + "?sig=not-a-real-token", URI + "#fragment", ROOT, ROOT + "/",
    ROOT + "/../lab1/checkpoint.json", ROOT + "/%2e%2e/checkpoint.json",
    URI.replace("https://", "http://"),
])
def test_download_cannot_leave_configured_prefix_or_accept_credentials(storage, tmp_path, uri):
    with pytest.raises(ValueError):
        storage.adapter.download(uri, str(tmp_path / "not-created" / "checkpoint.json"))
    storage.factory.assert_not_called()
    storage.credentials.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_missing_upload_source_stops_before_authentication(storage, tmp_path):
    with pytest.raises(FileNotFoundError):
        storage.adapter.upload(str(tmp_path / "missing.json"), KEY)
    storage.credentials.assert_not_called()
    storage.factory.assert_not_called()


@pytest.mark.parametrize("operation", ["upload", "download"])
def test_permission_errors_propagate_without_claiming_success(storage, tmp_path, operation):
    target = tmp_path / "checkpoint.json"
    target.write_bytes(CONTENT)
    error = HttpResponseError(message="AuthorizationPermissionMismatch", status_code=403)
    method = getattr(storage.client, operation + "_blob")
    method.side_effect = error
    with pytest.raises(HttpResponseError) as caught:
        if operation == "upload":
            storage.adapter.upload(str(target), KEY)
        else:
            storage.adapter.download(URI, str(target))
    assert caught.value is error
    method.assert_called_once()  # No adapter-level retry or anonymous fallback.
    assert target.read_bytes() == CONTENT
    assert list(tmp_path.iterdir()) == [target]
    storage.client.__exit__.assert_called_once()
    storage.credential.__exit__.assert_called_once()


@pytest.mark.parametrize("existing", [False, True])
def test_missing_remote_blob_is_not_treated_as_an_empty_checkpoint(storage, tmp_path, existing):
    target = tmp_path / "checkpoint.json"
    if existing:
        target.write_bytes(CONTENT)
    with pytest.raises(ResourceNotFoundError):
        storage.adapter.download(URI, str(target))
    assert target.exists() == existing
    if existing:
        assert target.read_bytes() == CONTENT
    assert list(tmp_path.iterdir()) == ([target] if existing else [])


@pytest.mark.parametrize("existing", [False, True])
def test_interrupted_download_preserves_old_checkpoint_and_removes_partial_file(
    storage, tmp_path, existing,
):
    target = tmp_path / "checkpoint.json"
    if existing:
        target.write_bytes(CONTENT)
    error = ServiceRequestError("Connection dropped")
    stream = create_autospec(StorageStreamDownloader, instance=True)

    def partial_download(output):
        output.write(b'{"partial":')
        # The existing checkpoint is untouched while the transfer is in progress.
        assert target.exists() == existing
        if existing:
            assert target.read_bytes() == CONTENT
        raise error

    stream.readinto.side_effect = partial_download
    storage.client.download_blob.side_effect = None
    storage.client.download_blob.return_value = stream
    with pytest.raises(ServiceRequestError) as caught:
        storage.adapter.download(URI, str(target))
    assert caught.value is error
    assert target.exists() == existing
    if existing:
        assert target.read_bytes() == CONTENT
    assert list(tmp_path.iterdir()) == ([target] if existing else [])
    assert stream.readinto.call_args.args[0].closed
