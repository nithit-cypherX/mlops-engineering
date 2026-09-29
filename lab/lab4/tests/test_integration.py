"""Lab 4 Task 1: start the built image, then check its real /predict response.

Run make serve-image followed by make test-integration with the same IMAGE/TAG.
Requires explicit serving config and host cloud login. No local model fallback,
source/cache mounts, training, deployment or automatic dependency installation.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager

import pytest

from cloudlayer.factory import get_adapter
from src import config

STARTUP_TIMEOUT = 180
# This only supplies test auth; exec keeps the image's original CMD and worker count.
BOOTSTRAP = (
    "import json, os, sys; payload=json.load(sys.stdin); "
    "os.environ.update(payload['environment']); command=payload['command']; "
    "os.execvp(command[0], command)"
)
SAMPLE = {
    "temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
    "hours_since_service": 4200, "load_pct": 68, "ambient_humidity": 55,
}


def _redact(line, environment):
    __tracebackhide__ = True
    for key, value in environment.items():
        if any(word in key for word in ("TOKEN", "SECRET", "PASSWORD")) and value:
            line = line.replace(value, "[REDACTED]")
    line = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
                  "[REDACTED_JWT]", line)
    return re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED_QUERY]", line)


@contextmanager
def _running_container(image_id, command, environment):
    __tracebackhide__ = True
    name = "lab4-integration-" + uuid.uuid4().hex
    payload = json.dumps({"environment": environment, "command": command})
    if len(payload.encode()) > 16384:
        raise RuntimeError("Test bootstrap input is unexpectedly large")
    logs = deque(maxlen=100)
    process = subprocess.Popen(
        ["docker", "run", "--rm", "--pull=never", "--name", name, "-i",
         "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
         "--cap-drop=ALL", "--security-opt=no-new-privileges", "--log-driver=none",
         "-p", "127.0.0.1::8080", "--entrypoint", "python", image_id,
         "-u", "-c", BOOTSTRAP],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )

    def collect_logs():
        __tracebackhide__ = True
        for line in process.stdout:
            logs.append(_redact(line, environment))

    reader = threading.Thread(target=collect_logs, daemon=True)
    reader.start()
    try:
        process.stdin.write(payload)
        process.stdin.close()
        yield name, process
    except BaseException:
        print("Container output (redacted, last 100 lines):\n" + "".join(list(logs)))
        raise
    finally:
        try:
            removed = subprocess.run(["docker", "rm", "--force", name],
                                     capture_output=True, text=True, timeout=20)
            if removed.returncode and "No such container" not in removed.stderr:
                raise RuntimeError(f"Could not remove owned test container {name}")
        finally:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()


@contextmanager
def _ready_connection(name, process):
    connection = None
    deadline = time.monotonic() + STARTUP_TIMEOUT
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Container exited before becoming ready")
            if connection is None:
                port = subprocess.run(["docker", "port", name, "8080/tcp"],
                                      capture_output=True, text=True, timeout=5)
                if port.returncode:
                    time.sleep(1)
                    continue
                host, number = port.stdout.strip().split(":")
                assert host == "127.0.0.1", "Test must bind only to localhost"
                connection = http.client.HTTPConnection(host, int(number), timeout=5)
            try:
                connection.request("GET", "/ready")
                response = connection.getresponse()
                body = response.read()
                if response.status == 200:
                    assert json.loads(body)["status"] == "ready"
                    break
                assert response.status == 503, f"Unexpected readiness HTTP {response.status}"
            except (OSError, http.client.HTTPException):
                connection.close()
                connection = None
            time.sleep(1)
        else:
            raise AssertionError(f"Container did not become ready within {STARTUP_TIMEOUT}s")
        # Reuse this socket: another worker may still be starting up.
        # Yield outside the retry handler: never retry errors from the actual test.
        yield connection
    finally:
        if connection is not None:
            connection.close()


@pytest.fixture
def serving_container():
    __tracebackhide__ = True
    cfg = config.load_serving()
    image = os.environ.get("INTEGRATION_IMAGE")
    if not image:
        pytest.fail("Set INTEGRATION_IMAGE to the built image; see make test-integration.",
                    pytrace=False)
    inspected = subprocess.run(["docker", "image", "inspect", image], check=True,
                               capture_output=True, text=True, timeout=15)
    metadata = json.loads(inspected.stdout)[0]
    assert (metadata["Os"], metadata["Architecture"]) == ("linux", "amd64")
    assert metadata["Config"]["User"].split(":")[0] not in ("", "root", "0")
    assert not metadata["Config"].get("Entrypoint"), "Bootstrap expects no image ENTRYPOINT"
    command = metadata["Config"]["Cmd"]
    assert command and all(isinstance(arg, str) for arg in command), "Image needs a CMD"
    environment = get_adapter(cfg).integration_environment()
    print(f"image_id={metadata['Id']}; model_version={cfg.model_version}")
    with _running_container(metadata["Id"], command, environment) as (name, process):
        with _ready_connection(name, process) as connection:
            yield connection, cfg.model_version


def _assert_prediction(body, expected_version):
    assert isinstance(body, dict), "Expected a JSON object"
    assert set(body) == {"probability", "model_version"}, "Prediction response shape changed"
    probability = body["probability"]
    assert type(probability) in (int, float), "Probability must be numeric, not bool/string"
    assert math.isfinite(probability) and 0 <= probability <= 1, "Invalid probability"
    assert body["model_version"] == expected_version, "Wrong model version in response"


def test_built_container_predicts_registered_version(serving_container):
    connection, expected_version = serving_container
    connection.request("POST", "/predict", body=json.dumps(SAMPLE),
                       headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    body = response.read()
    assert response.status == 200, f"Prediction returned HTTP {response.status}"
    assert response.getheader("Content-Type", "").split(";")[0] == "application/json"
    assert response.getheader("x-model-version") == expected_version
    _assert_prediction(json.loads(body), expected_version)
