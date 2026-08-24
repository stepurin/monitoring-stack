CREATE TABLE IF NOT EXISTS jobs (
    id         SERIAL PRIMARY KEY,
    payload    TEXT        NOT NULL,
    result     TEXT,
    status     TEXT        NOT NULL DEFAULT 'pending',
    attempts   INTEGER     NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The worker claims jobs by status on every tick.
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
