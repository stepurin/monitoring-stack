# monitoring-stack

A small observability stack for a single application: metrics, logs and
traces, wired so that each of the three can be switched off on its own.

## What's inside

| Service | Role |
|---|---|
| **demo-app** | FastAPI app: `/metrics`, traces over OTLP, JSON logs on stdout |
| **prometheus** | scrapes `demo-app` every 5s |
| **fluent-bit** | receives logs from Docker, forwards them to Loki |
| **loki** | log storage |
| **otel-collector** | receives traces over OTLP, forwards them to Tempo |
| **tempo** | trace storage |
| **grafana** | all three datasources, pre-wired |

```
metrics   prometheus ──scrape──> demo-app
logs      every container ──docker fluentd driver──> fluent-bit ──> loki
traces    demo-app ──OTLP──> otel-collector ──> tempo
```

The three branches are independent — nothing in one is required by another.

## Quick start

```bash
docker compose up --build
```

| Service | URL |
|---|---|
| demo-app | http://localhost:8000 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (anonymous, admin role) |

Generate some traffic:

```bash
while true; do curl -s localhost:8000/work > /dev/null; curl -s localhost:8000/error > /dev/null; sleep 1; done
```

## Endpoints

| Endpoint | What it shows |
|---|---|
| `/` | health check |
| `/work` | nested spans, ~0.1-0.8s latency |
| `/error` | ~20% chance of a 500, logged at error level |
| `/metrics` | Prometheus scrape target |

## Turning parts off

```bash
docker compose stop fluent-bit loki          # no logs
docker compose stop otel-collector tempo     # no traces
docker compose stop prometheus               # no metrics
```

The app keeps running in every case. Logging uses `fluentd-async`, so
containers start and stay up even when Fluent Bit isn't there — logs are
dropped, nothing blocks.

## Logs

Docker ships every container's stdout to Fluent Bit through the `fluentd`
logging driver, which tags each record with the container name. Fluent Bit
parses JSON lines, promotes `container_name` and `level` to Loki labels, and
attaches `trace_id` as structured metadata.

## Correlating logs and traces

Log lines the app emits inside a request carry the active `trace_id`
(see `JsonFormatter` in `app/main.py`). In Grafana:

- **Logs → trace**: open a log line in Explore (Loki), click *View Trace*
- **Trace → logs**: open a trace in Explore (Tempo), click *Logs for this span*

## Using it for your own app

1. Point `configs/prometheus.yml` at your service and expose `/metrics`
   from it (use the Prometheus client library for your language).
2. Send traces to `otel-collector:4317` over OTLP — with an OpenTelemetry
   SDK, as `app/main.py` does, or via zero-code auto-instrumentation.
3. Log JSON to stdout including `trace_id`, add the same `logging:` block as
   the other services, and logs land in Loki with no further wiring.
