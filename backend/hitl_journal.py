#!/usr/bin/env python3
"""hitl_journal.py — SOKKAN: the journal of human validations (always on, no feature flag).

Every approval a session asks for and every decision it gets is written durably: tool
permissions (allow / deny / allow with an edited input), answers to a question the agent
asked, changes of approval mode, and the approvals nobody clicked (auto-approved by the
session's mode, refused by an agent's policy or by the sandbox) — told apart from a human
decision by ``auto = 1`` and a reason.

Why a table of its own, in the same file as the action journal (``audit.db``):

* ``audit.events`` is append-only free text (``detail``, cut at 2 000 characters). A
  validation is ONE fact with two instants — asked at, decided at — and typed fields the
  study and the exports filter on (delay, origin, auto, edited). It is written when asked
  (``decision = 'pending'``) and completed when decided, which an append-only line cannot
  express without pairing lines back together at read time.
* Same file = same backup, same restore, same retention as the action journal: nothing new
  to operate.
* The delegated decisions that were already journaled (``share.hitl.*``, Teams) keep their
  ``audit.events`` lines unchanged; an owner's decision now also gets its line there
  (``hitl.allow`` …) so Operate › Journal shows it.

What is stored of the input: the tool, its one-line title and a JSON summary of the input,
with the vault's masking applied first (``agents.redact_obj`` over the values the session's
environment holds) and then a pattern pass for credentials pasted in clear. Never the
tool's output.

Who decided is not carried by ``AgentSession.resolve_permission(pid, decision)`` (its
callers and their tests pin that dict): the caller declares it just before with
``attribute(request_id, actor, origin)``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import time

import audit

SUMMARY_MAX = 2000
ORIGINS = ("owner", "project_member", "share", "teams", "system", "unknown")
HUMAN = ("allow", "deny", "edit", "answer", "mode_change")
COLUMNS = ("id", "request_id", "kind", "session_id", "project", "owner", "actor", "origin",
           "tool", "title", "input_summary", "input_sha256", "mode", "requested_at",
           "decided_at", "delay_ms", "decision", "auto", "auto_reason", "edited",
           "edited_summary", "message", "agent_run")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hitl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,          -- id of the permission / question / mode change
    kind TEXT NOT NULL,                       -- permission | question | mode
    session_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',           -- who opened the session
    actor TEXT NOT NULL DEFAULT '',           -- who decided ('' while pending / automatic)
    origin TEXT NOT NULL DEFAULT '',          -- owner | project_member | share | teams | system | unknown
    tool TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',           -- one line, secrets masked
    input_summary TEXT NOT NULL DEFAULT '',   -- JSON of the input, secrets masked, cut
    input_sha256 TEXT NOT NULL DEFAULT '',    -- of the masked input (same request = same hash)
    mode TEXT NOT NULL DEFAULT '',            -- approval mode of the session when asked
    requested_at REAL NOT NULL,
    decided_at REAL,
    delay_ms INTEGER,
    decision TEXT NOT NULL DEFAULT 'pending', -- pending | allow | deny | edit | answer |
                                              -- mode_change | auto_allow | auto_deny |
                                              -- cancelled | interrupted
    auto INTEGER NOT NULL DEFAULT 0,          -- 1 = nobody clicked
    auto_reason TEXT NOT NULL DEFAULT '',     -- mode:bypassPermissions | policy:… | sandbox
    edited INTEGER NOT NULL DEFAULT 0,        -- 1 = allowed with a modified input
    edited_summary TEXT NOT NULL DEFAULT '',  -- the input that actually ran (masked)
    message TEXT NOT NULL DEFAULT '',         -- reason of a refusal / the answers (masked)
    agent_run TEXT NOT NULL DEFAULT ''        -- agent name when the session is an agent run
);
CREATE INDEX IF NOT EXISTS ix_hitl_session ON hitl(session_id, requested_at);
CREATE INDEX IF NOT EXISTS ix_hitl_project ON hitl(project, requested_at);
CREATE INDEX IF NOT EXISTS ix_hitl_actor ON hitl(actor, requested_at);
"""

# credentials pasted in clear in a command or a file: masked whatever the vault holds
_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)",
                re.S), "[masked:private-key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(\.[A-Za-z0-9_-]+)?"), "[masked:jwt]"),
    (re.compile(r"\b(sk|pk|rk|sik|skx)[-_][A-Za-z0-9_-]{16,}"), "[masked:key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[masked:key]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), "[masked:key]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[masked:key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[masked:key]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 [masked]"),
    (re.compile(r"(?i)\b([a-z0-9_]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey)"
                r"[a-z0-9_]*)(\"?\s*[=:]\s*\"?)([^\s\"']{4,})"), r"\1\2[masked]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s@/]{3,})@"), r"\1:[masked]@"),
]

_MARKER = re.compile(r"\[(?:secret:[A-Za-z0-9_]+|masked(?::[a-z-]+)?)\]")
_KEPT = re.compile(r"\x00(\d+)\x00")

# who is about to decide a request: {request_id: (actor, origin, at)} — see module docstring
_attrib: dict[str, tuple[str, str, float]] = {}


def scrub(text: str) -> str:
    """Pattern pass: credentials written in clear, whatever the vault knows."""
    if not text:
        return text
    # the vault's markers ([secret:NAME]) and ours stay as they are: NAME is not a value
    kept: list[str] = []

    def keep(m: re.Match) -> str:
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"
    text = _MARKER.sub(keep, text)
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return _KEPT.sub(lambda m: kept[int(m.group(1))], text)


def mask(obj, secrets: dict[str, str] | None = None):
    """The vault's masking (exact values and their encoded forms) then the pattern pass,
    over every string of a JSON-like structure."""
    if secrets:
        import agents
        obj = agents.redact_obj(obj, secrets)
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [mask(x) for x in obj]
    if isinstance(obj, dict):
        return {k: mask(v) for k, v in obj.items()}
    return obj


def summarize(obj, secrets: dict[str, str] | None = None) -> tuple[str, str]:
    """(summary cut at SUMMARY_MAX, sha256 of the full masked JSON)."""
    try:
        full = json.dumps(mask(obj, secrets), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        full = scrub(str(obj))
    digest = hashlib.sha256(full.encode("utf-8", "replace")).hexdigest()
    return (full if len(full) <= SUMMARY_MAX else full[:SUMMARY_MAX - 1] + "…"), digest


def _con() -> sqlite3.Connection:
    con = audit._con()  # same file as the action journal (audit.DB, patched by tests)
    con.executescript(_SCHEMA)
    return con


def attribute(request_id: str, actor: str, origin: str) -> None:
    """Declare who is about to decide `request_id` (consumed by the next `decided`)."""
    if not request_id:
        return
    now = time.time()
    for k in [k for k, v in _attrib.items() if now - v[2] > 600]:
        _attrib.pop(k, None)
    _attrib[request_id] = ((actor or "").lower().strip(),
                           origin if origin in ORIGINS else "unknown", now)


def _session_facts(session_id: str) -> tuple[str, str]:
    """(project, owner) of a session, '' when SOKKAN does not know it."""
    try:
        import board
        return (board.get_session_project(session_id) or "",
                board.get_session_owner(session_id) or "")
    except Exception:  # noqa: BLE001
        return "", ""


def origin_of(actor: str, session_id: str, fallback_owner: str = "") -> str:
    """owner when the person who decides opened the session, else project_member."""
    owner = _session_facts(session_id)[1] or (fallback_owner or "").lower().strip()
    a = (actor or "").lower().strip()
    if not a:
        return "unknown"
    return "owner" if (not owner or a == owner) else "project_member"


def requested(request_id: str, kind: str, *, session_id: str, tool: str = "", title: str = "",
              input_data=None, mode: str = "", secrets: dict[str, str] | None = None,
              owner: str = "", agent_run: str = "") -> None:
    """A session starts waiting for a person. Best-effort: never breaks the tool call."""
    try:
        project, board_owner = _session_facts(session_id)
        summary, digest = summarize(input_data if input_data is not None else {}, secrets)
        con = _con()
        con.execute(
            "INSERT OR IGNORE INTO hitl(request_id, kind, session_id, project, owner, tool, title,"
            " input_summary, input_sha256, mode, requested_at, agent_run)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, kind, session_id, project, board_owner or (owner or "").lower().strip(),
             tool, str(mask(title or "", secrets))[:300], summary, digest, mode, time.time(),
             agent_run or ""))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def decided(request_id: str, decision: str, *, actor: str = "", origin: str = "",
            edited_input=None, message=None, secrets: dict[str, str] | None = None,
            fallback_owner: str = "") -> dict | None:
    """Complete the row of a pending request. Returns the row (None = unknown / already
    decided: a decision is written once)."""
    try:
        att = _attrib.pop(request_id, None)
        if att and not actor:
            actor, origin = att[0], att[1]
        con = _con()
        row = con.execute("SELECT * FROM hitl WHERE request_id = ? AND decision = 'pending'",
                          (request_id,)).fetchone()
        if row is None:
            con.close()
            return None
        now = time.time()
        if decision in ("cancelled", "interrupted"):
            actor, origin = "", "system"
        elif not origin or origin not in ORIGINS:
            origin = origin_of(actor, row["session_id"], fallback_owner or row["owner"])
        edited, edited_summary = 0, ""
        if decision == "allow" and edited_input is not None:
            s, digest = summarize(edited_input, secrets)
            if digest != row["input_sha256"]:
                decision, edited, edited_summary = "edit", 1, s
        msg = ""
        if message:
            msg = message if isinstance(message, str) else summarize(message, secrets)[0]
            msg = str(mask(msg, secrets))[:SUMMARY_MAX]
        con.execute(
            "UPDATE hitl SET decision=?, actor=?, origin=?, decided_at=?, delay_ms=?, edited=?,"
            " edited_summary=?, message=? WHERE id=?",
            (decision, (actor or "").lower().strip(), origin, now,
             int(round((now - row["requested_at"]) * 1000)), edited, edited_summary, msg,
             row["id"]))
        con.commit()
        out = dict(con.execute("SELECT * FROM hitl WHERE id = ?", (row["id"],)).fetchone())
        con.close()
    except Exception:  # noqa: BLE001
        return None
    if out["decision"] in HUMAN and out["origin"] in ("owner", "project_member", "unknown"):
        # Operate › Journal: the delegated paths (share, Teams) write their own line already
        audit.log(out["actor"], f"hitl.{out['decision']}", f"session:{out['session_id']}",
                  f"{out['tool']} — {out['title']} ({out['delay_ms']} ms)"[:500],
                  project=out["project"])
    return out


def automatic(kind: str, *, session_id: str, tool: str, title: str = "", input_data=None,
              allowed: bool, reason: str, mode: str = "",
              secrets: dict[str, str] | None = None, owner: str = "",
              agent_run: str = "") -> None:
    """An approval nobody clicked: auto-approved by the session's mode, or refused by an
    agent's policy / the sandbox. One row, asked and decided at the same instant."""
    try:
        import uuid
        project, board_owner = _session_facts(session_id)
        summary, digest = summarize(input_data if input_data is not None else {}, secrets)
        now = time.time()
        con = _con()
        con.execute(
            "INSERT INTO hitl(request_id, kind, session_id, project, owner, actor, origin, tool,"
            " title, input_summary, input_sha256, mode, requested_at, decided_at, delay_ms,"
            " decision, auto, auto_reason, agent_run)"
            " VALUES(?,?,?,?,?,'','system',?,?,?,?,?,?,?,0,?,1,?,?)",
            (uuid.uuid4().hex, kind, session_id, project,
             board_owner or (owner or "").lower().strip(), tool,
             str(mask(title or "", secrets))[:300], summary, digest, mode, now, now,
             "auto_allow" if allowed else "auto_deny", reason, agent_run or ""))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def mode_changed(session_id: str, old: str, new: str, actor: str = "",
                 owner: str = "") -> None:
    """The approval mode of a session changed (what the next tools will ask or not)."""
    try:
        import uuid
        project, board_owner = _session_facts(session_id)
        now = time.time()
        a = (actor or "").lower().strip()
        con = _con()
        con.execute(
            "INSERT INTO hitl(request_id, kind, session_id, project, owner, actor, origin, tool,"
            " title, mode, requested_at, decided_at, delay_ms, decision)"
            " VALUES(?,'mode',?,?,?,?,?,'',?,?,?,?,0,'mode_change')",
            (uuid.uuid4().hex, session_id, project, board_owner or (owner or "").lower().strip(),
             a, origin_of(a, session_id, owner), f"{old} → {new}", new, now, now))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        return
    audit.log(a, "hitl.mode_change", f"session:{session_id}", f"{old} → {new}", project=project)


def mark_interrupted(before: float | None = None) -> int:
    """At API start: a request still `pending` belongs to a process that is gone (its
    future lived in memory). It is closed as `interrupted`, never left looking live."""
    try:
        con = _con()
        cur = con.execute(
            "UPDATE hitl SET decision='interrupted', origin='system', decided_at=?"
            " WHERE decision='pending' AND requested_at < ?",
            (time.time(), before if before is not None else time.time()))
        con.commit()
        n = cur.rowcount
        con.close()
        return n
    except Exception:  # noqa: BLE001
        return 0


def parse_when(v: str | float | None) -> float | None:
    """Epoch seconds, or an ISO date / datetime (naive = UTC). None when empty."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    import datetime
    d = datetime.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def query(*, project: str | None = None, projects: list[str] | None = None, actor: str = "",
          session_id: str = "", sessions: list[str] | None = None, since: float | None = None,
          until: float | None = None, decision: str = "", limit: int = 5000) -> list[dict]:
    """Rows, oldest first. `actor` matches who decided OR who owns the session (a person's
    validations and the approvals asked on their sessions)."""
    where, args = [], []
    if project is not None:
        where.append("project = ?")
        args.append(project)
    if projects is not None:
        if not projects:
            return []
        where.append(f"project IN ({','.join('?' * len(projects))})")
        args += projects
    if actor:
        where.append("(actor = ? OR owner = ?)")
        args += [actor.lower().strip()] * 2
    if session_id:
        where.append("session_id = ?")
        args.append(session_id)
    if sessions is not None:
        if not sessions:
            return []
        where.append(f"session_id IN ({','.join('?' * len(sessions))})")
        args += sessions
    if since is not None:
        where.append("requested_at >= ?")
        args.append(since)
    if until is not None:
        where.append("requested_at < ?")
        args.append(until)
    if decision:
        where.append("decision = ?")
        args.append(decision)
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM hitl" + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY requested_at, id LIMIT ?", (*args, max(1, min(int(limit), 100000))))]
    con.close()
    return rows


def _iso(ts) -> str:
    if ts is None:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def public(row: dict) -> dict:
    """A row as exported: ISO instants next to the epoch ones, booleans as booleans."""
    out = {k: row.get(k) for k in COLUMNS}
    out["requested_at_iso"] = _iso(row.get("requested_at"))
    out["decided_at_iso"] = _iso(row.get("decided_at"))
    out["auto"] = bool(row.get("auto"))
    out["edited"] = bool(row.get("edited"))
    return out


def to_csv(rows: list[dict]) -> str:
    cols = [*COLUMNS, "requested_at_iso", "decided_at_iso"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        p = public(r)
        w.writerow(["" if p.get(c) is None else
                    (int(p[c]) if isinstance(p[c], bool) else p[c]) for c in cols])
    return buf.getvalue()
