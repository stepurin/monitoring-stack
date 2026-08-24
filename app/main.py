import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "demo-app")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")


# --- telemetry --------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """JSON on stdout, with the active trace id inlined.

    Docker ships these lines to Fluent Bit, which parses the JSON and passes
    trace_id on to Loki as structured metadata — that's what links a log line
    to its trace in Grafana.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload)


tracer_provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)

_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)

logger = logging.getLogger(SERVICE_NAME)
tracer = trace.get_tracer(SERVICE_NAME)


# --- metrics ----------------------------------------------------------------

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
)


# --- app --------------------------------------------------------------------

app = FastAPI(title=SERVICE_NAME)
FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics")


@app.middleware("http")
async def track_metrics(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start

    # Use the matched route template ("/users/{id}"), not the raw path, so
    # labels don't explode into one time series per distinct URL.
    route = request.scope.get("route")
    path = route.path if route else request.url.path

    REQUEST_COUNT.labels(request.method, path, response.status_code).inc()
    REQUEST_LATENCY.labels(request.method, path).observe(duration)

    return response


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/work")
async def work():
    with tracer.start_as_current_span("fetch-data"):
        await asyncio.sleep(random.uniform(0.05, 0.3))

    with tracer.start_as_current_span("process-data"):
        await asyncio.sleep(random.uniform(0.05, 0.5))

    logger.info("work completed")
    return {"status": "done"}


@app.get("/error")
async def maybe_error():
    if random.random() < 0.2:
        logger.error("simulated failure")
        raise HTTPException(status_code=500, detail="simulated failure")
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
