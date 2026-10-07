-- SOKKAN 3.2 multi-user: every note belongs to one project. Existing notes (and every note of
-- a single-project install) go to 'default': nothing is moved, nothing is lost, a search
-- without a project scope is unchanged. A search WITH a scope only reads the notes of the
-- projects in it (core.scope, Store.search(projects=...)).
ALTER TABLE notes ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS notes_project ON notes (project);
