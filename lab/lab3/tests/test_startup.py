"""Offline lifecycle checks: loading, score readiness and one load per startup."""
import asyncio
from threading import Event
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from service import app as service

VALID = {
    "temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
    "hours_since_service": 4200.0, "load_pct": 68.0, "ambient_humidity": 55.0,
}


@pytest.mark.parametrize("blocked_step", ["load", "score"])
def test_health_works_but_predictions_wait_for_readiness(
    monkeypatch, model_stub, wait_for_startup, blocked_step,
):
    started, release = Event(), Event()
    adapter = Mock()
    adapter.load_model.return_value = model_stub

    def block():
        started.set()
        assert release.wait(5), "Test did not release the startup gate"

    if blocked_step == "load":
        def load(*args):
            block()
            return model_stub

        adapter.load_model.side_effect = load
    else:
        original_score = model_stub.predict_proba.side_effect

        def score(frame):
            block()
            return original_score(frame)

        model_stub.predict_proba.side_effect = score

    monkeypatch.setattr(service, "get_adapter", Mock(return_value=adapter))
    with TestClient(service.app) as client:
        try:
            assert started.wait(5)
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 503
            assert client.post("/predict", json=VALID).status_code == 503
            assert client.post("/predict/batch", json={"rows": [VALID]}).status_code == 503
            assert service.STATE["model"] is None
        finally:
            release.set()
            wait_for_startup(client)

        assert client.get("/ready").status_code == 200
        assert service.STATE["model"] is model_stub
        adapter.load_model.assert_called_once_with("test-model", "1")
        model_stub.predict_proba.assert_called_once()

    assert service.STATE["model"] is None


@pytest.mark.parametrize("failure", [
    "exception", "nan", "infinity", "out_of_range", "wrong_row_count", "wrong_shape",
])
def test_loaded_but_unscorable_model_stays_unready(
    monkeypatch, model_stub, wait_for_startup, caplog, failure,
):
    outputs = {
        "nan": np.array([[0.5, np.nan]]),
        "infinity": np.array([[0.5, np.inf]]),
        "out_of_range": np.array([[-0.2, 1.2]]),
        "wrong_row_count": np.array([[0.5, 0.5], [0.5, 0.5]]),
        "wrong_shape": np.array([0.5, 0.5]),
    }
    model_stub.predict_proba.side_effect = (
        RuntimeError("score failed") if failure == "exception" else None
    )
    if failure != "exception":
        model_stub.predict_proba.return_value = outputs[failure]
    adapter = Mock()
    adapter.load_model.return_value = model_stub
    monkeypatch.setattr(service, "get_adapter", Mock(return_value=adapter))

    with TestClient(service.app) as client:
        wait_for_startup(client)
        for _ in range(2):
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 503
            assert client.post("/predict", json=VALID).status_code == 503
            assert client.post("/predict/batch", json={"rows": [VALID]}).status_code == 503
        assert service.STATE["model"] is None

    assert "model load failed" in caplog.text
    adapter.load_model.assert_called_once_with("test-model", "1")
    model_stub.predict_proba.assert_called_once()


def test_repeated_requests_do_not_reload_or_repeat_startup_probe(
    monkeypatch, model_stub, wait_for_startup,
):
    adapter = Mock()
    adapter.load_model.return_value = model_stub
    monkeypatch.setattr(service, "get_adapter", Mock(return_value=adapter))

    with TestClient(service.app) as client:
        wait_for_startup(client)
        model_stub.predict_proba.assert_called_once()
        for _ in range(3):
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 200
        model_stub.predict_proba.assert_called_once()

        for _ in range(3):
            response = client.post("/predict", json=VALID)
            assert response.status_code == 200
            assert response.json()["probability"] == pytest.approx(0.68)
            batch = client.post("/predict/batch", json={"rows": [VALID, VALID]})
            assert batch.status_code == 200
            assert batch.json()["probabilities"] == pytest.approx([0.68, 0.68])
        assert service.STATE["version"] == "1"

    adapter.load_model.assert_called_once_with("test-model", "1")
    assert model_stub.predict_proba.call_count == 7  # one probe + six prediction requests


def test_shutdown_during_load_cannot_publish_a_late_model(monkeypatch, model_stub):
    started, release, finished = Event(), Event(), Event()

    def load(*args):
        started.set()
        try:
            assert release.wait(5), "Test did not release the startup gate"
            return model_stub
        finally:
            finished.set()

    adapter = Mock()
    adapter.load_model.side_effect = load
    monkeypatch.setattr(service, "get_adapter", Mock(return_value=adapter))

    async def exercise():
        try:
            async with service.lifespan(service.app):
                assert await asyncio.to_thread(started.wait, 5)
                loading_task = service.app.state.model_loading_task
            assert loading_task.cancelled()
            assert service.STATE["model"] is None
        finally:
            release.set()

    # asyncio.run also waits for its worker thread to finish before returning.
    asyncio.run(exercise())
    assert finished.is_set()
    assert service.STATE["model"] is None
    adapter.load_model.assert_called_once_with("test-model", "1")
    model_stub.predict_proba.assert_not_called()
