"""state.py — the ingestion's own ledger (`$SOKKAN_DATA_DIR/doc-ingest/docingest.db`).

* `files`     — incremental state per (source, path): size, mtime, sha256, what came of it.
* `passes` / `pass_events` — the journal of each pass (read, noted, abstained, errors,
                duration, tokens), one event per document.
* `batches` / `batch_notes` / `views` / `decisions` — the batch quarantine and the journal of
                every human (or policy) decision.
* `notes_fts` — a small lexical index of the notes this agent produced (pending or approved),
                used to find neighbours, duplicates and contradictions without depending on
                the memory store.

Only metadata and the agent's own notes live here: never the text of a source document.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    source TEXT NOT NULL, rel TEXT NOT NULL,
    size INTEGER NOT NULL, mtime REAL NOT NULL, sha256 TEXT NOT NULL DEFAULT '',
    text_sha TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '[]', pass_id INTEGER, doc_date TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL, gone_at REAL,
    PRIMARY KEY (source, rel)
);
CREATE INDEX IF NOT EXISTS ix_files_text ON files(text_sha);
CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, requested_by TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL, ended_at REAL, heartbeat REAL NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    batch_id INTEGER, counters TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pass_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pass_id INTEGER NOT NULL, ts REAL NOT NULL,
    rel TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_events_pass ON pass_events(pass_id);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, project TEXT NOT NULL, pass_id INTEGER,
    status TEXT NOT NULL, created_at REAL NOT NULL, closed_at REAL, decided_at REAL
);
CREATE TABLE IF NOT EXISTS batch_notes (
    batch_id INTEGER NOT NULL, name TEXT NOT NULL,
    type TEXT NOT NULL, level INTEGER NOT NULL, rel TEXT NOT NULL, file TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    flags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT NOT NULL DEFAULT '', decided_at REAL, reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (batch_id, name)
);
CREATE INDEX IF NOT EXISTS ix_bn_name ON batch_notes(name);
CREATE TABLE IF NOT EXISTS views (
    batch_id INTEGER NOT NULL, name TEXT NOT NULL, user TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (batch_id, name, user)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user TEXT NOT NULL,
    source TEXT NOT NULL, project TEXT NOT NULL DEFAULT 'default',
    batch_id INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_dec_batch ON decisions(batch_id);
CREATE TABLE IF NOT EXISTS schedule (
    source TEXT PRIMARY KEY, next_run_at REAL, last_run_at REAL
);
CREATE TABLE IF NOT EXISTS note_index (
    name TEXT NOT NULL, project TEXT NOT NULL, source TEXT NOT NULL, rel TEXT NOT NULL,
    type TEXT NOT NULL, level INTEGER NOT NULL, status TEXT NOT NULL,
    batch_id INTEGER, created_at REAL NOT NULL, title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project, name)
);
CREATE INDEX IF NOT EXISTS ix_ni_rel ON note_index(source, rel);
"""
_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
        "name UNINDEXED, project UNINDEXED, title, description, body, "
        "tokenize='unicode61 remove_diacritics 2')")

_lock = threading.RLock()
_ready: set[str] = set()


def db_path() -> Path:
    return config.home() / "docingest.db"


def con() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30)
    c.row_factory = sqlite3.Row
    if str(p) not in _ready:
        with _lock:
            c.executescript(_SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            try:
                c.execute(_FTS)
            except sqlite3.OperationalError:      # an sqlite without FTS5: neighbours degrade
                pass
            c.commit()
            _ready.add(str(p))
    return c


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False)


# ---- files (incremental state) -----------------------------------------------------------
def file_get(source: str, rel: str) -> dict | None:
    c = con()
    try:
        r = c.execute("SELECT * FROM files WHERE source=? AND rel=?", (source, rel)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def file_put(source: str, rel: str, *, size: int, mtime: float, sha256: str, status: str,
             detail: str = "", notes: list | None = None, pass_id: int | None = None,
             text_sha: str = "", doc_date: str = "", tokens_in: int = 0,
             tokens_out: int = 0) -> None:
    c = con()
    try:
        c.execute(
            "INSERT INTO files(source, rel, size, mtime, sha256, text_sha, status, detail, notes,"
            " pass_id, doc_date, tokens_in, tokens_out, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source, rel) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
            " sha256=excluded.sha256, text_sha=excluded.text_sha, status=excluded.status,"
            " detail=excluded.detail, notes=excluded.notes, pass_id=excluded.pass_id,"
            " doc_date=excluded.doc_date, tokens_in=excluded.tokens_in,"
            " tokens_out=excluded.tokens_out, updated_at=excluded.updated_at, gone_at=NULL",
            (source, rel, size, mtime, sha256, text_sha, status, detail[:500], _j(notes or []),
             pass_id, doc_date, tokens_in, tokens_out, time.time()))
        c.commit()
    finally:
        c.close()


def file_touch(source: str, rel: str, size: int, mtime: float) -> None:
    """Same content under a new date (a copy, a `touch`): remember the date, redo nothing."""
    c = con()
    try:
        c.execute("UPDATE files SET size=?, mtime=?, gone_at=NULL WHERE source=? AND rel=?",
                  (size, mtime, source, rel))
        c.commit()
    finally:
        c.close()


def files_of(source: str) -> dict[str, dict]:
    c = con()
    try:
        return {r["rel"]: dict(r) for r in c.execute("SELECT * FROM files WHERE source=?",
                                                     (source,))}
    finally:
        c.close()


def file_mark_gone(source: str, rels: list[str]) -> None:
    c = con()
    try:
        c.executemany("UPDATE files SET gone_at=? WHERE source=? AND rel=?",
                      [(time.time(), source, r) for r in rels])
        c.commit()
    finally:
        c.close()


def same_text(text_sha: str, source: str, rel: str) -> dict | None:
    """Another file, already read, with exactly the same extracted text."""
    if not text_sha:
        return None
    c = con()
    try:
        r = c.execute(
            "SELECT source, rel, notes, status FROM files WHERE text_sha=? AND NOT (source=? AND "
            "rel=?) AND status IN ('noted','abstained') ORDER BY updated_at LIMIT 1",
            (text_sha, source, rel)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


# ---- passes ---------------------------------------------------------------------------------
COUNTERS = ("listed", "unchanged", "read", "noted", "notes", "decisions", "abstained",
            "duplicates", "errors", "skipped", "gone", "flagged", "tokens_in", "tokens_out")
STALE_S = 600      # a `running` pass without heartbeat for this long is a dead process


def pass_open(source: str, project: str, kind: str, requested_by: str = "") -> dict:
    """The pass to work in: the unfinished one of this source if there is one (RESUME after an
    interruption — same pass, same batch, counters kept), else a new one. Raises if another
    process is alive on this source."""
    now = time.time()
    c = con()
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM passes WHERE source=? AND status IN ('running','interrupted')"
                      " ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        if r is not None:
            if r["status"] == "running" and now - r["heartbeat"] < STALE_S:
                c.rollback()
                raise RuntimeError(f"a pass of {source} is already running (#{r['id']})")
            c.execute("UPDATE passes SET status='running', heartbeat=?, error='' WHERE id=?",
                      (now, r["id"]))
            c.commit()
            out = dict(r) | {"status": "running", "resumed": True}
            out["counters"] = json.loads(out["counters"] or "{}")
            return out
        cur = c.execute(
            "INSERT INTO passes(source, project, kind, status, requested_by, started_at, heartbeat,"
            " counters) VALUES(?,?,?,?,?,?,?,?)",
            (source, project, kind, "running", requested_by, now, now,
             _j({k: 0 for k in COUNTERS})))
        c.commit()
        return {"id": cur.lastrowid, "source": source, "project": project, "kind": kind,
                "status": "running", "started_at": now, "batch_id": None, "seconds": 0.0,
                "counters": {k: 0 for k in COUNTERS}, "resumed": False}
    finally:
        c.close()


def pass_update(pass_id: int, **cols) -> None:
    if "counters" in cols:
        cols["counters"] = _j(cols["counters"])
    cols.setdefault("heartbeat", time.time())
    c = con()
    try:
        c.execute(f"UPDATE passes SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                  (*cols.values(), pass_id))
        c.commit()
    finally:
        c.close()


def pass_get(pass_id: int) -> dict | None:
    c = con()
    try:
        r = c.execute("SELECT * FROM passes WHERE id=?", (pass_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["counters"] = json.loads(d["counters"] or "{}")
        return d
    finally:
        c.close()


def pass_list(source: str | None = None, project: str | None = None, limit: int = 50
              ) -> list[dict]:
    c = con()
    try:
        where, args = [], []
        for k, v in (("source", source), ("project", project)):
            if v is not None:
                where.append(f"{k}=?")
                args.append(v)
        rows = c.execute("SELECT * FROM passes" + (" WHERE " + " AND ".join(where) if where
                         else "") + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["counters"] = json.loads(d["counters"] or "{}")
            out.append(d)
        return out
    finally:
        c.close()


def event(pass_id: int, rel: str, outcome: str, detail: str = "", tokens_in: int = 0,
          tokens_out: int = 0, seconds: float = 0.0) -> None:
    c = con()
    try:
        c.execute("INSERT INTO pass_events(pass_id, ts, rel, outcome, detail, tokens_in,"
                  " tokens_out, seconds) VALUES(?,?,?,?,?,?,?,?)",
                  (pass_id, time.time(), rel, outcome, detail[:500], tokens_in, tokens_out,
                   round(seconds, 3)))
        c.commit()
    finally:
        c.close()


def events(pass_id: int, outcome: str | None = None, limit: int = 500) -> list[dict]:
    c = con()
    try:
        q = "SELECT * FROM pass_events WHERE pass_id=?"
        args: list = [pass_id]
        if outcome:
            q += " AND outcome=?"
            args.append(outcome)
        return [dict(r) for r in c.execute(q + " ORDER BY id LIMIT ?", (*args, limit))]
    finally:
        c.close()


# ---- schedule -------------------------------------------------------------------------------
def schedule_get(source: str) -> dict | None:
    c = con()
    try:
        r = c.execute("SELECT * FROM schedule WHERE source=?", (source,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def schedule_set(source: str, next_run_at: float | None, last_run_at: float | None = None
                 ) -> None:
    c = con()
    try:
        c.execute("INSERT INTO schedule(source, next_run_at, last_run_at) VALUES(?,?,?) "
                  "ON CONFLICT(source) DO UPDATE SET next_run_at=excluded.next_run_at, "
                  "last_run_at=COALESCE(excluded.last_run_at, schedule.last_run_at)",
                  (source, next_run_at, last_run_at))
        c.commit()
    finally:
        c.close()


# ---- lexical index of the agent's own notes ------------------------------------------------
def index_note(*, name: str, project: str, source: str, rel: str, type: str, level: int,
               status: str, batch_id: int | None, title: str, description: str, body: str
               ) -> None:
    c = con()
    try:
        c.execute("DELETE FROM note_index WHERE project=? AND name=?", (project, name))
        c.execute(
            "INSERT INTO note_index(name, project, source, rel, type, level, status, batch_id,"
            " created_at, title, description, body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, project, source, rel, type, level, status, batch_id, time.time(), title,
             description, body))
        try:
            c.execute("DELETE FROM notes_fts WHERE name=? AND project=?", (name, project))
            c.execute("INSERT INTO notes_fts(name, project, title, description, body) "
                      "VALUES(?,?,?,?,?)", (name, project, title, description, body))
        except sqlite3.OperationalError:
            pass
        c.commit()
    finally:
        c.close()


def index_set_status(project: str, name: str, status: str) -> None:
    c = con()
    try:
        if status == "rejected":
            # a refused note is no neighbour of anything: out of the index
            c.execute("DELETE FROM note_index WHERE project=? AND name=?", (project, name))
            try:
                c.execute("DELETE FROM notes_fts WHERE name=? AND project=?", (name, project))
            except sqlite3.OperationalError:
                pass
        else:
            c.execute("UPDATE note_index SET status=? WHERE project=? AND name=?",
                      (status, project, name))
        c.commit()
    finally:
        c.close()


def index_get(project: str, name: str) -> dict | None:
    c = con()
    try:
        r = c.execute("SELECT * FROM note_index WHERE project=? AND name=?",
                      (project, name)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def index_names(project: str) -> set[str]:
    c = con()
    try:
        return {r[0] for r in c.execute("SELECT name FROM note_index WHERE project=?",
                                        (project,))}
    finally:
        c.close()


def index_of_file(source: str, rel: str) -> list[dict]:
    c = con()
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM note_index WHERE source=? AND rel=?", (source, rel))]
    finally:
        c.close()


def _fts_query(text: str, max_terms: int = 64) -> str:
    import re
    seen, out = set(), []
    for w in re.findall(r"[^\W\d_]{4,}|\d{2,}", text.lower()):
        if w not in seen and w not in _STOP:
            seen.add(w)
            out.append(w)
        if len(out) >= max_terms:
            break
    return " OR ".join(f'"{w}"' for w in out)


_STOP = frozenset("""avec dans pour cette sont plus tout tous leur leurs nous vous elle elles
comme mais donc ainsi être avoir fait faire entre aussi sans sous chez dont
that this with from have will were been their there which would should about what when
dass nicht eine einer eines oder auch wird sind sich""".split())


def neighbours(project: str, text: str, max_level: int, limit: int = 6,
               exclude_rel: tuple[str, str] | None = None, since: float | None = None,
               same_source: str | None = None) -> list[dict]:
    """Notes of this agent closest to `text` (BM25, title and description weighted), at or
    below `max_level`: a document is only shown neighbours at or below its own source level,
    so the notes derived from it cannot carry down what a more classified note says.
    `same_source`: notes derived from that SAME source are returned whatever their level (the
    model raised them above the folder's floor) — the caller then makes the new notes inherit
    the highest level it showed."""
    q = _fts_query(text)
    if not q:
        return []
    c = con()
    try:
        try:
            rows = c.execute(
                "SELECT n.*, bm25(notes_fts, 0, 0, 6.0, 3.0, 1.0) AS rank FROM notes_fts "
                "JOIN note_index n ON n.name = notes_fts.name AND n.project = notes_fts.project "
                "WHERE notes_fts MATCH ? AND notes_fts.project = ? AND (n.level <= ? OR "
                "n.source = ?) ORDER BY rank LIMIT ?",
                (q, project, max_level, same_source or "", limit * 3)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            if exclude_rel and (r["source"], r["rel"]) == exclude_rel:
                continue
            if since is not None and r["created_at"] < since:
                continue
            out.append(dict(r))
            if len(out) >= limit:
                break
        return out
    finally:
        c.close()
