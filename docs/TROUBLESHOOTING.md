# Troubleshooting

Symptom first. Each section has the command that tells you what's wrong, then
the likely causes in the order they're worth checking.

| Symptom | Go to |
|---|---|
| A container keeps restarting | [Crash loops](#a-container-keeps-restarting) |
| `docker compose up` fails on a port | [Port already in use](#port-already-in-use) |
| Worker logs say Postgres isn't ready | [Worker can't reach Postgres](#worker-cant-reach-postgres) |
| Prometheus target is red | [A scrape target is down](#a-scrape-target-is-down) |
| Dashboards are missing in Grafana | [No dashboards](#no-dashboards-in-grafana) |
| Panels are there but empty | [Empty panels](#panels-render-but-are-empty) |
| No logs in Loki | [No logs](#no-logs-in-loki) |
| No traces in Tempo | [No traces](#no-traces-in-tempo) |
| `View Trace` link is missing on a log line | [Correlation](#view-trace-doesnt-appear-on-a-log-line) |
| Alerts never fire | [Alerts](#alerts-never-fire) |
| Everything is slow / fans spinning | [Resources](#everything-is-slow) |

## First two commands, always

```bash
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

```bash
docker compose logs --tail=50 <service>
```

Between them they explain most failures. Service names are the keys in
`docker-compose.yml`: `worker`, `postgres`, `postgres-exporter`, `prometheus`,
`alertmanager`, `fluent-bit`, `loki`, `otel-collector`, `tempo`, `grafana`.

---

## A container keeps restarting

```bash
docker compose logs --tail=50 worker
```

`Restarting` in `ps` means the process exits and Docker starts it again. The
logs from just before the exit say why.

- **A config file has a syntax error.** Everything except the worker mounts
  its config read-only from `configs/`; a bad line there kills the process on
  startup. The log names the file and usually the line.
- **The mounted file isn't there.** If you renamed or moved something under
  `configs/`, Docker creates an empty *directory* at the mount point instead
  of failing loudly, and the service starts with no configuration.
- **A port inside the container is taken** — rare, but happens if you added a
  service that binds the same container port.

## Port already in use

```
Error response from daemon: ... bind: address already in use
```

Find the squatter:

```bash
lsof -nP -iTCP:5432 -sTCP:LISTEN
```

Either stop it, or change the published port in `docker-compose.yml`. Only the
left-hand number changes — `"5433:5432"` keeps the container on 5432 and
publishes it to your machine on 5433. Nothing inside the stack cares, because
containers talk to each other by service name.

5432 (a local Postgres) and 3000 (any other dev server) are the usual
offenders.

## Worker can't reach Postgres

```bash
docker compose logs --tail=20 worker
```

`postgres not ready, retrying (3/30)` on startup is **normal** — there is no
`depends_on` anywhere in this stack, so the worker may well start first. It
retries once a second, thirty times, and Postgres is usually up within ten.

It is a problem when the counter runs to 30 and the container exits:

```bash
docker compose logs --tail=30 postgres
```

- Postgres failed to initialise — most often a leftover volume from an older
  run with different credentials. `docker compose down -v` wipes it.
- The database name doesn't match. `DATABASE_URL` on the worker ends in
  `/jobs`, and `POSTGRES_DB` on Postgres must say `jobs` too.

## A scrape target is down

http://localhost:9090/targets shows the error next to the target — it is
almost always more specific than anything you'd guess.

- **`worker` down** — the worker container isn't running. Check its logs.
- **`postgres` down** — that's `postgres-exporter`, not the database. Usually
  the exporter is up but can't authenticate; check its logs:

  ```bash
  docker compose logs --tail=20 postgres-exporter
  ```

- **connection refused on a service that is running** — Prometheus reaches
  targets by container name over the compose network. `localhost` in
  `configs/prometheus.yml` would mean *the Prometheus container itself*, which
  is correct only for the `prometheus` job.

## No dashboards in Grafana

```bash
docker compose logs grafana | grep -i provisioning
```

The provider reads `/var/lib/grafana/dashboards` every 15s, so a new file
appears without a restart. If nothing shows up:

- **The mount is missing.** `docker compose config` should list both
  `configs/grafana-dashboards.yaml` and `configs/dashboards` under grafana's
  volumes. If you started the stack before those lines existed, recreate the
  container: `docker compose up -d --force-recreate grafana`.
- **A dashboard file is invalid JSON**, so the whole provider run fails. The
  Grafana log names the file.
- **Two dashboards share a `uid`.** Grafana loads one and logs a conflict for
  the other.

## Panels render but are empty

Work out which side is missing data before touching the dashboard.

```bash
curl -s 'localhost:9090/api/v1/query?query=job_queue_depth'
```

If that returns a value and the panel is still blank, the problem is in the
panel. If it returns an empty result, the metric isn't being collected —
go to [scrape targets](#a-scrape-target-is-down).

For the **Postgres — database (9628)** dashboard specifically: it was written
for the Helm chart, which labels metrics with `kubernetes_namespace` and
`release`. Those labels don't exist here. The copy in this repository is
already adapted — but if you re-import 9628 from grafana.com yourself, most
panels will be empty for exactly that reason. Any community dashboard can
have this problem; check what labels its queries expect against what your
exporter actually emits:

```bash
curl -s localhost:9187/metrics | grep '^pg_up'
```

Empty panels on the adapted dashboard usually mean a metric your exporter
version no longer publishes — `pg_stat_bgwriter_*` was renamed in
postgres_exporter 0.15, and some collectors are off by default.

## No logs in Loki

The logging branch has the most moving parts. Walk it from the end.

**Is Loki alive?**

```bash
curl -s localhost:3100/ready
```

**Is Fluent Bit receiving anything?**

```bash
docker compose logs --tail=30 fluent-bit
```

- Errors mentioning `loki` — Fluent Bit is receiving logs but can't deliver
  them. If you see `structured_metadata` rejected, Loki's config lost
  `allow_structured_metadata: true` or the schema is older than v13.
- Nothing at all, no traffic — Docker isn't delivering. See below.

**Is Docker delivering?** Every service has `logging: *fluentd` in
`docker-compose.yml`, which makes the Docker daemon push stdout to
`localhost:24224`. Note `fluentd-async: "true"`: with it, containers start
happily even when Fluent Bit is down and their logs are silently dropped —
that's deliberate, so the logging branch can be switched off, but it also
means failure here is quiet.

If Fluent Bit was down when a container started, that container's logs are
gone for good. Restart it:

```bash
docker compose restart worker
```

One consequence worth knowing: `docker compose logs worker` shows nothing for
services using the fluentd driver, because their output went to Fluent Bit
instead of Docker's own log store. Read them in Grafana, not in the terminal.

## No traces in Tempo

```bash
docker compose logs --tail=30 otel-collector
```

- **Nothing arriving** — the worker can't reach the collector. Its
  `OTEL_EXPORTER_OTLP_ENDPOINT` must be `http://otel-collector:4317`; the
  collector must be up.
- **Arriving but not forwarded** — errors mentioning `tempo` in the collector
  log. Check Tempo: `curl -s localhost:3200/ready`.
- **Traces exist but appear late** — normal. `BatchSpanProcessor` buffers, so
  a trace can take a few seconds to show up. Widen the time range in Tempo
  search before concluding anything.

## `View Trace` doesn't appear on a log line

Three things have to line up, in this order:

1. **The line has a `trace_id`.** Expand it in Grafana — you should see
   `trace_id` under structured metadata. If it's absent, the log was written
   outside an active span. Producer and startup lines legitimately have none;
   `job N failed` always should.
2. **Fluent Bit passed it through.** In `configs/fluent-bit.conf`, the Loki
   output needs `structured_metadata trace_id=$trace_id`.
3. **Grafana knows what to do with it.** In `configs/grafana-datasources.yaml`,
   the Loki datasource needs the `derivedFields` block with
   `matcherType: label` — not a regex over the log body, because `trace_id`
   isn't in the body.

## Alerts never fire

```bash
curl -s localhost:9090/api/v1/rules | python3 -m json.tool | head -40
```

- **No rules listed** — Prometheus didn't load `configs/alerts.yml`. Check
  `rule_files` in `configs/prometheus.yml` and that the file is mounted.
- **Rules listed, state `inactive`** — the condition simply isn't true.
  `QueueBacklog` needs the queue above 200, which won't happen while the
  worker keeps up; starve it with `BATCH_SIZE=1` first.
- **Firing in Prometheus but nothing in Alertmanager** — check the
  `alerting` block in `configs/prometheus.yml` and that `alertmanager` is up.
- **Visible in Alertmanager, no notification** — expected. The default
  receiver has no integration on purpose. Uncomment `telegram_configs` in
  `configs/alertmanager.yml` and fill in your bot token and chat id.

To force one immediately:

```bash
docker compose stop worker
```

`WorkerDown` goes Pending, then Firing 30s later.

## Everything is slow

Ten containers is not much, but Docker Desktop ships with modest defaults.
Give it at least 4 GB of RAM in Settings → Resources.

To see who's actually eating the machine:

```bash
docker stats --no-stream
```

If you only need part of the stack, stop a branch — the others keep working:

```bash
docker compose stop otel-collector tempo
```

## Starting over

When the state is confusing enough that debugging costs more than a reset:

```bash
docker compose down -v --remove-orphans
```

```bash
docker compose up --build -d
```

This deletes the `pgdata` volume, so the job queue starts empty. Nothing else
in the stack keeps data worth saving.
