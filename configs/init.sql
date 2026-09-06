CREATE TABLE IF NOT EXISTS jobs (
    id         SERIAL PRIMARY KEY,
    payload    TEXT        NOT NULL,
    result     TEXT,
    status     TEXT        NOT NULL DEFAULT 'pending',
    attempts   INTEGER     NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- (status, id) rather than (status) alone: the claim query filters on status
-- and orders by id, so the composite index answers it without a sort and
-- stays fast however large the table gets.
CREATE INDEX IF NOT EXISTS jobs_status_id_idx ON jobs (status, id);

-- A backlog of finished jobs, so the table is the size a real one would be
-- after running for a while. Nothing ever reads these rows: they are here so
-- that a query which forgets its WHERE clause has something to be slow about.
-- Takes ten to twenty seconds on first start; the volume keeps it afterwards.
INSERT INTO jobs (payload, status, result)
SELECT 'seed-' || g, 'done', md5(g::text)
  FROM generate_series(1, 800000) AS g;

ANALYZE jobs;
