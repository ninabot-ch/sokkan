-- SOKKAN 3.4 « classification »: a note carries a level (0 public, 1 team, 2 project,
-- 3 confidential, 4 restricted — core/levels.py). Every existing note is at 2 (project),
-- the default: the migration changes what nobody sees. A floor per note keeps a derived or
-- reclassified note from going below its level by an edit of its file; the access log is
-- the audited recall (who obtained which note, through which path).
ALTER TABLE notes ADD COLUMN IF NOT EXISTS level smallint NOT NULL DEFAULT 2;
CREATE INDEX IF NOT EXISTS notes_project_level ON notes (project, level);

CREATE TABLE IF NOT EXISTS note_level_floor (
    project text NOT NULL,
    name text NOT NULL,
    level smallint NOT NULL,
    reason text NOT NULL DEFAULT '',
    set_by text NOT NULL DEFAULT '',
    set_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, name)
);

ALTER TABLE recall_log ADD COLUMN IF NOT EXISTS level smallint;

CREATE TABLE IF NOT EXISTS note_access (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    via text NOT NULL,
    actor text,
    session_id text,
    project text NOT NULL,
    note_name text NOT NULL,
    level smallint NOT NULL DEFAULT 2,
    query text
);
CREATE INDEX IF NOT EXISTS note_access_project_at ON note_access (project, at);
CREATE INDEX IF NOT EXISTS note_access_session ON note_access (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS note_access_actor ON note_access (actor, at);
