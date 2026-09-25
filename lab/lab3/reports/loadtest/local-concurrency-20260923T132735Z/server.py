"""One-off local diagnostic harness, not a serving implementation change."""
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import threading

import anyio
import joblib
import mlflow.sklearn
import uvicorn

from service import app as api

MODE = sys.argv[1]
assert MODE in ("original", "serialized")
EXPECTED = 0.00737357519563027
MODEL = mlflow.sklearn.load_model("/model")
assert MODEL.n_estimators == 100 and MODEL.n_jobs == -1
original_score = api._score_model
serial_gate = threading.Lock()
stats_lock = threading.Lock()
stats = {"active": 0, "max_active": 0, "calls": 0, "mismatches": 0, "load_calls": 0}


def load_local_model(cfg):
    with stats_lock:
        stats["load_calls"] += 1
    return MODEL


def measured_score(model, rows):
    # Both modes keep the existing route and thread pool. Only this gate differs.
    with serial_gate if MODE == "serialized" else nullcontext():
        with stats_lock:
            stats["active"] += 1
            stats["max_active"] = max(stats["max_active"], stats["active"])
        try:
            result = original_score(model, rows)
            with stats_lock:
                stats["mismatches"] += sum(abs(score - EXPECTED) > 1e-9 for score in result)
            return result
        finally:
            with stats_lock:
                stats["calls"] += 1
                stats["active"] -= 1


api._load_model = load_local_model
api._score_model = measured_score


@api.app.get("/_diagnostic")
async def diagnostic():
    with stats_lock:
        current = dict(stats)
    current.update(
        mode=MODE,
        model_version=str(api.STATE["version"]),
        n_estimators=MODEL.n_estimators,
        model_n_jobs=MODEL.n_jobs,
        effective_n_jobs=joblib.effective_n_jobs(MODEL.n_jobs),
        thread_tokens=anyio.to_thread.current_default_thread_limiter().total_tokens,
        cpu_max=Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        memory_max=Path("/sys/fs/cgroup/memory.max").read_text().strip(),
        cpu_stat=dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines()),
    )
    return current


uvicorn.run(api.app, host="127.0.0.1", port=8080, workers=1)
