# monitoring-stack

A small observability stack built around a my_service that never stops: it
chews through a job queue in Postgres while metrics, logs and traces flow
out of it. Each of the three signals can be switched off on its own.

![Architecture, as explained to a five-year-old](docs/architecture.svg)

## What's inside

| Service | Role |
|---|---|
| **my_service** | claims jobs from Postgres, processes them, writes results back |
| **postgres** | holds the `jobs` table |
| **postgres-exporter** | exposes Postgres' own metrics to Prometheus |
| **prometheus** | scrapes my_service and the exporter every 5s, evaluates alert rules |
| **alertmanager** | receives firing alerts, groups them, would notify |
| **fluent-bit** | receives logs from Docker, forwards them to Loki |
| **loki** | log storage |
| **otel-collector** | receives traces over OTLP, forwards them to Tempo |
| **tempo** | trace storage |
| **grafana** | all three datasources and the dashboards, pre-wired |

```
metrics   prometheus ──scrape──┬─> my_service:8000/metrics
                               ├─> postgres-exporter:9187
                               └──alerts──> alertmanager
logs      every container ──docker fluentd driver──> fluent-bit ──> loki
traces    my_service ──OTLP──> otel-collector ──> tempo
```

The three branches are independent — nothing in one is required by another.

## Quick start

```bash
docker compose up --build
```

Nothing else to do: my_service starts producing and consuming jobs
immediately, so there's data in Grafana within seconds.

First time, or something didn't come up?
[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) walks through the run and
what to check; [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) is organised
by symptom.

## Endpoints

Only Grafana, Prometheus and Alertmanager have a web UI. Loki and Tempo are
API-only — you read them through Grafana.

| Service | Endpoint | What's there |
|---|---|---|
| Grafana | http://localhost:3000 | UI — anonymous login, admin role |
| Prometheus | http://localhost:9090 | UI — `/targets`, `/alerts`, `/rules` |
| Alertmanager | http://localhost:9093 | UI — alerts that are currently firing |
| my_service | http://localhost:8000/metrics | raw metrics, exactly as Prometheus sees them |
| postgres-exporter | http://localhost:9187/metrics | Postgres' own metrics |
| Loki | http://localhost:3100 | API — `/ready`, `/metrics`, `/loki/api/v1/query` |
| Tempo | http://localhost:3200 | API — `/ready`, `/metrics`, `/api/traces/{id}` |
| Fluent Bit | `localhost:24224` | forward protocol, not HTTP |
| Postgres | `localhost:5432` | user / password / db: `postgres` / `postgres` / `jobs` |

## Dashboards

Four dashboards are provisioned from `configs/dashboards/` — they appear in
Grafana on first start, no import needed:

| Dashboard | What it shows |
|---|---|
| **my_service — metrics** | queue depth, throughput by status, failure rate, job and tick latency percentiles, a latency heatmap |
| **Logs — all containers** | log volume by level and by container, an errors-only panel, and a free-text search over everything |
| **Traces — my_service** | recent traces, traces containing a failed job, slow ticks, slow SQL |
| **Postgres — database (9628)** | connections, transactions, tuples, locks, bgwriter, cache hit ratio, settings |

The Postgres one is [dashboard 9628](https://grafana.com/grafana/dashboards/9628-postgresql-database/)
from grafana.com, adapted: it ships for the Helm chart, so it expects
`kubernetes_namespace` and `release` labels that a plain Docker exporter never
sets. The `namespace` and `release` variables are gone, the `release="$release"`
filter is stripped from the eight queries that carried it, and `instance` and
`datname` are resolved with `label_values` instead of Kubernetes-shaped
`query_result` regexes.

Edits made in the UI stick until the next restart; the files in
`configs/dashboards/` are the source of truth. The provider re-reads the
directory every 15s, so dropping a new JSON in there is enough — no restart.

### Where to look first

Dashboards are the guided version. **Explore** (the compass icon) is the raw one:

| Datasource | Query | What you get |
|---|---|---|
| Prometheus | `job_queue_depth` | the queue rising and draining |
| Prometheus | `rate(jobs_processed_total[1m])` | throughput, split by status |
| Loki | `{container_name="my_service"}` | the service's logs |
| Loki | `{job="docker"}` | every container's logs |
| Tempo | Search, service `my_service` | traces of individual ticks |

Expand a `job N failed` line in Loki and click **View Trace** — it jumps
straight to that job's trace, SQL statements included.

Straight from the shell, without Grafana:

```bash
curl -s localhost:8000/metrics | grep job_queue_depth
```

```bash
curl -s 'localhost:9090/api/v1/query?query=job_queue_depth' | python3 -m json.tool
```

## How my_service works

A producer thread keeps adding jobs; the main loop runs a tick every two
seconds:

1. claim a batch of `pending` jobs and flip them to `processing`
   (`FOR UPDATE SKIP LOCKED`, so several instances could run side by side)
2. process each one — takes a moment, and fails ~5% of the time
3. write the outcome back as `done` or `failed`

Every tick is one trace: the batch, a span per job, and a span for each SQL
statement underneath.

Tuning knobs, all environment variables on the `my_service` service:
`BATCH_SIZE`, `TICK_SECONDS`, `FAILURE_RATE`.

## Metrics

| Metric | Type | What it tells you |
|---|---|---|
| `job_queue_depth` | gauge | how far behind my_service is |
| `jobs_processed_total{status}` | counter | throughput, and the failure rate |
| `jobs_produced_total` | counter | how fast work arrives |
| `job_duration_seconds` | histogram | per-job latency |
| `batch_duration_seconds` | histogram | how long a full tick takes |

Watch the queue drain by raising `BATCH_SIZE`, or watch it grow by lowering
it — the gauge reacts within seconds.

Postgres reports on itself through `postgres-exporter` — connections
(`pg_stat_activity_count`), transaction and rollback rates
(`pg_stat_database_xact_commit`), cache hit ratio, table and index sizes.
Handy next to the service's own numbers: a growing queue with flat
transaction throughput usually means the bottleneck isn't the database.

## Alerts

Rules live in `configs/alerts.yml`, Prometheus evaluates them every 15s and
pushes what fires to Alertmanager.

| Alert | Fires when |
|---|---|
| `MyServiceDown` | `up{job="my_service"} == 0` for 30s |
| `QueueBacklog` | `job_queue_depth > 200` for 1m |
| `HighFailureRate` | more than 20% of jobs fail, over 5m, for 2m |
| `SlowJobs` | p99 job duration above 1s for 2m |
| `PostgresDown` | `pg_up == 0` for 30s |

To watch one fire, stop my_service:

```bash
docker compose stop my_service
```

`MyServiceDown` shows up as *Pending* on http://localhost:9090/alerts, turns
*Firing* after 30s, and lands in http://localhost:9093 a few seconds later.

The default receiver has no integration, so nothing leaves the machine —
firing alerts are visible in the Alertmanager UI and nowhere else. To change
that, uncomment `telegram_configs` in `configs/alertmanager.yml` and fill in a
bot token and chat id; `slack_configs` and `webhook_configs` are there too.

## Turning parts off

```bash
docker compose stop fluent-bit loki                         # no logs
```

```bash
docker compose stop otel-collector tempo                    # no traces
```

```bash
docker compose stop prometheus alertmanager postgres-exporter   # no metrics
```

my_service keeps running in every case. Logging uses `fluentd-async`, so
containers start and stay up even when Fluent Bit isn't there — logs are
dropped, nothing blocks.

## Logs

Docker ships every container's stdout to Fluent Bit through the `fluentd`
logging driver, which tags each record with the container name. Fluent Bit
parses JSON lines, promotes `container_name` and `level` to Loki labels, and
attaches `trace_id` as structured metadata.

## Correlating logs and traces

Log lines my_service emits inside a tick carry the active `trace_id`
(see `JsonFormatter` in `app/my_service.py`). In Grafana:

- **Logs → trace**: open a log line in Explore (Loki), click *View Trace*
- **Trace → logs**: open a trace in Explore (Tempo), click *Logs for this span*

A failed job is the shortest path through all three signals: the counter
ticks up, the log line says which job, and the trace shows the SQL around it.

## The exercise: a connection leak

The stack ships with a fault in it on purpose. `main` is the broken state,
`fixed` is the repaired one, and `git diff main fixed` is four lines.

> Spoilers below. If you want to work it out from the dashboards first, stop
> reading and run the stack.

`measure_backlog()` in `app/my_service.py` takes a connection from the pool
to read the queue depth and never returns it. One per tick, so the pool only
grows. Postgres runs with `max_connections=30` here, which puts the ceiling
about ninety seconds away.

What you see, in the order you tend to see it:

| Signal | What it shows |
|---|---|
| Alert | `MyServiceDown` — the process exits with code 1 |
| Logs | thirty lines of `postgres not ready, retrying (N/30)` |
| Metrics | connections climbing to the ceiling, `active` flat at 1 |
| Traces | one failing trace, the last one before the exit |

The logs are the interesting part, because they are wrong. `connect()` cannot
tell *too many clients already* from *not up yet*, so it reports the latter.
Postgres never went anywhere — `pg_up` stays at 1 and `PostgresDown` never
fires. The connections were the service's own, all of them idle.

**Where to look.** The *Connections vs ceiling* panel on the Postgres
dashboard: a straight climb towards the red dashed line, with the active
count unmoved. Connections open but doing nothing is the shape of a leak.

**Then the trace.** Retry log lines carry a `trace_id`, so *View Trace* takes
you to the failing tick: a connection reused in 0.2 ms with `pool.size` at 29,
ten jobs processed and committed, and a thirty-second red span at the end.
The work succeeded; the bookkeeping after it could not get a connection.
Expand the red `pool-acquire`, open **Events**, and the exception names it:
`psycopg2.OperationalError: FATAL: sorry, too many clients already`.

In TraceQL:

```
{status = error}                                     the failure
{name = "pool-acquire" && span.pool.size > 10}       the pool, oversized
{name = "pool-acquire" && span.pool.reused = false}  who keeps opening new ones
```

Only the last trace before the exit fails — every tick before it is healthy.
If you just started the stack, wait for it.

**The fix**, in full:

```python
    with tracer.start_as_current_span("measure-backlog"):
        connection = acquire()

        try:
            return count_pending(connection)
        finally:
            release(connection)
```

`run_tick()` and `produce_forever()` already do this. A connection is
something you borrow and give back.

### Running it

```bash
git switch main                          # or: fixed
docker compose build --no-cache my_service
docker compose restart postgres
docker compose up -d --force-recreate my_service
```

`--no-cache` matters: Docker will happily rebuild the image without noticing
that the source changed. Check what actually ended up inside the container
rather than what is on disk:

```bash
docker compose exec my_service grep -A7 "def measure_backlog" /app/my_service.py
```

No `release` in that function means the leak is in place.

Watch it go:

```bash
docker compose exec postgres psql -U postgres -d jobs \
  -c "SELECT state, count(*) FROM pg_stat_activity WHERE datname='jobs' GROUP BY state"
```

Open that psql session *before* the connections run out — at the ceiling a new
one is refused, and `postgres-exporter` loses its slot too, which puts a gap in
the metric right when you want it.

## Using it for your own app

1. Point `configs/prometheus.yml` at your service and expose `/metrics`
   from it (use the Prometheus client library for your language).
2. Send traces to `otel-collector:4317` over OTLP — with an OpenTelemetry
   SDK, as `app/my_service.py` does, or via zero-code auto-instrumentation.
3. Log JSON to stdout including `trace_id`, add the same `logging:` block as
   the other services, and logs land in Loki with no further wiring.
