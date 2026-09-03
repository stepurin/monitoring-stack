"""A worker that keeps chewing through a job queue in Postgres.

Runs until the container stops: a producer thread keeps adding jobs, the
main loop claims them in batches, processes them, and writes the results
back. Every tick is a trace, every SQL statement a span inside it.
"""

import hashlib
import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone

import psycopg2
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram, start_http_server

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "worker")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/jobs")

METRICS_PORT = int(os.getenv("METRICS_PORT", "8000"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2"))
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0.05"))


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


# Logging goes first: instrument() below reports failure by logging rather
# than raising, and anything logged before this line escapes the JSON format
# and never reaches Loki.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)

tracer_provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)

# Wraps every SQL statement in its own span, so a trace shows the actual
# queries and how long each took.
#
# skip_dep_check is required, not optional: the instrumentation declares
# `psycopg2 >= 2.7.3.1`, and we install psycopg2-binary — a different
# distribution name. The check therefore fails, and instrument() would log
# a DependencyConflict and return without patching anything, leaving traces
# silently free of SQL spans. The driver itself is identical.
Psycopg2Instrumentor().instrument(skip_dep_check=True)

logger = logging.getLogger(SERVICE_NAME)
tracer = trace.get_tracer(SERVICE_NAME)


# --- metrics ----------------------------------------------------------------

JOBS_PROCESSED = Counter(
    "jobs_processed_total",
    "Jobs that reached a terminal state",
    ["status"],
)

JOBS_PRODUCED = Counter(
    "jobs_produced_total",
    "Jobs added to the queue",
)

JOB_DURATION = Histogram(
    "job_duration_seconds",
    "Time spent processing a single job",
)

QUEUE_DEPTH = Gauge(
    "job_queue_depth",
    "Jobs currently waiting to be picked up",
)

BATCH_DURATION = Histogram(
    "batch_duration_seconds",
    "Time spent on one full tick: claim, process, write back",
)


# --- database ---------------------------------------------------------------


def connect(retries: int = 30) -> psycopg2.extensions.connection:
    """Postgres may still be starting up, so retry before giving up."""
    for attempt in range(1, retries + 1):
        try:
            connection = psycopg2.connect(DSN)
            connection.autocommit = True
            return connection
        except psycopg2.OperationalError:
            if attempt == retries:
                raise
            logger.info(f"postgres not ready, retrying ({attempt}/{retries})")
            time.sleep(1)

    raise RuntimeError("unreachable")


def claim_batch(connection, size: int) -> list[tuple[int, str]]:
    """Grab a batch of pending jobs and mark them as in flight.

    SKIP LOCKED means several workers could run side by side without
    handing the same job to two of them.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs
               SET status = 'processing',
                   attempts = attempts + 1,
                   updated_at = now()
             WHERE id IN (
                   SELECT id FROM jobs
                    WHERE status = 'pending'
                    ORDER BY id
                    LIMIT %s
                      FOR UPDATE SKIP LOCKED
             )
         RETURNING id, payload
            """,
            (size,),
        )
        return cursor.fetchall()


def finish_job(connection, job_id: int, status: str, result: str | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs
               SET status = %s,
                   result = %s,
                   updated_at = now()
             WHERE id = %s
            """,
            (status, result, job_id),
        )


def count_pending(connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM jobs WHERE status = 'pending'")
        return cursor.fetchone()[0]


# --- work -------------------------------------------------------------------


def process(payload: str) -> str:
    """Stand-in for real work: takes a while, and sometimes falls over."""
    time.sleep(random.uniform(0.05, 0.3))

    if random.random() < FAILURE_RATE:
        raise RuntimeError("processing failed")

    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def produce_forever() -> None:
    """Keeps the queue fed so the worker always has something to do."""
    connection = connect()

    while True:
        count = random.randint(1, 5)
        with tracer.start_as_current_span("produce"):
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO jobs (payload) VALUES (%s)",
                    [(f"task-{random.randint(1000, 9999)}",) for _ in range(count)],
                )
        JOBS_PRODUCED.inc(count)
        time.sleep(1)


def run_tick(connection) -> int:
    with tracer.start_as_current_span("tick") as tick:
        jobs = claim_batch(connection, BATCH_SIZE)
        tick.set_attribute("batch.size", len(jobs))

        for job_id, payload in jobs:
            with tracer.start_as_current_span("process-job") as span:
                span.set_attribute("job.id", job_id)
                start = time.perf_counter()

                try:
                    result = process(payload)
                    finish_job(connection, job_id, "done", result)
                    JOBS_PROCESSED.labels("done").inc()
                except RuntimeError:
                    finish_job(connection, job_id, "failed", None)
                    JOBS_PROCESSED.labels("failed").inc()
                    span.set_attribute("job.failed", True)
                    logger.error(f"job {job_id} failed")
                finally:
                    JOB_DURATION.observe(time.perf_counter() - start)

        QUEUE_DEPTH.set(count_pending(connection))
        return len(jobs)


def main() -> None:
    start_http_server(METRICS_PORT)
    logger.info(f"metrics served on :{METRICS_PORT}")

    connection = connect()
    threading.Thread(target=produce_forever, daemon=True).start()
    logger.info("worker started")

    while True:
        start = time.perf_counter()
        processed = run_tick(connection)
        BATCH_DURATION.observe(time.perf_counter() - start)

        if processed:
            logger.info(f"processed {processed} jobs")

        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
