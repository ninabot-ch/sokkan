-- Recall bench on the client's own corpus (P0-6) and profile switches (P0-1 b).
-- Numbered after the chantier (P0-6) so that parallel migrations do not collide.
--
-- Questions, runs and switches are HISTORY (like note_versions): they cannot be rebuilt
-- from the .md files. They reference generations by id WITHOUT a foreign key, so that the
-- results of a purged generation stay readable.

-- A question = what someone asked, and the note(s) that answer it.
--   transcript  harvested: a real prompt followed by memory_get(note) in the same session
--   client      written by a person (form, CLI)
--   generated   paraphrased from a note description by the instance's LLM: flatters the
--               model (it read the note), hence a reduced weight
CREATE TABLE IF NOT EXISTS eval_questions (
    id            bigserial PRIMARY KEY,
    question      text NOT NULL,
    expected      text[] NOT NULL CHECK (cardinality(expected) BETWEEN 1 AND 10),
    source        text NOT NULL CHECK (source IN ('transcript', 'client', 'generated')),
    weight        real NOT NULL DEFAULT 1 CHECK (weight > 0 AND weight <= 1),
    -- sha1 of source + folded question: the same prompt seen in two sessions is one question
    fingerprint   text NOT NULL UNIQUE,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    author        text,
    session_id    text,
    seen          integer NOT NULL DEFAULT 1,      -- times harvested
    origin        jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS eval_questions_source ON eval_questions (source, status);

-- One pass of the bench over one generation with one embedder (= one profile).
CREATE TABLE IF NOT EXISTS eval_runs (
    id              bigserial PRIMARY KEY,
    generation_id   integer,
    embed_identity  text,
    profile         text,
    trigger         text NOT NULL DEFAULT 'manual',   -- manual | nightly | switch | cli
    status          text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'done', 'failed')),
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    n_questions     integer NOT NULL DEFAULT 0,
    metrics         jsonb NOT NULL DEFAULT '{}',      -- overall + by source
    config          jsonb NOT NULL DEFAULT '{}',      -- k, rerank, weights
    regression      jsonb,                            -- set when a regression was detected
    error           text
);
CREATE INDEX IF NOT EXISTS eval_runs_generation ON eval_runs (generation_id, id DESC);

CREATE TABLE IF NOT EXISTS eval_results (
    run_id       bigint NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    question_id  bigint NOT NULL,
    weight       real NOT NULL,
    rank         integer,                -- rank of the first expected note, NULL = missed
    ranked       text[] NOT NULL,        -- top 10 returned
    relevant     text[] NOT NULL,        -- expected notes that existed at run time
    ndcg10       real NOT NULL,
    PRIMARY KEY (run_id, question_id)
);

-- Small key/value state of the bench (last harvest, …).
CREATE TABLE IF NOT EXISTS eval_state (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- How each generation is served: memory profile, embedding model and servers. A profile
-- change that keeps the same model only changes this row; a model change builds a new
-- generation.
CREATE TABLE IF NOT EXISTS generation_profiles (
    generation_id  integer PRIMARY KEY REFERENCES index_generations(id) ON DELETE CASCADE,
    profile        text NOT NULL,
    target         jsonb NOT NULL DEFAULT '{}',
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- A profile change (or a rollback), built in the background and gated by the bench.
CREATE TABLE IF NOT EXISTS index_switches (
    id               serial PRIMARY KEY,
    kind             text NOT NULL DEFAULT 'switch' CHECK (kind IN ('switch', 'rollback')),
    status           text NOT NULL DEFAULT 'building' CHECK (status IN
                     ('building', 'evaluating', 'switched', 'blocked', 'failed', 'cancelled',
                      'rolled_back')),
    phase            text,                        -- human-readable step
    progress         real NOT NULL DEFAULT 0,     -- 0..1
    detail           text,
    from_generation  integer,
    from_target      jsonb,
    to_generation    integer,
    to_target        jsonb NOT NULL,
    built            boolean NOT NULL DEFAULT false,  -- a new generation was built
    baseline_run     bigint,
    candidate_run    bigint,
    max_drop         real,
    comparison       jsonb,
    requested_by     text,
    decided_by       text,
    started_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS index_switches_one_running
    ON index_switches ((true)) WHERE status IN ('building', 'evaluating');
