-- SOKKAN 3.2 lot 3: note names are unique PER PROJECT (decision of 07.10.2026). With names
-- unique per instance, a refused write ("name already used") would reveal that a note of
-- that name exists in a project the writer cannot see. links / note_versions / recall_log
-- carry the project too; existing rows go to 'default' (recall_log: NULL = default).
ALTER TABLE notes DROP CONSTRAINT IF EXISTS notes_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS notes_project_name ON notes (project, name);
CREATE INDEX IF NOT EXISTS notes_name ON notes (name);

ALTER TABLE links ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'default';
ALTER TABLE links DROP CONSTRAINT IF EXISTS links_pkey;
ALTER TABLE links ADD PRIMARY KEY (project, src, dst);
DROP INDEX IF EXISTS links_dst;
CREATE INDEX IF NOT EXISTS links_project_dst ON links (project, dst);

ALTER TABLE note_versions ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS note_versions_project_name ON note_versions (project, note_name, id DESC);

ALTER TABLE recall_log ADD COLUMN IF NOT EXISTS project text;
CREATE INDEX IF NOT EXISTS recall_log_project ON recall_log (project, at);
