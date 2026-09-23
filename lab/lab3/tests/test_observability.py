"""Versioned API responses and actual JSON log output, without cloud calls."""
import io
import json
import math
import uuid
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from service import app as service

VALID = {
    "temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
    "hours_since_service": 4200.0, "load_pct": 68.0, "ambient_humidity": 55.0,
}


@pytest.fixture
def service_logs(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(service.log_handler, "stream", stream)
    return stream


@pytest.fixture
def client(monkeypatch, model_stub, wait_for_startup, service_logs):
    adapter = Mock()
    adapter.load_model.return_value = model_stub
    monkeypatch.setattr(service, "get_adapter", Mock(return_value=adapter))
    with TestClient(service.app, raise_server_exceptions=False) as test_client:
        wait_for_startup(test_client)
        yield test_client


def assert_response_and_log(response, stream, status, path, version="1"):
    assert response.status_code == status
    assert response.json()["model_version"] == version
    assert response.headers["x-model-version"] == version
    request_id = response.headers["x-request-id"]
    assert request_id
    # Parse the emitted lines, not just the unformatted logging records.
    entries = [json.loads(line) for line in stream.getvalue().splitlines()]
    matches = [entry for entry in entries if entry.get("request_id") == request_id]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["model_version"] == version
    assert entry["status"] == status
    assert entry["path"] == path
    assert isinstance(entry["latency_ms"], (int, float))
    assert math.isfinite(entry["latency_ms"]) and entry["latency_ms"] >= 0
    assert entry["ts"]
    assert entry["level"] == ("ERROR" if status >= 500 else "INFO")
    return entry


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/health", None),
    ("GET", "/ready", None),
    ("POST", "/predict", VALID),
    ("POST", "/predict/batch", {"rows": [VALID]}),
])
def test_success_responses_and_logs_share_version_and_request_id(
    client, service_logs, method, path, payload,
):
    request_id = 'lab3-"quoted"\\trace'
    response = client.request(method, path, json=payload, headers={"x-request-id": request_id})
    assert_response_and_log(response, service_logs, 200, path)
    assert response.headers["x-request-id"] == request_id


@pytest.mark.parametrize("path,body", [
    ("/predict", {key: value for key, value in VALID.items() if key != "temp_c"}),
    ("/predict", {**VALID, "load_pct": 250}),
    ("/predict", {**VALID, "temp_c": "not a number"}),
    ("/predict", {**VALID, "extra_field": 1}),
    ("/predict", {**VALID, "temp_c": float("nan")}),
    ("/predict/batch", {"rows": []}),
    ("/predict/batch", {"rows": [VALID] * 101}),
    ("/predict/batch", {"rows": [{**VALID, "pressure_kpa": -1}]}),
])
def test_invalid_input_keeps_useful_422_and_version(client, service_logs, path, body):
    response = client.post(path, content=json.dumps(body), headers={"content-type": "application/json"})
    assert_response_and_log(response, service_logs, 422, path)
    errors = response.json()["detail"]
    assert errors
    assert all(error["loc"] and error["msg"] and error["type"] for error in errors)


def test_malformed_json_is_versioned_422(client, service_logs):
    response = client.post("/predict", content='{"temp_c":', headers={"content-type": "application/json"})
    assert_response_and_log(response, service_logs, 422, "/predict")
    assert response.json()["detail"][0]["type"] == "json_invalid"


@pytest.mark.parametrize("path,status", [("/missing", 404), ("/predict", 405)])
def test_http_errors_keep_status_headers_and_version(client, service_logs, path, status):
    response = client.get(path)
    assert_response_and_log(response, service_logs, status, path)
    assert response.json()["detail"]
    if status == 405:
        assert "POST" in response.headers["allow"]


@pytest.mark.parametrize("path,payload", [
    ("/predict", VALID), ("/predict/batch", {"rows": [VALID]}),
])
def test_scoring_failure_is_logged_and_returns_versioned_500(
    client, model_stub, service_logs, path, payload,
):
    model_stub.predict_proba.side_effect = RuntimeError("private internal detail")
    response = client.post(path, json=payload)
    entry = assert_response_and_log(response, service_logs, 500, path)
    assert response.json()["detail"] == "Internal server error"
    assert entry["error_type"] == "RuntimeError"
    assert "private internal detail" not in response.text
    assert "private internal detail" not in service_logs.getvalue()


def test_load_failure_has_versioned_503_and_json_startup_log(
    offline_registry, wait_for_startup, service_logs,
):
    failure = 'registry "blocked"\ntry later'
    offline_registry.load.side_effect = PermissionError(failure)
    with TestClient(service.app) as client:
        wait_for_startup(client)
        assert_response_and_log(client.get("/health"), service_logs, 200, "/health")
        assert_response_and_log(client.get("/ready"), service_logs, 503, "/ready")
        for path, payload in [("/predict", VALID), ("/predict/batch", {"rows": [VALID]})]:
            assert_response_and_log(client.post(path, json=payload), service_logs, 503, path)

    entries = [json.loads(line) for line in service_logs.getvalue().splitlines()]
    startup = [entry for entry in entries if entry["message"].startswith("model load failed")]
    assert len(startup) == 1
    assert startup[0]["message"] == "model load failed: " + failure
    assert startup[0]["model_version"] == "1"


def test_missing_version_is_reported_as_unknown_not_invented(
    monkeypatch, wait_for_startup, service_logs,
):
    monkeypatch.delenv("MODEL_VERSION")
    with TestClient(service.app) as client:
        wait_for_startup(client)
        assert_response_and_log(client.get("/health"), service_logs, 200, "/health", "unknown")
        assert_response_and_log(client.get("/ready"), service_logs, 503, "/ready", "unknown")


@pytest.mark.parametrize("headers", [{}, {"x-request-id": ""}])
def test_request_id_is_generated_when_missing_or_empty(client, service_logs, headers):
    response = client.get("/health", headers=headers)
    assert_response_and_log(response, service_logs, 200, "/health")
    assert str(uuid.UUID(response.headers["x-request-id"])) == response.headers["x-request-id"]


def test_batch_of_100_is_still_accepted(client, service_logs):
    response = client.post("/predict/batch", json={"rows": [VALID] * 100})
    assert_response_and_log(response, service_logs, 200, "/predict/batch")
    assert response.json()["probabilities"] == pytest.approx([0.68] * 100)
