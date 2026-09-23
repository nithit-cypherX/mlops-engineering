"""Lab 3 — inference service.

Provider-neutral by construction: the model arrives through the adapter, and the same
container image deploys to SageMaker, Azure ML, or Vertex AI. Route paths differ per
platform; that difference belongs in cloudlayer/, never here.

Run locally:  uvicorn service.app:app --port 8080
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloudlayer.factory import get_adapter
from service.schemas import BatchRequest, BatchResponse, PredictRequest, PredictResponse, StatusResponse
from src import config


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
            **getattr(record, "fields", {}),
        }, allow_nan=False)


# Configure only our service logger; leave library/server loggers untouched.
log_handler = logging.StreamHandler()
log_handler.setFormatter(JsonFormatter())
log = logging.getLogger("service")
log.setLevel(logging.INFO)
log.addHandler(log_handler)
log.propagate = False

STATE: dict[str, Any] = {"model": None, "version": "unknown"}


def _load_model(cfg: config.Config):
    """Load once, at startup. Never per request.

    Loading per request is the commonest cause of a p99 that looks nothing like p50, and
    it is the first thing to check when your latency distribution has a long tail.
    """
    return get_adapter(cfg).load_model(cfg.model_registry_name, cfg.model_version)


async def _initialize_model():
    try:
        cfg = config.load_serving()
        STATE["version"] = cfg.model_version
        model = await asyncio.to_thread(_load_model, cfg)
        # One valid sensor row checks serving compatibility, not model accuracy.
        probe = PredictRequest(
            temp_c=78.4, vibration_mm_s=3.1, pressure_kpa=315.2,
            hours_since_service=4200.0, load_pct=68.0, ambient_humidity=55.0,
        )
        scores = await asyncio.to_thread(_score_model, model, [probe.model_dump()])
        if len(scores) != 1 or not math.isfinite(scores[0]) or not 0 <= scores[0] <= 1:
            raise ValueError("model readiness check did not return one valid probability")
        # Publish only after the score check passes. Worker threads never update STATE.
        STATE["model"] = model
        log.info("model loaded", extra={"fields": {"model_version": str(STATE["version"])}})
    except Exception as exc:  # readiness stays false; liveness still passes
        STATE["model"] = None
        log.error("model load failed: %s", exc,
                  extra={"fields": {"model_version": str(STATE["version"])}})


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE.update(model=None, version="unknown")
    loading_task = asyncio.create_task(_initialize_model())
    app.state.model_loading_task = loading_task
    try:
        yield
    finally:
        # An in-flight download may finish, but must not publish after shutdown.
        loading_task.cancel()
        with suppress(asyncio.CancelledError):
            await loading_task
        STATE["model"] = None


app = FastAPI(title="ITCS355 inference", version="1.0.0", lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "model_version": str(STATE["version"])},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Keep useful field errors without echoing raw input (which may include NaN).
    errors = [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"detail": errors, "model_version": str(STATE["version"])},
    )


@app.middleware("http")
async def add_request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    started = time.perf_counter()
    fields = {"request_id": request_id, "path": request.url.path}
    try:
        response = await call_next(request)
    except Exception as exc:
        # Return a versioned 500, not the exception text or a misleading success.
        fields["error_type"] = type(exc).__name__
        response = JSONResponse(status_code=500, content={
            "detail": "Internal server error", "model_version": str(STATE["version"]),
        })
    latency_ms = (time.perf_counter() - started) * 1000
    response.headers["x-request-id"] = request_id
    response.headers["x-model-version"] = str(STATE["version"])
    fields.update(status=response.status_code, latency_ms=latency_ms,
                  model_version=str(STATE["version"]))
    log.log(
        logging.ERROR if response.status_code >= 500 else logging.INFO,
        "request completed", extra={"fields": fields},
    )
    return response


@app.get("/health", response_model_exclude_none=True)
def health() -> StatusResponse:
    """Liveness. The process is up. Says nothing about whether it can serve."""
    return StatusResponse(status="alive", model_version=str(STATE["version"]))


@app.get("/ready", response_model_exclude_none=True)
def ready(response: Response) -> StatusResponse:
    """Readiness. The model is loaded and can score.

    These two are genuinely different, and confusing them causes a specific production
    failure: traffic routed to a container whose model has not finished loading. All three
    providers distinguish them, and Quiz 3 asks about it.
    """
    if STATE["model"] is None:
        response.status_code = 503
        return StatusResponse(status="not_ready", reason="model not loaded",
                              model_version=str(STATE["version"]))
    return StatusResponse(status="ready", model_version=str(STATE["version"]))


def _score(rows: list[dict]) -> list[float]:
    if STATE["model"] is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    return _score_model(STATE["model"], rows)


def _score_model(model, rows: list[dict]) -> list[float]:
    import pandas as pd

    from src.data import FEATURES

    frame = pd.DataFrame(rows)[FEATURES]
    return [float(p) for p in model.predict_proba(frame)[:, 1]]


class TimedPredictionRequest(Request):
    async def body(self) -> bytes:
        body = await super().body()
        if not hasattr(self, "_processing_started_ns"):
            # Start after the complete body arrives; cached reads must not reset it.
            self._processing_started_ns = time.perf_counter_ns()
        return body

    async def json(self) -> Any:
        if hasattr(self, "_json"):
            return await super().json()
        await self.body()
        started = time.perf_counter_ns()
        value = await super().json()  # Keep the framework's parser and error behavior.
        self.state.prediction_timings["json_decode_ms"] = (
            time.perf_counter_ns() - started
        ) / 1_000_000
        return value


class PredictionTimingRoute(APIRoute):
    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def timed_handler(request: Request) -> Response:
            request = TimedPredictionRequest(request.scope, request.receive)
            request.state.prediction_timings = {}
            response = await original_handler(request)
            # Exceptions keep their existing handlers and get no success timings.
            if response.status_code == 200:
                timings = request.state.prediction_timings
                timings["processing_ms"] = (
                    time.perf_counter_ns() - request._processing_started_ns
                ) / 1_000_000
                response.headers["Server-Timing"] = ", ".join(
                    f"{name};dur={timings[name + '_ms']:.6f}"
                    for name in ("json_decode", "scoring", "processing")
                )
            return response

        return timed_handler


# Only /predict uses this route; batch, readiness and liveness stay unchanged.
prediction_router = APIRouter(route_class=PredictionTimingRoute)


@prediction_router.post("/predict", response_model=PredictResponse)
def predict(payload: PredictRequest, request: Request) -> PredictResponse:
    started = time.perf_counter_ns()
    score = _score([payload.model_dump()])[0]
    request.state.prediction_timings["scoring_ms"] = (
        time.perf_counter_ns() - started
    ) / 1_000_000
    return PredictResponse(probability=score, model_version=str(STATE["version"]))


app.include_router(prediction_router)


@app.post("/predict/batch", response_model=BatchResponse)
def predict_batch(payload: BatchRequest) -> BatchResponse:
    scores = _score([row.model_dump() for row in payload.rows])
    return BatchResponse(probabilities=scores, model_version=str(STATE["version"]))
