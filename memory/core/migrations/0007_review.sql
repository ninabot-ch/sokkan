-- CortHeXis review (P0-4): health history, problems tracked over time, alert state.
-- History, like note_versions: back it up with the database.

CREATE TABLE IF NOT EXISTS review_runs (
    at         timestamptz PRIMARY KEY,
    score      smallint NOT NULL CHECK (score BETWEEN 0 AND 100),
    crit       integer NOT NULL DEFAULT 0,
    warn       integer NOT NULL DEFAULT 0,
    info       integer NOT NULL DEFAULT 0,
    signature  text NOT NULL DEFAULT '',
    notes      integer NOT NULL DEFAULT 0,
    report     jsonb NOT NULL
);

-- One row per (check, note) problem: when it was first and last seen, when it went away.
-- Gives "open for more than 7 days" and the mean time to fix.
CREATE TABLE IF NOT EXISTS review_problems (
    key          text PRIMARY KEY,          -- "<check>:<note>"
    check_id     text NOT NULL,
    note         text NOT NULL,
    severity     text NOT NULL,
    first_seen   timestamptz NOT NULL,
    last_seen    timestamptz NOT NULL,
    resolved_at  timestamptz
);
CREATE INDEX IF NOT EXISTS review_problems_open ON review_problems (resolved_at)
    WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS review_state (
    key         text PRIMARY KEY,
    value       text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
