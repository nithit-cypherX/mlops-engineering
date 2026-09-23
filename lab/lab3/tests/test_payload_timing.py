"""Offline timing boundaries and API compatibility, not performance benchmarks."""
import asyncio
import json
import math
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from service import app as service

VALID = {
    "temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
    "hours_since_service": 4200, "load_pct": 68, "ambient_humidity": 55,
}
BASE = json.dumps(VALID, separators=(",", ":")).encode("ascii")


@pytest.fixture
def client(monkeypatch, model_stub, wait_for_startup):
    adapter = Mock()
    adapter.load_model.return_value = model_stub
    monkeypatch.setattr(service, "get_adapter", Mock(return_value=adapter))
    with TestClient(service.app, raise_server_exceptions=False) as test_client:
        wait_for_startup(test_client)
        model_stub.predict_proba.reset_mock()
        yield test_client


def timing_values(response):
    parts = response.headers["server-timing"].split(", ")
    assert len(parts) == 3
    values = dict(part.split(";dur=") for part in parts)
    assert set(values) == {"json_decode", "scoring", "processing"}
    values = {key: float(value) for key, value in values.items()}
    assert all(math.isfinite(value) and value >= 0 for value in values.values())
    return values


@pytest.mark.parametrize("size", [120, 10240, 102400, 1048576])
def test_sizes_keep_predictions_and_return_separate_timings(client, model_stub, size):
    assert len(BASE) == 120
    body = BASE[:-1] + b" " * (size - len(BASE)) + b"}"
    response = client.post("/predict", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert response.json() == {"probability": 0.68, "model_version": "1"}
    assert response.headers["x-model-version"] == "1"
    assert response.headers["x-request-id"]
    values = timing_values(response)
    # Component timers are contained in processing; allow header rounding only.
    assert values["processing"] + 0.000002 >= values["json_decode"] + values["scoring"]
    model_stub.predict_proba.assert_called_once()
    frame = model_stub.predict_proba.call_args.args[0]
    assert frame.to_dict("records") == [VALID]


@pytest.mark.parametrize("body,content_type", [
    (json.dumps({**VALID, "padding": "x"}).encode(), "application/json"),
    (json.dumps({**VALID, "load_pct": 250}).encode(), "application/json"),
    (json.dumps({k: v for k, v in VALID.items() if k != "temp_c"}).encode(), "application/json"),
    (b'{"temp_c":', "application/json"),
    (b"", "application/json"),
    (BASE, "application/octet-stream"),
])
def test_invalid_requests_stay_422_without_success_timings(client, model_stub, body, content_type):
    response = client.post("/predict", content=body, headers={"content-type": content_type})
    assert response.status_code == 422
    assert response.json()["model_version"] == "1"
    assert response.json()["detail"]
    assert "server-timing" not in response.headers
    model_stub.predict_proba.assert_not_called()


def test_scoring_failure_then_success_does_not_reuse_timings(client, model_stub):
    normal = model_stub.predict_proba.side_effect
    model_stub.predict_proba.side_effect = RuntimeError("private model error")
    failed = client.post("/predict", json=VALID)
    assert failed.status_code == 500
    assert failed.json() == {"detail": "Internal server error", "model_version": "1"}
    assert "server-timing" not in failed.headers
    model_stub.predict_proba.side_effect = normal
    good = client.post("/predict", json=VALID)
    assert good.status_code == 200
    timing_values(good)


def test_unavailable_model_stays_503_without_success_timings(client):
    service.STATE["model"] = None
    response = client.post("/predict", json=VALID)
    assert response.status_code == 503
    assert response.json() == {"detail": "model not loaded", "model_version": "1"}
    assert "server-timing" not in response.headers


@pytest.mark.parametrize("method,path,body,status", [
    ("GET", "/health", None, 200),
    ("GET", "/ready", None, 200),
    ("POST", "/predict/batch", {"rows": [VALID]}, 200),
    ("GET", "/predict", None, 405),
])
def test_other_routes_do_not_get_prediction_timing(client, method, path, body, status):
    response = client.request(method, path, json=body)
    assert response.status_code == status
    assert "server-timing" not in response.headers


def test_json_cache_is_not_parsed_or_timed_twice(monkeypatch):
    clock = SimpleNamespace(ns=0)
    monkeypatch.setattr(service.time, "perf_counter_ns", lambda: clock.ns)
    original_loads = json.loads

    def decode(raw, *args, **kwargs):
        clock.ns += 2_000_000
        return original_loads(raw, *args, **kwargs)

    decoder = Mock(side_effect=decode)
    monkeypatch.setattr(json, "loads", decoder)
    receives = []

    async def receive():
        receives.append(True)
        clock.ns += 100_000_000  # Simulated upload; no real sleep.
        return {"type": "http.request", "body": BASE, "more_body": False}

    async def exercise():
        request = service.TimedPredictionRequest({"type": "http", "headers": []}, receive)
        request.state.prediction_timings = {}
        first = await request.json()
        assert request._processing_started_ns == 100_000_000
        assert request.state.prediction_timings == {"json_decode_ms": 2}
        clock.ns += 50_000_000
        assert await request.json() is first
        assert await request.body() == BASE
        assert request.state.prediction_timings == {"json_decode_ms": 2}
        assert request._processing_started_ns == 100_000_000

    asyncio.run(exercise())
    assert len(receives) == 1
    decoder.assert_called_once_with(BASE)


def test_header_units_and_processing_exclude_body_receipt(client, monkeypatch):
    clock = SimpleNamespace(ns=0)
    monkeypatch.setattr(service.time, "perf_counter_ns", lambda: clock.ns)
    original_body, original_loads = Request.body, json.loads
    original_score, original_response_init = service._score, service.PredictResponse.__init__

    async def delayed_body(request):
        cached = hasattr(request, "_body")
        raw = await original_body(request)
        if not cached:
            clock.ns += 100_000_000
        return raw

    def decode(raw, *args, **kwargs):
        result = original_loads(raw, *args, **kwargs)
        if raw == BASE:
            clock.ns += 2_000_000
        return result

    def score(rows):
        result = original_score(rows)
        clock.ns += 3_000_000
        return result

    def response_init(self, **kwargs):
        original_response_init(self, **kwargs)
        clock.ns += 5_000_000

    monkeypatch.setattr(Request, "body", delayed_body)
    monkeypatch.setattr(json, "loads", decode)
    monkeypatch.setattr(service, "_score", score)
    monkeypatch.setattr(service.PredictResponse, "__init__", response_init)
    response = client.post("/predict", content=BASE, headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert clock.ns == 110_000_000
    assert timing_values(response) == {"json_decode": 2, "scoring": 3, "processing": 10}


def test_concurrent_requests_keep_their_own_timing_state(client, model_stub, monkeypatch):
    barrier = Barrier(2)
    original_json = service.TimedPredictionRequest.json
    original_score = model_stub.predict_proba.side_effect
    states = []

    async def tag_request(request):
        result = await original_json(request)
        states.append(request.state.prediction_timings)
        # Distinct test markers let us detect state mixing without timing/sleep guesses.
        request.state.prediction_timings["json_decode_ms"] = result["load_pct"]
        return result

    def score_together(frame):
        barrier.wait(timeout=5)
        return original_score(frame)

    monkeypatch.setattr(service.TimedPredictionRequest, "json", tag_request)
    model_stub.predict_proba.side_effect = score_together
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.post, "/predict", json={**VALID, "load_pct": value})
            for value in (20, 80)
        ]
        responses = [future.result(timeout=10) for future in futures]
    assert len(states) == 2 and states[0] is not states[1]
    for response, value in zip(responses, (20, 80)):
        assert response.status_code == 200
        assert response.json()["probability"] == value / 100
        assert timing_values(response)["json_decode"] == value
    assert responses[0].headers["x-request-id"] != responses[1].headers["x-request-id"]
