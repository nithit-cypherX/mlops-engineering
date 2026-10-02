"""Offline checks for validated feature values in the existing request JSON log."""
import io
import json
import math
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import Barrier
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from service import app as service

VALID = {
    "temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
    "hours_since_service": 4200.0, "load_pct": 68.0, "ambient_humidity": 55.0,
}


@pytest.fixture
def offline_service(monkeypatch):
    def predict_proba(frame):
        values = frame["load_pct"].to_numpy(dtype=float) / 100
        return np.column_stack((1 - values, values))

    model = Mock()
    model.predict_proba.side_effect = predict_proba
    stream = io.StringIO()
    monkeypatch.setattr(service, "STATE", {"model": model, "version": "1"})
    monkeypatch.setattr(service.log_handler, "stream", stream)

    @asynccontextmanager
    async def offline_lifespan(_app):
        # Exercise the real routes and middleware, without registry startup.
        yield

    monkeypatch.setattr(service.app.router, "lifespan_context", offline_lifespan)
    with TestClient(service.app, raise_server_exceptions=False) as client:
        yield client, model, stream


def request_log(response, stream, path, status):
    assert response.status_code == status
    assert response.json()["model_version"] == "1"
    assert response.headers["x-model-version"] == "1"
    entries = [json.loads(line) for line in stream.getvalue().splitlines()]
    matches = [
        entry for entry in entries
        if entry.get("request_id") == response.headers["x-request-id"]
    ]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["message"] == "request completed"
    assert entry["status"] == status and entry["path"] == path
    assert entry["model_version"] == "1"
    assert math.isfinite(entry["latency_ms"]) and entry["latency_ms"] >= 0
    assert set(entry) <= {
        "ts", "level", "message", "request_id", "path", "status", "latency_ms",
        "model_version", "error_type", "load_pct_values",
    }
    return entry


@pytest.mark.parametrize("path,values", [
    ("/predict", [68.0]),
    ("/predict/batch", [0.0, 100.0]),
    ("/predict/batch", [float(value) for value in range(100)]),
])
def test_validated_values_are_logged_without_changing_prediction(offline_service, path, values):
    client, model, stream = offline_service
    rows = [{**VALID, "load_pct": value} for value in values]
    body = rows[0] if path == "/predict" else {"rows": rows}
    response = client.post(path, json=body)
    entry = request_log(response, stream, path, 200)

    assert entry["load_pct_values"] == values
    scores = [value / 100 for value in values]
    expected = {"probability": scores[0]} if path == "/predict" else {"probabilities": scores}
    assert response.json() == {**expected, "model_version": "1"}
    assert ("server-timing" in response.headers) == (path == "/predict")
    model.predict_proba.assert_called_once()
    assert model.predict_proba.call_args.args[0].to_dict("records") == rows
    # Only the selected feature is logged, not the full body or other features.
    assert "temp_c" not in stream.getvalue()
    assert "pressure_kpa" not in stream.getvalue()


@pytest.mark.parametrize("path,body", [
    ("/predict", {**VALID, "load_pct": -1}),
    ("/predict", {**VALID, "load_pct": 250}),
    ("/predict", {**VALID, "load_pct": float("nan")}),
    ("/predict", {**VALID, "load_pct": float("inf")}),
    ("/predict", {key: value for key, value in VALID.items() if key != "load_pct"}),
    ("/predict", {**VALID, "temp_c": "not a number"}),
    ("/predict", {**VALID, "load_pct_values": [99]}),
    ("/predict/batch", {"rows": [VALID, {**VALID, "load_pct": 250}]}),
    ("/predict/batch", {"rows": []}),
    ("/predict/batch", {"rows": [VALID] * 101}),
])
def test_rejected_input_does_not_add_feature_values(offline_service, path, body):
    client, model, stream = offline_service
    response = client.post(
        path, content=json.dumps(body), headers={"content-type": "application/json"},
    )
    entry = request_log(response, stream, path, 422)
    assert "load_pct_values" not in entry
    model.predict_proba.assert_not_called()


def test_malformed_json_does_not_add_feature_values(offline_service):
    client, model, stream = offline_service
    response = client.post(
        "/predict", content='{"load_pct":', headers={"content-type": "application/json"},
    )
    assert "load_pct_values" not in request_log(response, stream, "/predict", 422)
    model.predict_proba.assert_not_called()


@pytest.mark.parametrize("path", ["/predict", "/predict/batch"])
@pytest.mark.parametrize("status", [500, 503])
def test_validated_inputs_remain_visible_when_scoring_fails(offline_service, path, status):
    client, model, stream = offline_service
    if status == 500:
        model.predict_proba.side_effect = RuntimeError("private model detail")
    else:
        service.STATE["model"] = None
    body = VALID if path == "/predict" else {"rows": [VALID]}
    response = client.post(path, json=body)
    entry = request_log(response, stream, path, status)
    # These are valid incoming inputs, not a claim that prediction succeeded.
    assert entry["load_pct_values"] == [68.0]
    detail = "Internal server error" if status == 500 else "model not loaded"
    assert response.json() == {"detail": detail, "model_version": "1"}
    assert "server-timing" not in response.headers
    assert "private model detail" not in response.text + stream.getvalue()


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_feature_values_do_not_leak_into_later_requests(offline_service, path):
    client, model, stream = offline_service
    good = client.post("/predict", json=VALID)
    assert request_log(good, stream, "/predict", 200)["load_pct_values"] == [68.0]
    probe = client.get(path)
    assert "load_pct_values" not in request_log(probe, stream, path, 200)
    bad = client.post("/predict", json={**VALID, "load_pct": 250})
    assert "load_pct_values" not in request_log(bad, stream, "/predict", 422)
    model.predict_proba.assert_called_once()


def test_concurrent_requests_keep_their_own_feature_values(offline_service):
    client, model, stream = offline_service
    barrier = Barrier(2)
    normal_score = model.predict_proba.side_effect

    def score_together(frame):
        barrier.wait(timeout=5)
        return normal_score(frame)

    model.predict_proba.side_effect = score_together
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.post, "/predict", json={**VALID, "load_pct": value})
            for value in (20.0, 80.0)
        ]
        responses = [future.result(timeout=10) for future in futures]
    for value, response in zip((20.0, 80.0), responses):
        assert request_log(response, stream, "/predict", 200)["load_pct_values"] == [value]
        assert response.json() == {"probability": value / 100, "model_version": "1"}
