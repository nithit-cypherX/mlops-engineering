"""Offline test boundaries: no registry requests, credentials or training."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import mlflow
import mlflow.sklearn
import numpy as np
import pytest

from service import app as service
from src import config, data


@pytest.fixture
def model_stub():
    def predict_proba(frame):
        assert list(frame.columns) == data.FEATURES
        probabilities = frame["load_pct"].to_numpy(dtype=float) / 100
        return np.column_stack((1 - probabilities, probabilities))

    return SimpleNamespace(predict_proba=Mock(side_effect=predict_proba))


@pytest.fixture
def wait_for_startup():
    async def wait():
        await asyncio.wait_for(asyncio.shield(service.app.state.model_loading_task), timeout=5)

    return lambda client: client.portal.call(wait)


@pytest.fixture(autouse=True)
def offline_registry(monkeypatch, caplog):
    for key in (
        *config.CAPABILITY_SLOTS, *config.AZURE_WORKSPACE_SLOTS,
        "MODEL_VERSION", "MODEL_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "CLOUD_PROVIDER": "azure",
        "MLFLOW_TRACKING_URI": "azureml://unit-test-workspace",
        "MODEL_REGISTRY_NAME": "test-model",
        "MODEL_VERSION": "1",
    }.items():
        monkeypatch.setenv(key, value)

    mocks = SimpleNamespace(
        tracking=Mock(),
        registry=Mock(),
        load=Mock(side_effect=AssertionError("Unexpected registry load in offline test")),
    )
    monkeypatch.setattr(mlflow, "set_tracking_uri", mocks.tracking)
    monkeypatch.setattr(mlflow, "set_registry_uri", mocks.registry)
    monkeypatch.setattr(mlflow.sklearn, "load_model", mocks.load)
    monkeypatch.setattr(service, "STATE", {"model": None, "version": "unknown"})
    # The service owns its JSON handler rather than propagating to the root logger.
    service.log.addHandler(caplog.handler)
    try:
        yield mocks
    finally:
        service.log.removeHandler(caplog.handler)
