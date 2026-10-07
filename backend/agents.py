#!/usr/bin/env python3
"""agents.py — SOKKAN 3.1 "Crew up": agents and their runs (store + rules).

An agent = a named, owned job description (model, purpose, deliverable + done
criteria, trigger, tools, vault secret NAMES, budget, HITL policy, outputs). A run
= one execution, i.e. one ordinary SDK session started by the scheduler. Spec:
docs/AGENTS.md. This module is pure storage + validation + access rules (no
asyncio, no SDK): the API, the scheduler (agents_runtime.py) and the MCP server
(agents_mcp.py, a separate process) all share it through SQLite.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import cronexpr
import iam

DB = Path(os.environ.get(
    "SOKKAN_AGENTS_DB",
    os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
                 "agents.db")))

STATUSES = ("draft", "pending", "active", "paused", "archived")
TRIGGERS = ("manual", "once", "cron", "event")
OUTPUTS = ("card", "memory", "file", "notify")
NOTIFY_ON = ("failure", "timeout", "budget", "approval", "success")
MCP_CHOICES = ("sokkan-memory", "sokkan-board", "sokkan-observability")
DEFAULT_TOOLS = ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "Bash"]
KNOWN_TOOLS = ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "Bash", "Edit", "Write",
               "NotebookEdit", "TodoWrite", "Task"]
RUN_FINAL = ("succeeded", "incomplete", "failed", "timeout", "budget", "interrupted",
             "cancelled", "skipped")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
# a tool rule: Name or Name(spec) — Claude Code permission-rule syntax
_TOOL_RE = re.compile(r"^(mcp__[A-Za-z0-9_-]+(__[A-Za-z0-9_*-]+)?|[A-Z][A-Za-z]+)(\(.{1,200}\))?$")
# values that look like a credential pasted where a NAME was expected
_SECRETISH = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-|AKIA[0-9A-Z]{16}"
                        r"|-----BEGIN|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")

_init_lock = threading.Lock()
_initialized_for: str | None = None


class AgentError(ValueError):
    """Validation / lifecycle error — maps to HTTP 400 (or 403 for `Forbidden`)."""


class Forbidden(AgentError):
    pass


class NotFound(AgentError):
    pass


# ---- schema ------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    deliverable TEXT NOT NULL DEFAULT '',
    done_criteria TEXT NOT NULL DEFAULT '',
    playbook TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT 'manual',
    schedule TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'Europe/Zurich',
    once_at REAL,
    event TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '[]',
    mcp TEXT NOT NULL DEFAULT '[]',
    auto_approve TEXT NOT NULL DEFAULT '[]',
    secrets TEXT NOT NULL DEFAULT '[]',
    budget_usd REAL NOT NULL DEFAULT 0,
    max_minutes INTEGER NOT NULL DEFAULT 30,
    outputs TEXT NOT NULL DEFAULT '["card"]',
    notify_on TEXT NOT NULL DEFAULT '["failure","timeout","budget","approval"]',
    status TEXT NOT NULL DEFAULT 'draft',
    pending_change TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at REAL,
    next_run_at REAL,
    last_run_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    trigger TEXT NOT NULL,
    scheduled_for REAL,
    status TEXT NOT NULL DEFAULT 'queued',
    waiting_approval INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL DEFAULT '',
    runner TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    num_turns INTEGER NOT NULL DEFAULT 0,
    deliverable TEXT NOT NULL DEFAULT '',
    outputs TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '{}',
    requested_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL,
    UNIQUE(agent_id, scheduled_for)
);
CREATE INDEX IF NOT EXISTS ix_runs_agent ON runs(agent_id, id);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);
"""

_JSON_FIELDS = ("tools", "mcp", "auto_approve", "secrets", "outputs", "notify_on")


def init(force: bool = False) -> None:
    global _initialized_for
    with _init_lock:
        if _initialized_for == str(DB) and not force:
            return
        DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB)
        con.executescript(_SCHEMA)
        con.commit()
        con.close()
        _initialized_for = str(DB)


def _con() -> sqlite3.Connection:
    init()
    # several processes (API + one MCP server per session) share the file
    con = sqlite3.connect(DB, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


# ---- (de)serialisation -------------------------------------------------------
def _agent_out(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for f in _JSON_FIELDS:
        try:
            d[f] = json.loads(d[f] or "[]")
        except ValueError:
            d[f] = []
    d["pending_change"] = json.loads(d["pending_change"]) if d.get("pending_change") else None
    return d


def _run_out(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for f in ("outputs", "context"):
        try:
            d[f] = json.loads(d[f] or "{}")
        except ValueError:
            d[f] = {}
    d["waiting_approval"] = bool(d["waiting_approval"])
    return d


# ---- validation --------------------------------------------------------------
def _str_list(v, field: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise AgentError(f"{field} must be a list of strings")
    out: list[str] = []
    for x in v:
        x = x.strip()
        if x and x not in out:
            out.append(x)
    return out


def tool_base(rule: str) -> str:
    """'Bash(npm audit:*)' → 'Bash'."""
    return rule.split("(", 1)[0]


def validate(fields: dict, partial: bool = False, known_secrets: list[str] | None = None) -> dict:
    """Normalise and check agent fields. `partial` = an update (only given keys).
    Raises AgentError with a message a human (or a model) can act on."""
    out: dict = {}
    f = dict(fields)

    def has(k: str) -> bool:
        return k in f and f[k] is not None

    if has("name") or not partial:
        name = (f.get("name") or "").strip().lower()
        if not _NAME_RE.match(name):
            raise AgentError("name: kebab-case slug, 2-48 chars, e.g. 'nightly-cve-audit'")
        out["name"] = name
    for k, lim in (("purpose", 6000), ("deliverable", 3000), ("done_criteria", 2000)):
        if has(k):
            v = str(f[k]).strip()
            if len(v) > lim:
                raise AgentError(f"{k}: {lim} characters max")
            out[k] = v
    if not partial:
        if not out.get("purpose"):
            raise AgentError("purpose is required — what is this agent for?")
        if not out.get("deliverable"):
            raise AgentError("deliverable is required — what must a run hand back?")
    if has("model"):
        m = str(f["model"]).strip()
        if m and not re.match(r"^[A-Za-z0-9._:/\[\]-]{2,80}$", m):
            raise AgentError("model: an alias (haiku, sonnet, opus) or a model id")
        out["model"] = m
    if has("playbook"):
        out["playbook"] = str(f["playbook"]).strip()
        if out["playbook"]:
            import playbooks
            if playbooks.get(out["playbook"]) is None:
                raise AgentError(f"unknown playbook: {out['playbook']}")
    if has("trigger") or not partial:
        t = (f.get("trigger") or "manual").strip()
        if t not in TRIGGERS:
            raise AgentError(f"trigger must be one of {', '.join(TRIGGERS)}")
        out["trigger"] = t
    if has("timezone"):
        tz = str(f["timezone"]).strip() or cronexpr.DEFAULT_TZ
        if not cronexpr.valid_tz(tz):
            raise AgentError(f"unknown timezone: {tz}")
        out["timezone"] = tz
    if has("schedule"):
        s = str(f["schedule"]).strip()
        if s:
            try:
                cronexpr.parse(s)
            except cronexpr.CronError as e:
                raise AgentError(f"schedule: {e}") from None
        out["schedule"] = s
    if has("once_at"):
        try:
            out["once_at"] = float(f["once_at"]) if f["once_at"] != "" else None
        except (TypeError, ValueError):
            raise AgentError("once_at: a UTC epoch timestamp") from None
    if has("event"):
        ev = str(f["event"]).strip()
        if ev and not re.match(r"^alert(:[\w*?.\- ]{1,80})?$", ev):
            raise AgentError("event: 'alert' or 'alert:<alertname glob>'")
        out["event"] = ev
    if has("tools") or not partial:
        tools = _str_list(f.get("tools"), "tools") if has("tools") else list(DEFAULT_TOOLS)
        for t in tools:
            if not _TOOL_RE.match(t):
                raise AgentError(f"tools: invalid tool {t!r}")
        out["tools"] = tools
    if has("auto_approve"):
        aa = _str_list(f["auto_approve"], "auto_approve")
        for t in aa:
            if not _TOOL_RE.match(t):
                raise AgentError(f"auto_approve: invalid rule {t!r}")
        out["auto_approve"] = aa
    if has("mcp") or not partial:
        mcp = _str_list(f.get("mcp"), "mcp") if has("mcp") else []
        for m in mcp:
            if m not in MCP_CHOICES:
                raise AgentError(f"mcp: one of {', '.join(MCP_CHOICES)}")
        if "sokkan-memory" not in mcp:
            mcp.insert(0, "sokkan-memory")
        out["mcp"] = mcp
    if has("secrets"):
        sec = _str_list(f["secrets"], "secrets")
        for s in sec:
            if _SECRETISH.search(s) or not re.match(r"^[A-Z_][A-Z0-9_]{0,63}$", s):
                raise AgentError("secrets: vault secret NAMES only (e.g. GITHUB_TOKEN) — "
                                 "never a value. Store the value in Profile → Secrets first.")
        if known_secrets is not None:
            missing = [s for s in sec if s not in known_secrets]
            if missing:
                raise AgentError(f"secrets not in the vault: {', '.join(missing)} — an admin "
                                 "adds them in Profile → Secrets")
        out["secrets"] = sec
    for k, lo, hi in (("budget_usd", 0, 1000), ("max_minutes", 1, 24 * 60)):
        if has(k):
            try:
                v = float(f[k])
            except (TypeError, ValueError):
                raise AgentError(f"{k}: a number") from None
            if not lo <= v <= hi:
                raise AgentError(f"{k}: between {lo} and {hi}")
            out[k] = int(v) if k == "max_minutes" else v
    if has("outputs") or not partial:
        o = _str_list(f.get("outputs"), "outputs") if has("outputs") else ["card"]
        for x in o:
            if x not in OUTPUTS:
                raise AgentError(f"outputs: any of {', '.join(OUTPUTS)}")
        out["outputs"] = o
    if has("notify_on"):
        n = _str_list(f["notify_on"], "notify_on")
        for x in n:
            if x not in NOTIFY_ON:
                raise AgentError(f"notify_on: any of {', '.join(NOTIFY_ON)}")
        out["notify_on"] = n
    # no secret values smuggled into the free text either
    for k in ("purpose", "deliverable", "done_criteria"):
        if out.get(k) and _SECRETISH.search(out[k]):
            raise AgentError(f"{k} seems to contain a credential — put it in the vault and "
                             "reference it by name in `secrets`")
    return out


def _check_trigger(a: dict) -> None:
    t = a.get("trigger")
    if t == "cron" and not a.get("schedule"):
        raise AgentError("a cron agent needs a schedule (e.g. '0 2 * * *')")
    if t == "once" and not a.get("once_at"):
        raise AgentError("a one-shot agent needs once_at (when to run)")
    if t == "event" and not a.get("event"):
        raise AgentError("an event agent needs an event ('alert' or 'alert:<name>')")
    aa = {tool_base(x) for x in a.get("auto_approve") or []}
    tools = {tool_base(x) for x in a.get("tools") or []}
    if not aa <= tools:
        raise AgentError("auto_approve must be a subset of tools: "
                         f"{', '.join(sorted(aa - tools))} not in tools")


def next_fire(a: dict, after: float | None = None) -> float | None:
    """Next scheduled time (UTC epoch) for an ACTIVE agent, or None."""
    now = time.time() if after is None else after
    if a.get("status") != "active":
        return None
    if a.get("trigger") == "cron" and a.get("schedule"):
        try:
            return cronexpr.parse(a["schedule"]).next_after(now, a.get("timezone")
                                                            or cronexpr.DEFAULT_TZ)
        except cronexpr.CronError:
            return None
    if a.get("trigger") == "once" and a.get("once_at") and not a.get("last_run_at"):
        # a one-shot fires once (the scheduler clears next_run_at once it is queued)
        return float(a["once_at"])
    return None


# ---- access control (IAM roles, owner field) ---------------------------------
def can_read(user: dict, a: dict) -> bool:
    role = iam.rank(user.get("role", ""))
    return role >= iam.rank("admin") or (role >= iam.rank("dev")
                                          and a["owner"] == user.get("email"))


def can_manage(user: dict, a: dict) -> bool:
    return can_read(user, a)


def _need(user: dict, a: dict | None) -> dict:
    if a is None:
        raise NotFound("agent not found")
    if not can_read(user, a):
        # same answer as "missing": do not leak other owners' agents
        raise NotFound("agent not found")
    return a


# ---- CRUD ----------------------------------------------------------------------
def get(agent_id: int) -> dict | None:
    con = _con()
    try:
        return _agent_out(con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
    finally:
        con.close()


def get_by_name(name: str) -> dict | None:
    con = _con()
    try:
        return _agent_out(con.execute("SELECT * FROM agents WHERE name=?",
                                      ((name or "").strip().lower(),)).fetchone())
    finally:
        con.close()


def resolve(ref) -> dict | None:
    """id (int or digits) or name."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return get(int(ref))
    return get_by_name(str(ref))


def list_agents(user: dict, include_archived: bool = False) -> list[dict]:
    con = _con()
    try:
        rows = con.execute("SELECT * FROM agents ORDER BY status='archived', name").fetchall()
        stats = {r["agent_id"]: dict(r) for r in con.execute(
            "SELECT agent_id, COUNT(*) AS runs, SUM(cost_usd) AS cost,"
            " SUM(status IN ('queued','running')) AS live,"
            " SUM(waiting_approval) AS waiting FROM runs GROUP BY agent_id")}
        last = {r["agent_id"]: dict(r) for r in con.execute(
            "SELECT r.agent_id, r.id, r.status, r.started_at, r.ended_at, r.cost_usd FROM runs r"
            " JOIN (SELECT agent_id, MAX(id) AS m FROM runs"
            "  WHERE status NOT IN ('skipped','cancelled','queued','running')"
            "  GROUP BY agent_id) x"
            " ON r.id = x.m")}
    finally:
        con.close()
    out = []
    for r in rows:
        a = _agent_out(r)
        if not can_read(user, a) or (a["status"] == "archived" and not include_archived):
            continue
        s = stats.get(a["id"], {})
        a["stats"] = {"runs": s.get("runs") or 0, "cost_usd": round(s.get("cost") or 0, 4),
                      "live": s.get("live") or 0, "waiting": s.get("waiting") or 0}
        a["last_run"] = last.get(a["id"])
        a.update(deck_state(a))
        out.append(a)
    return out


_ERROR_RUNS = ("failed", "timeout", "budget", "interrupted", "incomplete")


def deck_state(a: dict) -> dict:
    """The Crew deck column of an agent (spec § The Crew tab) — computed here so
    the UI, the MCP and the tests agree: idle (blue) · armed (green) · running
    (orange) · error (red), plus the badges."""
    stats = a.get("stats") or {}
    last = a.get("last_run") or {}
    if a["status"] == "archived":
        state = "archived"
    elif stats.get("live"):
        state = "running"
    elif last.get("status") in _ERROR_RUNS:
        state = "error"
    elif a["status"] == "active" and a["trigger"] in ("cron", "once", "event") and (
            a.get("next_run_at") or a["trigger"] == "event"):
        state = "armed"
    else:
        state = "idle"
    return {"deck": state,
            "needs_approval": a["status"] == "pending" or bool(a.get("pending_change")),
            "waiting_for_human": bool(stats.get("waiting"))}


def create(user: dict, fields: dict, created_by: str = "", activate: bool = False,
           proposal: bool = False, known_secrets: list[str] | None = None) -> dict:
    """Human form: draft, or active if `activate`. Session / Nina: `proposal` →
    pending (never runs before a human approves it)."""
    if iam.rank(user.get("role", "")) < iam.rank("dev"):
        raise Forbidden("role 'dev' required to create agents")
    v = validate(fields, partial=False, known_secrets=known_secrets)
    for k, default in (("timezone", cronexpr.DEFAULT_TZ), ("auto_approve", []), ("secrets", []),
                       ("budget_usd", 0.0), ("max_minutes", 30), ("model", ""),
                       ("playbook", ""), ("schedule", ""), ("event", ""), ("once_at", None),
                       ("done_criteria", ""),
                       ("notify_on", ["failure", "timeout", "budget", "approval"])):
        v.setdefault(k, default)
    _check_trigger(v)
    if get_by_name(v["name"]):
        raise AgentError(f"an agent named {v['name']!r} already exists")
    now = time.time()
    status = "pending" if proposal else ("active" if activate else "draft")
    v.update(owner=user["email"], status=status, created_by=created_by or f"user:{user['email']}",
             created_at=now, updated_at=now)
    if status == "active":
        v.update(approved_by=user["email"], approved_at=now)
    v["next_run_at"] = next_fire(v, now)
    cols = list(v)
    vals = [json.dumps(v[c]) if c in _JSON_FIELDS else v[c] for c in cols]
    con = _con()
    try:
        cur = con.execute(f"INSERT INTO agents({', '.join(cols)}) VALUES"
                          f"({', '.join('?' * len(cols))})", vals)
        aid = cur.lastrowid
    finally:
        con.close()
    return get(aid)


def _write(aid: int, **cols) -> dict:
    cols["updated_at"] = time.time()
    sets, vals = [], []
    for k, val in cols.items():
        sets.append(f"{k}=?")
        as_json = k in _JSON_FIELDS or (k == "pending_change" and val is not None)
        vals.append(json.dumps(val) if as_json else val)
    con = _con()
    try:
        con.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id=?", (*vals, aid))
    finally:
        con.close()
    return get(aid)


_EDITABLE = ("name", "model", "purpose", "deliverable", "done_criteria", "playbook", "trigger",
             "schedule", "timezone", "once_at", "event", "tools", "mcp", "auto_approve",
             "secrets", "budget_usd", "max_minutes", "outputs", "notify_on")


def update(user: dict, agent_id: int, fields: dict, from_session: bool = False,
           known_secrets: list[str] | None = None) -> dict:
    """A human edit applies directly (the human is the gate). A session edit on an
    approved agent (active/paused) becomes `pending_change` — the approved
    version keeps running until a human approves it."""
    a = _need(user, get(agent_id))
    if a["status"] == "archived":
        raise AgentError("archived agents are read-only")
    fields = {k: v for k, v in fields.items() if k in _EDITABLE and v is not None}
    if not fields:
        raise AgentError(f"nothing to update (editable: {', '.join(_EDITABLE)})")
    v = validate(fields, partial=True, known_secrets=known_secrets)
    if "name" in v and v["name"] != a["name"] and get_by_name(v["name"]):
        raise AgentError(f"an agent named {v['name']!r} already exists")
    merged = {**a, **v}
    _check_trigger(merged)
    if from_session and a["status"] in ("active", "paused"):
        pc = {**(a.get("pending_change") or {}), **v}
        return _write(a["id"], pending_change=pc)
    new_status = a["status"]
    if from_session and a["status"] == "draft":
        new_status = "pending"
    merged["status"] = new_status
    return _write(a["id"], **v, status=new_status, next_run_at=next_fire(merged))


def approve(user: dict, agent_id: int) -> dict:
    """Activate a pending agent, or apply a pending change. Owner (dev+) or admin."""
    a = _need(user, get(agent_id))
    now = time.time()
    if a.get("pending_change"):
        merged = {**a, **a["pending_change"]}
        _check_trigger(merged)
        cols = {k: merged[k] for k in a["pending_change"]}
        return _write(a["id"], **cols, pending_change=None, approved_by=user["email"],
                      approved_at=now, next_run_at=next_fire(merged))
    if a["status"] not in ("pending", "draft"):
        raise AgentError(f"nothing to approve (status {a['status']})")
    merged = {**a, "status": "active"}
    _check_trigger(merged)
    return _write(a["id"], status="active", approved_by=user["email"], approved_at=now,
                  next_run_at=next_fire(merged))


def reject(user: dict, agent_id: int) -> dict:
    a = _need(user, get(agent_id))
    if a.get("pending_change"):
        return _write(a["id"], pending_change=None)
    if a["status"] != "pending":
        raise AgentError(f"nothing to reject (status {a['status']})")
    return _write(a["id"], status="draft")


def set_status(user: dict, agent_id: int, status: str, from_session: bool = False) -> dict:
    a = _need(user, get(agent_id))
    cur = a["status"]
    if status == "paused":
        if cur != "active":
            raise AgentError(f"only an active agent can be paused (status {cur})")
        return _write(a["id"], status="paused", next_run_at=None)
    if status == "active":  # resume
        if cur != "paused":
            raise AgentError(f"only a paused agent can be resumed (status {cur})")
        if from_session and not a.get("approved_at"):
            raise Forbidden("this agent was never approved by a human — approve it in Crew")
        return _write(a["id"], status="active", next_run_at=next_fire({**a, "status": "active"}))
    if status == "archived":
        if cur == "archived":
            return a
        return _write(a["id"], status="archived", next_run_at=None, pending_change=None)
    raise AgentError(f"unsupported status change → {status}")


def set_next_run(agent_id: int, next_run_at: float | None, last_run_at: float | None = None,
                 status: str | None = None) -> None:
    cols: dict = {"next_run_at": next_run_at}
    if last_run_at is not None:
        cols["last_run_at"] = last_run_at
    if status:
        cols["status"] = status
    _write(agent_id, **cols)


# ---- runs ----------------------------------------------------------------------
def enqueue_run(agent_id: int, trigger: str, requested_by: str = "",
                scheduled_for: float | None = None, context: dict | None = None,
                status: str = "queued", error: str = "") -> dict | None:
    """Insert a run. For a scheduled occurrence `scheduled_for` is set and the
    (agent_id, scheduled_for) UNIQUE constraint makes a second insert a no-op →
    returns None (no double run, even with two schedulers)."""
    now = time.time()
    con = _con()
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO runs(agent_id, trigger, scheduled_for, status, context,"
            " requested_by, created_at, error, ended_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (agent_id, trigger, scheduled_for, status, json.dumps(context or {}),
             requested_by, now, error, now if status in RUN_FINAL else None))
        if cur.rowcount == 0:
            return None
        return _run_out(con.execute("SELECT * FROM runs WHERE id=?",
                                    (cur.lastrowid,)).fetchone())
    finally:
        con.close()


def request_run(user: dict, agent_id: int, trigger: str = "manual",
                requested_by: str = "", context: dict | None = None) -> dict:
    a = _need(user, get(agent_id))
    if a["status"] != "active":
        raise AgentError(f"agent is {a['status']} — only an active (approved) agent runs")
    if active_run(a["id"]):
        raise AgentError("a run of this agent is already queued or running")
    return enqueue_run(a["id"], trigger, requested_by or user["email"], context=context)


def active_run(agent_id: int) -> dict | None:
    con = _con()
    try:
        return _run_out(con.execute(
            "SELECT * FROM runs WHERE agent_id=? AND status IN ('queued','running')"
            " ORDER BY id LIMIT 1", (agent_id,)).fetchone())
    finally:
        con.close()


def claim_run(run_id: int, runner: str) -> bool:
    """Atomic queued → running. Only one claimer wins."""
    con = _con()
    try:
        cur = con.execute("UPDATE runs SET status='running', started_at=?, runner=?"
                          " WHERE id=? AND status='queued'", (time.time(), runner, run_id))
        return cur.rowcount == 1
    finally:
        con.close()


def update_run(run_id: int, **cols) -> dict | None:
    if not cols:
        return get_run(run_id)
    if cols.get("status") in RUN_FINAL and "ended_at" not in cols:
        cols["ended_at"] = time.time()
    sets, vals = [], []
    for k, val in cols.items():
        sets.append(f"{k}=?")
        vals.append(json.dumps(val) if k in ("outputs", "context") else val)
    con = _con()
    try:
        con.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", (*vals, run_id))
    finally:
        con.close()
    return get_run(run_id)


def get_run(run_id: int) -> dict | None:
    con = _con()
    try:
        return _run_out(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    finally:
        con.close()


def get_run_for(user: dict, run_id: int) -> dict:
    r = get_run(run_id)
    if r is None:
        raise NotFound("run not found")
    a = _need(user, get(r["agent_id"]))
    return {**r, "agent_name": a["name"]}


def list_runs(user: dict, agent_id: int, limit: int = 50) -> list[dict]:
    _need(user, get(agent_id))
    con = _con()
    try:
        rows = con.execute("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC LIMIT ?",
                           (agent_id, max(1, min(int(limit), 500)))).fetchall()
    finally:
        con.close()
    return [_run_out(r) for r in rows]


def runs_with_status(*statuses: str) -> list[dict]:
    con = _con()
    try:
        rows = con.execute(f"SELECT * FROM runs WHERE status IN ({','.join('?' * len(statuses))})"
                           " ORDER BY id", statuses).fetchall()
    finally:
        con.close()
    return [_run_out(r) for r in rows]


def due_agents(now: float) -> list[dict]:
    con = _con()
    try:
        rows = con.execute("SELECT * FROM agents WHERE status='active' AND next_run_at IS NOT NULL"
                           " AND next_run_at <= ?", (now,)).fetchall()
    finally:
        con.close()
    return [_agent_out(r) for r in rows]


def event_agents(kind: str) -> list[dict]:
    con = _con()
    try:
        rows = con.execute("SELECT * FROM agents WHERE status='active' AND trigger='event'"
                           " AND (event=? OR event LIKE ?)", (kind, f"{kind}:%")).fetchall()
    finally:
        con.close()
    return [_agent_out(r) for r in rows]


def pending_approvals(user: dict) -> dict:
    """What waits for a human: proposals, pending changes, runs blocked on a tool."""
    agents_ = [a for a in list_agents(user) if a["status"] == "pending" or a["pending_change"]]
    con = _con()
    try:
        rows = con.execute("SELECT r.*, a.name AS agent_name, a.owner AS owner FROM runs r"
                           " JOIN agents a ON a.id = r.agent_id"
                           " WHERE r.status='running' AND r.waiting_approval=1").fetchall()
    finally:
        con.close()
    runs = []
    for r in rows:
        d = _run_out(r)
        if can_read(user, {"owner": d.pop("owner")}):
            runs.append(d)
    return {"agents": agents_, "runs": runs}


def redact(text: str, secrets: dict[str, str]) -> str:
    """Replace every secret VALUE by [secret:NAME] (longest first)."""
    if not text:
        return text
    for name, val in sorted(secrets.items(), key=lambda kv: -len(kv[1] or "")):
        if val and len(val) >= 4:
            text = text.replace(val, f"[secret:{name}]")
    return text


def public(a: dict) -> dict:
    """Agent view safe for a model / the UI (it holds no secret value anyway)."""
    keep = ("id", "name", "owner", "model", "purpose", "deliverable", "done_criteria",
            "playbook", "trigger", "schedule", "timezone", "once_at", "event", "tools", "mcp",
            "auto_approve", "secrets", "budget_usd", "max_minutes", "outputs", "notify_on",
            "status", "pending_change", "created_by", "approved_by", "approved_at",
            "next_run_at", "last_run_at", "stats", "last_run")
    return {k: a[k] for k in keep if k in a}
