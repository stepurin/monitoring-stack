# Getting started

For a first run, start to finish. If something goes wrong, jump to
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## What you need

**Docker with Compose v2.** Check both at once:

```bash
docker compose version
```

You want `Docker Compose version v2.x`. If you get `command not found`,
install Docker first — Docker Desktop on macOS and Windows, `docker-ce` from
your distro's repository on Linux. If you get `docker-compose` (with a hyphen)
working but not `docker compose`, you're on Compose v1: it is end-of-life and
this stack is not tested against it.

**About 2 GB of free RAM.** Ten containers, none of them heavy, but Loki and
Prometheus each want a few hundred megabytes once data starts flowing.

**Nine free ports.** These are all published to your machine:

| Port | Service |
|---|---|
| 3000 | Grafana |
| 3100 | Loki |
| 3200 | Tempo |
| 5432 | Postgres |
| 8000 | worker metrics |
| 9090 | Prometheus |
| 9093 | Alertmanager |
| 9187 | postgres-exporter |
| 24224 | Fluent Bit |

5432 is the one most likely to be taken — a local Postgres, or another
project's container. To check before you start:

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -E '3000|3100|3200|5432|8000|909[03]|9187|24224'
```

Empty output means you're clear. If something answers, either stop it or
change the left-hand side of the port mapping in `docker-compose.yml` —
`"5433:5432"` publishes the database on 5433 instead.

## Run it

```bash
git clone https://github.com/stepurin/monitoring-stack && cd monitoring-stack
```

```bash
docker compose up --build -d
```

The first run pulls nine images and builds one, so give it a few minutes.
Later runs take seconds.

`-d` puts everything in the background. Drop it if you'd rather watch the
logs scroll — then `Ctrl+C` stops the whole stack.

## Check it worked

```bash
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

Ten containers, every one of them `Up`. A container that says `Restarting`
is in a crash loop — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Then the shortest end-to-end proof, which needs nothing but curl:

```bash
curl -s localhost:8000/metrics | grep job_queue_depth
```

A number means the worker is alive, connected to Postgres and counting.

## The first sixty seconds

Everything below has data within a minute of starting. In this order:

**1. Prometheus is scraping** — http://localhost:9090/targets

Three targets, all green: `prometheus`, `worker`, `postgres`. If `worker` is
red, the worker container is not up.

**2. The dashboards are there** — http://localhost:3000/dashboards

Four of them, provisioned from the repository — nothing to import. No login:
anonymous access is on, with admin rights.

Open **Worker — metrics**. Queue depth near zero, throughput a few jobs per
second, failure rate around 5%. That 5% is deliberate — the worker fails jobs
on purpose so there is something to look at.

**3. Logs are arriving** — Grafana → Explore → Loki datasource:

```
{container_name="worker"}
```

JSON lines, roughly one per tick. If this is empty, the logging branch is the
thing to debug; the other two are unaffected.

**4. Traces are arriving** — Grafana → Explore → Tempo → Search, service
`worker`. Open any trace: `tick` at the top, `process-job` under it, SQL
statements under those.

**5. The whole point** — Grafana → Dashboards → **Logs — all containers** →
find a `job N failed` line in the errors panel → expand it → **View Trace**.

That jump, from a log line to the SQL statements around the failure, is what
the three signals are for.

## Make something happen

The queue is boring when the worker keeps up. Starve it:

```bash
BATCH_SIZE=1 docker compose up -d worker
```

The producer still adds up to five jobs a second while the worker now takes
one every two seconds. Watch `job_queue_depth` climb on the dashboard. Put it
back with:

```bash
docker compose up -d worker
```

Other knobs on the `worker` service: `TICK_SECONDS`, `FAILURE_RATE`.

## Stopping

Stop, keep the data:

```bash
docker compose stop
```

Remove the containers, keep the database:

```bash
docker compose down
```

Remove everything including the `pgdata` volume:

```bash
docker compose down -v --remove-orphans
```

Use the last one when you want a genuinely clean start — and after changing
the set of services, since containers from the previous set stick around as
orphans otherwise.

## Where to go next

- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — symptom, command, cause
- [../README.md](../README.md) — what each service does, the metrics, the
  alerts, and how to point this at your own application
