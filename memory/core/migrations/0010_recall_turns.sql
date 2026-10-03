-- Recall at every turn (P0-3): one row per recall attempt, including the ones that
-- injected nothing (below threshold, already injected, skipped prompt). recall_log only
-- holds the notes that WERE injected; this table answers "which share of the turns got a
-- recall" and "how long did the hook take", per profile.
CREATE TABLE IF NOT EXISTS recall_turns (
    id             bigserial PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    channel        text NOT NULL,          -- prompt | subagent | spawn
    session_id     text,
    agent_id       text,
    query          text,
    candidates     integer NOT NULL DEFAULT 0,   -- hits returned by the search
    injected       integer NOT NULL DEFAULT 0,   -- notes injected in this turn
    deduplicated   integer NOT NULL DEFAULT 0,   -- relevant notes skipped: already injected
    skipped        text,                         -- why nothing was searched (short prompt…)
    degraded       boolean NOT NULL DEFAULT false,
    reranked       boolean NOT NULL DEFAULT false,
    latency_ms     integer,
    threshold      real,
    profile        text,
    generation_id  integer
);
CREATE INDEX IF NOT EXISTS recall_turns_at ON recall_turns (at);
CREATE INDEX IF NOT EXISTS recall_turns_session ON recall_turns (session_id, at);
