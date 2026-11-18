"""store.py — SQLite store of the councils ($SOKKAN_DATA_DIR/council.db).

councils   one row per council: project, owner, brief, state, budget and totals, card, pii map
documents  what the CEO attached (a text copy, or a reference to a project document)
turns      the journal: every speech, timestamped, with role, model, tokens, duration
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB = Path(os.environ.get("SOKKAN_COUNCIL_DB") or os.path.join(      # compose passes "" when unset
    os.environ.get("SOKKAN_DATA_DIR") or os.path.expanduser("~/.local/share/sokkan"), "council.db"))

STATES = ("brief", "round1", "round2", "synthesis", "card", "interrupted", "accepted", "refused",
          "stopped", "stopped_budget", "failed")
LIVE = ("round1", "round2", "synthesis")           # states in which the council is working
# `interrupted`: the worker is gone (API restarted, process killed) — not final: the CEO resumes
# the round that was running (`resume_step`) or stops the council.
FINAL = ("accepted", "refused", "stopped", "stopped_budget", "failed")

_lock = threading.RLock()
_ready_for: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS councils(
  id TEXT PRIMARY KEY, project TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
  brief TEXT NOT NULL DEFAULT '', lang TEXT NOT NULL DEFAULT 'fr', state TEXT NOT NULL DEFAULT 'brief',
  level INTEGER NOT NULL DEFAULT 4, claude INTEGER NOT NULL DEFAULT 0, claude_scope TEXT NOT NULL DEFAULT 'daf',
  claude_model TEXT NOT NULL DEFAULT '', pii_ack INTEGER NOT NULL DEFAULT 0, pii_map TEXT NOT NULL DEFAULT '{}',
  max_tokens INTEGER NOT NULL, max_cost_chf REAL NOT NULL,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost_chf REAL NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0, refusals INTEGER NOT NULL DEFAULT 0, refusal_reason TEXT NOT NULL DEFAULT '',
  stop_requested INTEGER NOT NULL DEFAULT 0, speaking TEXT NOT NULL DEFAULT '[]', stop_reason TEXT NOT NULL DEFAULT '', stop_message TEXT NOT NULL DEFAULT '',
  card TEXT, findings TEXT NOT NULL DEFAULT '[]', accepted TEXT, positions TEXT NOT NULL DEFAULT '[]',
  resume_step TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, council_id TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL,
  source_doc_id TEXT NOT NULL DEFAULT '', added_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, council_id TEXT NOT NULL, round INTEGER NOT NULL, seq INTEGER NOT NULL,
  ts REAL NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', addressed_to TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '', route TEXT NOT NULL DEFAULT '', tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0, ms INTEGER NOT NULL DEFAULT 0, cost_chf REAL NOT NULL DEFAULT 0,
  text TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS turns_c ON turns(council_id, round, seq, id);
CREATE INDEX IF NOT EXISTS docs_c ON documents(council_id);
"""
_JSON = ("card", "findings", "accepted", "positions", "pii_map", "speaking", "plan")
_ADDED = (("resume_step", "TEXT NOT NULL DEFAULT ''"), ("plan", "TEXT NOT NULL DEFAULT '{}'"))   # councils of 4.1 alpha


def _con() -> sqlite3.Connection:
    global _ready_for
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    if _ready_for != DB:
        with _lock:
            con.executescript(SCHEMA)
            have = {r["name"] for r in con.execute("PRAGMA table_info(councils)")}
            for col, decl in _ADDED:
                if col not in have:
                    con.execute(f"ALTER TABLE councils ADD COLUMN {col} {decl}")
            con.commit()
            _ready_for = DB
    return con


def _out(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in _JSON:
        if k in d and d[k] is not None:
            d[k] = json.loads(d[k])
    d["claude"] = bool(d["claude"])
    d["pii_ack"] = bool(d["pii_ack"])
    d["stop_requested"] = bool(d["stop_requested"])
    return d


def create(*, project: str, owner: str, title: str, brief: str, lang: str, level: int,
           max_tokens: int, max_cost_chf: float, claude: bool = False, claude_scope: str = "daf",
           claude_model: str = "", pii_ack: bool = False) -> dict:
    cid = "cn-" + uuid.uuid4().hex[:12]
    now = time.time()
    con = _con()
    try:
        con.execute("INSERT INTO councils(id, project, owner, title, brief, lang, level, claude, claude_scope,"
                    " claude_model, pii_ack, max_tokens, max_cost_chf, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, project, owner, title, brief, lang, level, int(claude), claude_scope, claude_model,
                     int(pii_ack), max_tokens, max_cost_chf, now, now))
        con.commit()
    finally:
        con.close()
    return get(cid)


def get(cid: str) -> dict | None:
    con = _con()
    try:
        return _out(con.execute("SELECT * FROM councils WHERE id=?", (cid,)).fetchone())
    finally:
        con.close()


def list_project(project: str) -> list[dict]:
    con = _con()
    try:
        rows = con.execute("SELECT * FROM councils WHERE project=? ORDER BY created_at DESC", (project,)).fetchall()
        out = []
        for r in rows:
            d = _out(r)
            d.pop("pii_map", None)
            out.append(d)
        return out
    finally:
        con.close()


def update(cid: str, **cols) -> dict:
    cols["updated_at"] = time.time()
    vals = [json.dumps(v, ensure_ascii=False) if k in _JSON and v is not None else v for k, v in cols.items()]
    con = _con()
    try:
        con.execute(f"UPDATE councils SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*vals, cid))
        con.commit()
    finally:
        con.close()
    return get(cid)


def claim(cid: str) -> bool:
    """brief → round1, once (the row that changed is ours)."""
    con = _con()
    try:
        cur = con.execute("UPDATE councils SET state='round1', updated_at=? WHERE id=? AND state='brief'", (time.time(), cid))
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def live_ids() -> list[str]:
    con = _con()
    try:
        q = ",".join("?" * len(LIVE))
        return [r["id"] for r in con.execute(f"SELECT id FROM councils WHERE state IN ({q})", LIVE)]
    finally:
        con.close()


def set_speaking(cid: str, role: str, on: bool) -> None:
    """Who is speaking now (at most 2 in round 1): the room's « speaks » indicator."""
    with _lock:
        c = get(cid)
        cur = [r for r in c["speaking"] if r != role]
        update(cid, speaking=cur + ([role] if on else []))


def add_usage(cid: str, tokens_in: int, tokens_out: int, cost_chf: float) -> dict:
    """Atomic: two calls in flight add their usage without losing one."""
    con = _con()
    try:
        con.execute("UPDATE councils SET tokens_in=tokens_in+?, tokens_out=tokens_out+?, cost_chf=cost_chf+?,"
                    " calls=calls+1, updated_at=? WHERE id=?", (tokens_in, tokens_out, cost_chf, time.time(), cid))
        con.commit()
    finally:
        con.close()
    return get(cid)


# ---- documents ---------------------------------------------------------------------------
def add_document(cid: str, name: str, text: str, source_doc_id: str = "") -> dict:
    con = _con()
    try:
        cur = con.execute("INSERT INTO documents(council_id, name, text, source_doc_id, added_at) VALUES(?,?,?,?,?)",
                          (cid, name, text, source_doc_id, time.time()))
        con.commit()
        return {"id": cur.lastrowid, "name": name, "chars": len(text), "source_doc_id": source_doc_id}
    finally:
        con.close()


def documents(cid: str, with_text: bool = True) -> list[dict]:
    con = _con()
    try:
        rows = con.execute("SELECT * FROM documents WHERE council_id=? ORDER BY id", (cid,)).fetchall()
        return [{"id": r["id"], "name": r["name"], "chars": len(r["text"]), "source_doc_id": r["source_doc_id"],
                 **({"text": r["text"]} if with_text else {})} for r in rows]
    finally:
        con.close()


# ---- journal -----------------------------------------------------------------------------
def add_turn(cid: str, *, round: int, seq: int, kind: str, role: str = "", text: str = "", addressed_to: str = "",
             model: str = "", route: str = "", tokens_in: int = 0, tokens_out: int = 0, ms: int = 0,
             cost_chf: float = 0.0) -> dict:
    con = _con()
    try:
        cur = con.execute("INSERT INTO turns(council_id, round, seq, ts, kind, role, addressed_to, model, route,"
                          " tokens_in, tokens_out, ms, cost_chf, text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, round, seq, time.time(), kind, role, addressed_to, model, route, tokens_in,
                           tokens_out, ms, cost_chf, text))
        con.commit()
        return _turn(con.execute("SELECT * FROM turns WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        con.close()


def set_turn_text(turn_id: int, text: str) -> None:
    """The readable text of a turn (the DAF's JSON, shown as words)."""
    con = _con()
    try:
        con.execute("UPDATE turns SET text=? WHERE id=?", (text, turn_id))
        con.commit()
    finally:
        con.close()


def transition(cid: str, frm: tuple[str, ...] | str, to: str, **cols) -> bool:
    """frm → to, once: the row that changed is the caller's (two resumes cannot both win)."""
    frm = (frm,) if isinstance(frm, str) else tuple(frm)
    cols = {"state": to, **cols, "updated_at": time.time()}
    vals = [json.dumps(v, ensure_ascii=False) if k in _JSON and v is not None else v for k, v in cols.items()]
    con = _con()
    try:
        cur = con.execute(f"UPDATE councils SET {', '.join(f'{k}=?' for k in cols)} "
                          f"WHERE id=? AND state IN ({','.join('?' * len(frm))})", (*vals, cid, *frm))
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def _turn(r: sqlite3.Row) -> dict:
    return dict(r)


def turns(cid: str, after: int = 0) -> list[dict]:
    """The journal in reading order: by round, then by the order the roles speak (DAF → coach),
    CEO messages at their moment (seq 0 of the round in progress comes first within it)."""
    con = _con()
    try:
        rows = con.execute("SELECT * FROM turns WHERE council_id=? AND id>? ORDER BY round, seq, id", (cid, after)).fetchall()
        return [_turn(r) for r in rows]
    finally:
        con.close()


def journal_for_prompt(cid: str) -> list[dict]:
    """Every turn so far in the order they happened (what a role that starts now has heard)."""
    con = _con()
    try:
        return [_turn(r) for r in con.execute("SELECT * FROM turns WHERE council_id=? ORDER BY id", (cid,)).fetchall()]
    finally:
        con.close()
