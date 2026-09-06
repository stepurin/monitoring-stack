# The seeded bug

This repository ships one deliberate bug. It exists so a workshop can show
the whole point of the three signals: a problem that logs alone cannot
explain, found by following metric → log → trace → SQL, and then fixed.

**This file is the presenter's script. It gives the answer away.**

## The bug

`count_pending()` in `app/my_service.py` feeds the `job_queue_depth` gauge:

```python
def count_pending(connection) -> int:
    """How many jobs are still waiting. Feeds the job_queue_depth gauge."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT status FROM jobs")
        return sum(1 for (status,) in cursor.fetchall() if status == "pending")
```

It pulls **every row in the table** over the wire and counts in Python. The
filter belongs in the query. Written this way it does not just waste effort —
it cannot use the index, and it gets slower every minute the service runs,
because finished jobs accumulate in the same table.

`configs/init.sql` seeds 800 000 finished jobs at first start, so the table is
the size a real one would be after a few weeks. Without that seed the bug is
invisible: on an empty table a full scan is instant, which is exactly why this
kind of code survives review and then falls over in production.

## What it does to the numbers

Rough figures on a laptop — treat them as the shape of the problem, not a
benchmark:

| | healthy | with the bug |
|---|---|---|
| `SELECT` for the gauge | under 1 ms | ~1 s |
| one tick | ~1.75 s | ~2.9 s |
| full cycle (tick + `TICK_SECONDS`) | ~2.75 s | ~3.9 s |
| jobs finished per second | ~3.6 | ~2.5 |
| jobs produced per second | ~3 | ~3 |
| queue | flat, near zero | climbs ~30/min |

The important part is what stays normal: `job_duration_seconds` does not move,
the failure rate stays at its usual 5%, and **not one line of ERROR appears in
the logs**. Nothing is broken. It is only slow, and slow in a place nobody
logged.

## Running the investigation

Give it three or four minutes after `docker compose up -d` before starting —
the queue needs time to visibly climb.

### 1. The symptom is a metric

Grafana → **my_service — metrics**.

`job_queue_depth` climbs in a straight line. Next to it, `Throughput` shows
produced above done — the service is falling behind. And `Job duration`
percentiles are flat, which already rules out the obvious suspect: the jobs
themselves have not got slower.

Say the question out loud: *the work is the same speed, so where is the extra
time going?*

### 2. The logs say nothing

Grafana → **Logs — all containers**, filter to `my_service`.

`processed 10 jobs`, over and over. The occasional `job N failed` is the
built-in 5% failure rate and has nothing to do with this. There is no error,
no warning, no stack trace. This is the moment to make the point: a log tells
you what the code decided to tell you, and nobody writes `logger.warning("this
query is slower than it should be")`.

### 3. The trace shows where the time went

Grafana → **Traces — my_service** → **Slow ticks (> 1s)**, open one.

The waterfall answers the question:

```
tick ─────────────────────────────────────────  2.9 s
  UPDATE jobs … claim                            2 ms
  process-job × 10                               1.75 s
  SELECT ██████████████████████                  1.0 s   ← here
```

One span is eating a third of every tick. Click it and read `db.statement`:

```sql
SELECT status FROM jobs
```

No `WHERE`. No `count()`. It selects the entire table, every two seconds.

### 4. The fix

In `app/my_service.py`:

```python
 def count_pending(connection) -> int:
     with connection.cursor() as cursor:
-        cursor.execute("SELECT status FROM jobs")
-        return sum(1 for (status,) in cursor.fetchall() if status == "pending")
+        cursor.execute("SELECT count(*) FROM jobs WHERE status = 'pending'")
+        return cursor.fetchone()[0]
```

Two lines. The filter moves into the query, so Postgres answers it from
`jobs_status_id_idx` instead of reading 800 000 rows, and returns one number
instead of a million.

```bash
docker compose up -d --build my_service
```

### 5. The payoff

Back on the metrics dashboard, within a minute:

- the `SELECT` span drops from ~1 s to under 1 ms
- tick duration falls back to the cost of the actual work
- throughput crosses above the production rate
- the queue turns around and drains

Let it drain on screen. The line coming back down is the end of the story.

## Re-arming it

To run the demo again, put the two lines back and rebuild. The seeded rows
live in the `pgdata` volume, so they survive restarts; `docker compose down -v`
wipes them and the next start re-seeds (ten to twenty seconds).

## Why this bug and not another

It had to be findable *only* by combining signals, and it had to end somewhere
concrete:

- an exception would have shown up in the logs, and the trace would be decoration
- a slow HTTP dependency would be honest but would not touch the database
- an infrastructure change (a dropped index) is not a bug in the code, and the
  fix would be a `psql` command rather than a diff

A missing `WHERE` is none of those. It is a mistake a person actually makes, it
is invisible until the table grows, and the span text names it exactly.

One caveat worth saying out loud during the talk: this example ends at the
database, but that is a property of this bug, not of tracing. The same path —
metric, log, trace, span — lands wherever the time actually goes.
