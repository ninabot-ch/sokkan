-- SOKKAN 3.0 memory store — schema version 1 (Postgres 16 + pgvector >= 0.7).
--
-- The .md notes stay the source of truth: notes / chunks / links / lex_df are an index that
-- can be rebuilt from the files. note_versions and recall_log are HISTORY and cannot: back
-- them up with the database.
--
-- Embedding dimension per generation. An index generation = one embedding model (identity +
-- dimension). Two generations of different dimensions must coexist while a new one is
-- built in the background (P0-1). `chunks` is therefore LIST-partitioned by generation:
--   * the parent declares `embedding halfvec` WITHOUT a dimension, so every partition can
--     hold its own dimension, enforced by a CHECK (vector_dims) added on the partition;
--   * each partition gets its own HNSW index on the expression
--     `(embedding::halfvec(<dim>))`, which is how pgvector indexes a dimension-less column;
--   * retiring a generation is DETACH + DROP of its partition: instant, no 250 000-row
--     DELETE, no bloat to vacuum, the other generation's index is never touched.
-- (The alternative, one table + partial expression indexes `WHERE generation_id = n`, works
-- too, but deleting a retired generation then costs a mass DELETE + VACUUM on the table the
-- live search is reading.) Partitions are created by Store.create_generation(), not here.
--
-- Migrations: this file is version 1. Later changes go in migrations/NNNN_<name>.sql
-- (NNNN > 0001), applied in order by Store.migrate() and recorded in schema_migrations.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- One embedding model = one generation. At most one is `active` (served by default);
-- `building` is being filled in the background; `retired` stays searchable on demand
-- (rollback window, 7 days by default) until purged.
CREATE TABLE IF NOT EXISTS index_generations (
    id              serial PRIMARY KEY,
    embed_identity  text NOT NULL,
    dim             integer NOT NULL CHECK (dim BETWEEN 1 AND 4000),  -- halfvec HNSW limit
    status          text NOT NULL DEFAULT 'building'
                    CHECK (status IN ('building', 'active', 'retired')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    activated_at    timestamptz,
    retired_at      timestamptz,
    chunk_count     bigint NOT NULL DEFAULT 0,
    hnsw_m          integer NOT NULL DEFAULT 16,
    hnsw_ef_construction integer NOT NULL DEFAULT 64,
    index_built     boolean NOT NULL DEFAULT false,
    -- dense/lexical blend suited to this model (NULL = default of its model family)
    lexical_weight  double precision CHECK (lexical_weight BETWEEN 0 AND 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS index_generations_one_active
    ON index_generations ((true)) WHERE status = 'active';

-- Notes: generation-independent (the text does not depend on the model).
-- head_tokens / lex_tokens are the folded keyword sets of the lexical score
-- (head = name + description ; lex = name + description + body), see search.py.
CREATE TABLE IF NOT EXISTS notes (
    id              bigserial PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    description     text NOT NULL DEFAULT '',
    type            text,
    priority        smallint NOT NULL DEFAULT 0,
    source_path     text,
    modified        text,           -- ISO 8601, kept verbatim (effective date, see truth)
    modified_source text,
    body            text NOT NULL DEFAULT '',
    head_tokens     text[] NOT NULL DEFAULT '{}',
    lex_tokens      text[] NOT NULL DEFAULT '{}',
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notes_lex_tokens_gin ON notes USING gin (lex_tokens);
CREATE INDEX IF NOT EXISTS notes_source_path ON notes (source_path);

-- Document frequency of each folded keyword over the notes (IDF), maintained in the same
-- transaction as notes. The empty token '' holds the number of notes.
CREATE TABLE IF NOT EXISTS lex_df (
    token   text PRIMARY KEY,
    df      integer NOT NULL
);

-- Content history of a note (contract.SeenRecord): one row per version as the indexer saw it.
-- A version is appended when the body hash OR the description changes, else the latest row
-- is refreshed. first_seen = when this BODY was first seen (carried across description-only
-- changes and renames), date_source = its provenance. Description and body are kept so the
-- "description != body" drift check can compare versions. Keyed by NAME, not notes.id: the
-- history survives a deletion, and a renamed note finds its date by content_hash.
-- Dates are ISO 8601 text, kept verbatim as the truth code wrote them.
CREATE TABLE IF NOT EXISTS note_versions (
    id            bigserial PRIMARY KEY,
    note_name     text NOT NULL,
    content_hash  text NOT NULL,
    first_seen    text NOT NULL,
    seeded        boolean NOT NULL DEFAULT false,
    date_source   text NOT NULL DEFAULT 'indexed' CHECK (date_source IN
                  ('frontmatter', 'indexed', 'migrated-mtime', 'inferred', 'transcript',
                   'inconnue')),
    description   text NOT NULL DEFAULT '',
    body          text NOT NULL DEFAULT '',
    seen_at       text NOT NULL DEFAULT '',
    last_seen     timestamptz NOT NULL DEFAULT now(),   -- last refresh of this version
    extra         jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS note_versions_name ON note_versions (note_name, id DESC);
CREATE INDEX IF NOT EXISTS note_versions_hash ON note_versions (content_hash);

-- Chunks, one partition per generation (see header).
-- tsv = the chunk's folded keywords (array_to_tsvector of search.tokens), so that the
-- lexical-only mode and the snippet choice use the same folding as the ranking.
CREATE TABLE IF NOT EXISTS chunks (
    generation_id  integer NOT NULL REFERENCES index_generations(id),
    note_id        bigint  NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    idx            integer NOT NULL,
    body           text    NOT NULL,
    -- PLAIN: never TOASTed. A 768-d halfvec is 1.5 kB; out-of-line storage would cost a
    -- TOAST lookup per row in every exact scan.
    embedding      halfvec STORAGE PLAIN NOT NULL,
    tsv            tsvector NOT NULL,
    PRIMARY KEY (generation_id, note_id, idx)
) PARTITION BY LIST (generation_id);
CREATE INDEX IF NOT EXISTS chunks_note ON chunks (note_id);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);

-- [[wikilinks]] between notes, by name (a link may point to a note that does not exist).
CREATE TABLE IF NOT EXISTS links (
    src  text NOT NULL,
    dst  text NOT NULL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX IF NOT EXISTS links_dst ON links (dst);

-- What was injected where (P0-3): which session / sub-agent received which note, in which
-- version, from which generation. Basis of the recall audit (P1-6).
CREATE TABLE IF NOT EXISTS recall_log (
    id             bigserial PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    channel        text NOT NULL,          -- prompt | subagent | spawn | search
    session_id     text,
    agent_id       text,
    query          text,
    note_name      text NOT NULL,
    rank           integer,
    score          real,
    rerank         real,
    content_hash   text,                  -- version of the note that was injected
    generation_id  integer
);
CREATE INDEX IF NOT EXISTS recall_log_session ON recall_log (session_id, at);
CREATE INDEX IF NOT EXISTS recall_log_note ON recall_log (note_name, at);
