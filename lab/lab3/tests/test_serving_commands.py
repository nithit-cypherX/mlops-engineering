"""Task 2 command wiring and smoke assertions, with an offline adapter."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import serving


def test_smoke_invokes_three_distinct_valid_payloads():
    adapter = Mock()
    adapter.invoke.side_effect = [
        {"probability": score, "model_version": "1"} for score in (0.1, 0.2, 0.3)
    ]
    results = serving.smoke(adapter, "test-endpoint", "1")
    assert [row["probability"] for row in results] == [0.1, 0.2, 0.3]
    assert [call.args for call in adapter.invoke.call_args_list] == [
        ("test-endpoint", payload) for payload in serving.SMOKE_PAYLOADS
    ]
    assert len({json.dumps(payload, sort_keys=True) for payload in serving.SMOKE_PAYLOADS}) == 3


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5", None])
def test_smoke_rejects_bad_probability_and_stops(score):
    adapter = Mock()
    adapter.invoke.return_value = {"probability": score, "model_version": "1"}
    with pytest.raises(RuntimeError, match="probability"):
        serving.smoke(adapter, "test-endpoint", "1")
    assert adapter.invoke.call_count == 1


@pytest.mark.parametrize("version", ["2", "unknown", 1, None])
def test_smoke_rejects_wrong_version(version):
    adapter = Mock()
    adapter.invoke.return_value = {"probability": 0.2, "model_version": version}
    with pytest.raises(RuntimeError, match="model_version"):
        serving.smoke(adapter, "test-endpoint", "1")


@pytest.mark.parametrize("command", ["deploy", "smoke"])
def test_cli_uses_adapter_and_prints_result(monkeypatch, capsys, command):
    cfg = SimpleNamespace(model_registry_name="test-model", model_version="1",
                          endpoint_name="test-endpoint", serving_instance="test-size")
    loader = Mock(return_value=cfg)
    monkeypatch.setattr(serving.config, "load_deployment" if command == "deploy" else "load_serving", loader)
    adapter = Mock()
    adapter.deploy.return_value = "https://test-endpoint.example"
    adapter.invoke.return_value = {"probability": 0.25, "model_version": "1"}
    factory = Mock(return_value=adapter)
    monkeypatch.setattr(serving, "get_adapter", factory)
    assert serving.main([command]) == 0
    loader.assert_called_once_with()
    factory.assert_called_once_with(cfg)
    output = json.loads(capsys.readouterr().out)
    if command == "deploy":
        adapter.deploy.assert_called_once_with("models:/test-model/1", "test-endpoint", "test-size")
        adapter.invoke.assert_not_called()
        assert output["endpoint"] == "https://test-endpoint.example"
    else:
        adapter.deploy.assert_not_called()
        assert output["smoke"] == "PASS" and len(output["results"]) == 3


def test_cli_missing_endpoint_stops_before_adapter(monkeypatch):
    monkeypatch.setattr(serving.config, "load_serving", Mock(return_value=SimpleNamespace(endpoint_name="")))
    factory = Mock()
    monkeypatch.setattr(serving, "get_adapter", factory)
    with pytest.raises(RuntimeError, match="ENDPOINT_NAME"):
        serving.main(["smoke"])
    factory.assert_not_called()


def test_failed_smoke_never_prints_pass(monkeypatch, capsys):
    cfg = SimpleNamespace(model_version="1", endpoint_name="test-endpoint")
    monkeypatch.setattr(serving.config, "load_serving", Mock(return_value=cfg))
    adapter = Mock()
    adapter.invoke.side_effect = RuntimeError("HTTP 503")
    monkeypatch.setattr(serving, "get_adapter", Mock(return_value=adapter))
    with pytest.raises(RuntimeError, match="503"):
        serving.main(["smoke"])
    assert capsys.readouterr().out == ""
