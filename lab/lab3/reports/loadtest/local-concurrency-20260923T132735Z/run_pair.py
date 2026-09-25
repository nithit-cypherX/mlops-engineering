"""Run one original/serialized pair locally; no Azure or service-file mutations."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
LAB = ROOT.parents[2]
MODEL = Path(sys.argv[1]).resolve()
assert MODEL.parent.parent == Path("/tmp") and MODEL.parent.name.startswith("lab3-concurrency-")
assert hashlib.sha256((MODEL / "model.pkl").read_bytes()).hexdigest() == "ebe4f91523f5b872a5290effb8484b59398756fecd97f8fe069e293e5d4def61"
IMAGE = "itcs355-lab3:0606ecb"
K6 = "grafana/k6@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6"
LABEL = "codex.local-concurrency=" + ROOT.name
events = []
owned = []


def event(name, **data):
    item = {"event": name, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(), **data}
    events.append(item)
    print(json.dumps(item, allow_nan=False), flush=True)


def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(str(args[:3]) + ": " + result.stderr[-1500:])
    return result.stdout.strip()


def cleanup(name):
    found = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=10)
    if found.returncode:
        return
    info = json.loads(found.stdout)[0]
    assert info["Config"]["Labels"].get("codex.local-concurrency") == ROOT.name
    command(["docker", "stop", "--time", "3", name], timeout=12)
    command(["docker", "rm", name], timeout=12)


def inside(name, code, timeout=30):
    return json.loads(command(["docker", "exec", name, "python", "-c", code], timeout=timeout))


ready_code = """
import json,time,urllib.request,urllib.error
deadline=time.monotonic()+45
while time.monotonic()<deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=2) as r:
            body=json.load(r)
        if body.get('status')=='ready' and body.get('model_version')=='1':
            print(json.dumps(body)); break
    except (urllib.error.URLError,TimeoutError):
        pass
    time.sleep(.5)
else: raise TimeoutError('Local model did not become ready')
"""
warm_code = """
import json,urllib.request
body=b'{"temp_c":78.4,"vibration_mm_s":3.1,"pressure_kpa":315.2,"hours_since_service":4200,"load_pct":68,"ambient_humidity":55}'
assert len(body)==120
results=[]
for _ in range(5):
    request=urllib.request.Request('http://127.0.0.1:8080/predict',data=body,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=10) as r:
        item=json.load(r)
    assert item['model_version']=='1' and abs(item['probability']-0.00737357519563027)<=1e-9
    results.append(item)
print(json.dumps(results))
"""
state_code = "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8080/_diagnostic',timeout=5))))"
head = command(["git", "rev-parse", "HEAD"])
script_sha = hashlib.sha256((LAB / "loadtest/k6.js").read_bytes()).hexdigest()
error = None
try:
    assert command(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"]) == "sha256:e92a8d6ef68cf94d487f7b3803bba251c2f5843d9dc4d1974b479006a29cfb6c"
    for mode in ("original", "serialized"):
        result_dir = ROOT / (mode + "-measured")
        result_dir.mkdir()  # Never overwrite an earlier attempt.
        service = ROOT.name + "-" + mode
        client = service + "-k6"
        owned.extend([service, client])
        args = [
            "docker", "run", "-d", "--name", service, "--label", LABEL, "--pull=never",
            "--network", "none", "--cpus", "0.5", "--memory", "1g", "--pids-limit", "128",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,src={MODEL},dst=/model,readonly",
            "--mount", f"type=bind,src={ROOT},dst=/experiment,readonly",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "CLOUD_PROVIDER=azure",
            "-e", "MLFLOW_TRACKING_URI=file:///tmp/unused", "-e", "MODEL_REGISTRY_NAME=local-diagnostic",
            "-e", "MODEL_VERSION=1", "--entrypoint", "python", IMAGE, "/experiment/server.py", mode,
        ]
        ident = command(args)
        event("local_service_started", mode=mode, container=ident)
        inside(service, ready_code, timeout=55)
        warmups = inside(service, warm_code, timeout=55)
        before = inside(service, state_code)
        assert before["cpu_max"] == "50000 100000" and before["memory_max"] == "1073741824"
        assert before["effective_n_jobs"] == 1 and before["thread_tokens"] == 40
        assert before["load_calls"] == 1 and before["active"] == 0 and before["mismatches"] == 0
        cfg = json.loads(command(["docker", "inspect", service]))[0]
        assert cfg["HostConfig"]["NetworkMode"] == "none"
        assert cfg["HostConfig"]["NanoCpus"] == 500000000 and cfg["HostConfig"]["Memory"] == 1073741824
        event("local_ready", mode=mode, state=before, warmup_predictions=warmups,
              user=cfg["Config"]["User"], image_id=cfg["Image"])
        k6args = [
            "docker", "run", "--rm", "--name", client, "--label", LABEL, "--pull=never",
            "--network", "container:" + service, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
            "--mount", f"type=bind,src={LAB / 'loadtest'},dst=/work/loadtest,readonly",
            "--mount", f"type=bind,src={ROOT},dst=/experiment,readonly",
            "--mount", f"type=bind,src={result_dir},dst=/results",
            K6, "run", "--no-usage-report", "--include-system-env-vars=false",
            "--console-output", "/results/console.log", "--log-output", "file=/results/runtime.log",
            "-e", "TARGET=http://127.0.0.1:8080/predict", "-e", "VUS=10", "-e", "DURATION=60s",
            "-e", "RUN_STARTED_UTC=" + datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "-e", "GIT_SHA=" + head, "-e", "GIT_DIRTY=true", "-e", "SCRIPT_SHA256=" + script_sha,
            "-e", "K6_IMAGE=" + K6, "/experiment/k6-local.js",
        ]
        event("local_measurement_started", mode=mode, vus=10, duration="60s")
        result = subprocess.run(k6args, capture_output=True, text=True, timeout=100)
        assert result.returncode in (0, 99), result.stderr[-1500:]
        native = json.loads((result_dir / "summary.json").read_text())
        summary = native["lab3"]
        after = inside(service, state_code)
        assert after["calls"] - before["calls"] == summary["completed_responses"]
        assert after["load_calls"] == 1 and after["active"] == 0 and after["mismatches"] == 0
        assert summary["timing_evidence"]["complete"] and summary["error_count"] == summary["unfinished_requests"] == 0
        assert native["k6"]["metrics"]["prediction_mismatches"]["values"]["rate"] == 0
        assert after["max_active"] == 1 if mode == "serialized" else after["max_active"] > 1
        event("local_measurement_finished", mode=mode, exit_code=result.returncode,
              summary=summary, before=before, after=after,
              failed_thresholds={name:[key for key,value in metric.get("thresholds",{}).items() if not value["ok"]]
              for name,metric in native["k6"]["metrics"].items() if any(not value["ok"] for value in metric.get("thresholds",{}).values())})
        cleanup(service)
        event("local_service_removed", mode=mode)
except Exception as exc:
    error = type(exc).__name__ + ": " + str(exc)
    event("local_experiment_error", error=error)
finally:
    failures = []
    for name in reversed(owned):
        try:
            cleanup(name)
        except Exception as exc:
            failures.append(name + ": " + str(exc))
    remaining = command(["docker", "ps", "-a", "-q", "--filter", "label=" + LABEL])
    event("local_controller_finished", error=error, cleanup_errors=failures, remaining_containers=remaining.split())
    if error or failures or remaining:
        raise SystemExit(1)
