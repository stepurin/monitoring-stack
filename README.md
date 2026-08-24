# monitoring-stack

A small observability stack built around a worker that never stops: it
chews through a job queue in Postgres while metrics, logs and traces flow
out of it. Each of the three signals can be switched off on its own.

## What's inside

| Service | Role |
|---|---|
| **worker** | claims jobs from Postgres, processes them, writes results back |
| **postgres** | holds the `jobs` table |
| **prometheus** | scrapes the worker every 5s |
| **fluent-bit** | receives logs from Docker, forwards them to Loki |
| **loki** | log storage |
| **otel-collector** | receives traces over OTLP, forwards them to Tempo |
| **tempo** | trace storage |
| **grafana** | all three datasources, pre-wired |

```
metrics   prometheus ──scrape──> worker:8000/metrics
logs      every container ──docker fluentd driver──> fluent-bit ──> loki
traces    worker ──OTLP──> otel-collector ──> tempo
```

The three branches are independent — nothing in one is required by another.

## Quick start

```bash
docker compose up --build
```

Nothing else to do: the worker starts producing and consuming jobs
immediately, so there's data in Grafana within seconds.

| Service | URL |
|---|---|
| worker metrics | http://localhost:8000/metrics |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (anonymous, admin role) |
| Postgres | `localhost:5432`, user/password/db: `postgres`/`postgres`/`jobs` |

## How the worker works

A producer thread keeps adding jobs; the main loop runs a tick every two
seconds:

1. claim a batch of `pending` jobs and flip them to `processing`
   (`FOR UPDATE SKIP LOCKED`, so several workers could run side by side)
2. process each one — takes a moment, and fails ~5% of the time
3. write the outcome back as `done` or `failed`

Every tick is one trace: the batch, a span per job, and a span for each SQL
statement underneath.

Tuning knobs, all environment variables on the `worker` service:
`BATCH_SIZE`, `TICK_SECONDS`, `FAILURE_RATE`.

## Metrics

| Metric | Type | What it tells you |
|---|---|---|
| `job_queue_depth` | gauge | how far behind the worker is |
| `jobs_processed_total{status}` | counter | throughput, and the failure rate |
| `jobs_produced_total` | counter | how fast work arrives |
| `job_duration_seconds` | histogram | per-job latency |
| `batch_duration_seconds` | histogram | how long a full tick takes |

Watch the queue drain by raising `BATCH_SIZE`, or watch it grow by lowering
it — the gauge reacts within seconds.

## Turning parts off

```bash
docker compose stop fluent-bit loki          # no logs
docker compose stop otel-collector tempo     # no traces
docker compose stop prometheus               # no metrics
```

The worker keeps running in every case. Logging uses `fluentd-async`, so
containers start and stay up even when Fluent Bit isn't there — logs are
dropped, nothing blocks.

## Logs

Docker ships every container's stdout to Fluent Bit through the `fluentd`
logging driver, which tags each record with the container name. Fluent Bit
parses JSON lines, promotes `container_name` and `level` to Loki labels, and
attaches `trace_id` as structured metadata.

## Correlating logs and traces

Log lines the worker emits inside a tick carry the active `trace_id`
(see `JsonFormatter` in `app/worker.py`). In Grafana:

- **Logs → trace**: open a log line in Explore (Loki), click *View Trace*
- **Trace → logs**: open a trace in Explore (Tempo), click *Logs for this span*

A failed job is the shortest path through all three signals: the counter
ticks up, the log line says which job, and the trace shows the SQL around it.

## Using it for your own app

1. Point `configs/prometheus.yml` at your service and expose `/metrics`
   from it (use the Prometheus client library for your language).
2. Send traces to `otel-collector:4317` over OTLP — with an OpenTelemetry
   SDK, as `app/worker.py` does, or via zero-code auto-instrumentation.
3. Log JSON to stdout including `trace_id`, add the same `logging:` block as
   the other services, and logs land in Loki with no further wiring.
