#!/usr/bin/env python3
"""classification.py — SOKKAN 3.4: classification levels and clearances.

Every note, decision, card and agent deliverable carries a LEVEL on one ordered scale
(``memory/core/levels.py``)::

    public < team < project < confidential < restricted      (default: project)

Every person carries a CLEARANCE per project: the highest of

* the level of their role in that project (``SOKKAN_CLEARANCE_ROLES`` / admin screen; every
  role reads up to ``project`` by default — the 3.2 behaviour);
* the levels mapped to their SSO groups (``clearance_groups``: team ``sso:<group>`` → level,
  for one project or every project ``*``), set in the admin screen.

No role in the project = no clearance at all (the project does not exist for them).

What reads memory or cards asks for a SCOPE: ``(project@clearance, shared@clearance)`` —
the memory engine (``core.scope``) filters every stage of every search with it. Nina, the
recall, the MCP servers, the cockpit and Teams all build the scope of THE PERSON they act
for (``scope_for``): there is no service view.

Off (`classification` feature off): every scope is the 3.2 one, which reads up to the
default level — a note classified above ``project`` stays out of reach of everyone
(turning the feature off never opens data).

The derived inherits the highest level of its sources (`derive`, `session_level`): a note
written by a session, a run's deliverable, a Nina answer, a decision captured in a Teams
thread. Lowering a level takes a person cleared for the current level, with a reason, and
is journaled (`set_note_level`, `set_card_level`).

Audited recall: the memory store logs every note handed out (``note_access``: via spawn,
prompt, subagent, mcp, cockpit, nina, brief, teams); `access_log` serves it to the project
admins, CSV export included.
"""
from __future__ import annotations

import csv
import datetime
import io
import os
import re
import time

import features
import projects

ROLE_DEFAULTS = {"viewer": 2, "dev": 2, "maintainer": 2, "admin": 2}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clearance_groups (
    team_id TEXT NOT NULL,               -- 'sso:<group>' | 'local:<slug>' | 'user:<email>'
    project TEXT NOT NULL DEFAULT '*',   -- '*' = every project
    level INTEGER NOT NULL CHECK (level BETWEEN 0 AND 4),
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (team_id, project)
);
CREATE TABLE IF NOT EXISTS clearance_roles (
    role TEXT PRIMARY KEY CHECK (role IN ('viewer', 'dev', 'maintainer', 'admin')),
    level INTEGER NOT NULL CHECK (level BETWEEN 0 AND 4)
);
"""
_ready_for: str | None = None


def _lv():
    from core import levels
    return levels


def _sc():
    from core import scope
    return scope


def enabled() -> bool:
    return features.enabled("classification")


def _con():
    global _ready_for
    con = projects._con()
    if _ready_for != str(projects.DB):
        con.executescript(_SCHEMA)
        con.commit()
        _ready_for = str(projects.DB)
    return con


# ---- configuration ------------------------------------------------------------------------
def _env_roles() -> dict[str, int]:
    out = {}
    for part in (os.environ.get("SOKKAN_CLEARANCE_ROLES") or "").split(","):
        role, _, lvl = part.partition("=")
        r = _lv().parse(lvl)
        if role.strip() in ROLE_DEFAULTS and r is not None:
            out[role.strip()] = r
    return out


def role_levels() -> dict[str, int]:
    """Level each project role reads up to: defaults < env < admin screen."""
    out = {**ROLE_DEFAULTS, **_env_roles()}
    con = _con()
    try:
        for r in con.execute("SELECT role, level FROM clearance_roles"):
            out[r["role"]] = int(r["level"])
    finally:
        con.close()
    return out


def set_role_level(role: str, level, by: str = "") -> dict:
    if role not in ROLE_DEFAULTS:
        raise ValueError(f"role must be one of {', '.join(ROLE_DEFAULTS)}")
    lvl = _parse_strict(level)
    con = _con()
    with con:
        con.execute("INSERT OR REPLACE INTO clearance_roles(role, level) VALUES(?,?)",
                    (role, lvl))
    con.close()
    return role_levels()


def _parse_strict(level) -> int:
    lv = _lv()
    v = str(level).strip().lower()
    if v.isdigit() and 0 <= int(v) <= lv.MAX:
        return int(v)
    if v in lv.IDS:
        return lv.IDS.index(v)
    low = [x.lower() for x in lv.labels()]
    if v in low:
        return low.index(v)
    raise ValueError(f"unknown level {level!r}: one of {', '.join(lv.IDS)}")


def group_map() -> list[dict]:
    con = _con()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT team_id, project, level, created_at, created_by FROM clearance_groups "
            "ORDER BY team_id, project")]
    finally:
        con.close()
    for r in rows:
        r["level_id"] = _lv().ident(r["level"])
    return rows


_PRINCIPAL = re.compile(r"^(sso|local):[^\s]{1,200}$|^user:[^@\s]+@[^\s]+$")


def set_group_level(team_id: str, level, project: str = "*", by: str = "") -> list[dict]:
    team_id = (team_id or "").strip()
    if not team_id.startswith(("sso:", "local:", "user:")):
        team_id = f"sso:{team_id}"          # a bare IdP group name
    if not _PRINCIPAL.match(team_id):
        raise ValueError("principal: sso:<group> | local:<team> | user:<email>")
    project = (project or "*").strip()
    if project != "*" and not projects.valid_slug(project):
        raise ValueError("project: a project slug or *")
    lvl = _parse_strict(level)
    con = _con()
    with con:
        con.execute("INSERT OR REPLACE INTO clearance_groups(team_id, project, level, "
                    "created_at, created_by) VALUES(?,?,?,?,?)",
                    (team_id, project, lvl, time.time(), by))
    con.close()
    return group_map()


def delete_group_level(team_id: str, project: str = "*") -> list[dict]:
    con = _con()
    with con:
        con.execute("DELETE FROM clearance_groups WHERE team_id=? AND project=?",
                    (team_id, project or "*"))
    con.close()
    return group_map()


# ---- clearance ----------------------------------------------------------------------------
def user_for(email: str) -> dict:
    """The identity of a person known only by email (a session owner, a Teams user)."""
    import iam
    return iam.get_user(email)


def clearance(user: dict, project: str) -> int | None:
    """Highest level ``user`` may read in ``project``; None = no access to the project."""
    role = projects.effective_role(user, project)
    if role is None:
        return None
    lv = _lv()
    if not enabled():
        return lv.DEFAULT
    best = role_levels().get(role, lv.DEFAULT)
    email = (user.get("email") or "").lower().strip()
    principals = projects.team_ids(email) + ([f"user:{email}"] if email else [])
    if principals:
        con = _con()
        try:
            q = ",".join("?" * len(principals))
            r = con.execute(f"SELECT max(level) AS m FROM clearance_groups WHERE team_id IN ({q})"
                            " AND project IN ('*', ?)", (*principals, project)).fetchone()
        finally:
            con.close()
        if r and r["m"] is not None:
            best = max(best, int(r["m"]))
    return best


def scope_for(user: dict | None, project: str | None) -> tuple[str, ...]:
    """Memory scope of ``user`` working in ``project``: the project + shared, each with the
    person's clearance there. No user (an ownerless session) = the 3.2 scope, which reads up
    to the default level only."""
    base = projects.recall_scope(project)
    if not base or not enabled() or not user:
        return base
    out = []
    for p in base:
        c = clearance(user, p)
        if c is not None:
            out.append(_sc().entry(p, c))
    return _sc().normalize(out)


def scope_for_email(email: str, project: str | None) -> tuple[str, ...]:
    if not enabled() or not email or "@" not in email:
        return projects.recall_scope(project)
    try:
        user = user_for(email)
    except Exception as e:  # noqa: BLE001 — unknown identity: the default level only
        print(f"[classification] identity of {email} unreadable ({e!r}): default level")
        return projects.recall_scope(project)
    return scope_for(user, project)


def session_scope(session_id: str | None) -> tuple[str, ...]:
    """Recall scope of a SOKKAN session: its project, with its OWNER's clearance."""
    import board
    project = board.get_session_project(session_id) if session_id else None
    if project is None:
        return projects.session_scope(None)
    owner = board.get_session_owner(session_id) if enabled() else ""
    return scope_for_email(owner, project) if owner else projects.session_scope(project)


def ctx_user() -> dict | None:
    """The person of the request being served, with their INSTANCE role (projectgate
    replaced it by the project role for the 3.1 checks)."""
    import projectgate
    pu = projectgate.current()
    if pu is None:
        return None
    return {**pu, "role": pu.get("instance_role") or pu.get("role")}


def ctx_scope() -> tuple[str, ...]:
    import projectgate
    pu = projectgate.current()
    project = (pu or {}).get("project") or projects.DEFAULT_PROJECT
    return scope_for(ctx_user(), project)


def ctx_clearance() -> int | None:
    """Clearance of the request's person in the request's project (None = no filter:
    feature off keeps the 3.2 behaviour where a card has no level)."""
    if not enabled():
        return None
    import projectgate
    pu = projectgate.current()
    if pu is None:
        return None
    if "clearance" in pu:
        return pu["clearance"]
    return clearance(ctx_user(), pu["project"])


def own_entry(scope: tuple[str, ...], project: str) -> list[str]:
    """The entry of ``project`` alone (its own notes, not shared), clearance kept."""
    return [e for e in scope if (_sc().parse_entry(e) or ("",))[0] == project]


# ---- derived content ----------------------------------------------------------------------
def derive(*levels_) -> int:
    """The derived inherits the highest level of its sources."""
    return _lv().highest(levels_)


def session_level(session_id: str | None) -> int | None:
    """Highest level a session (or an agent run's session) has obtained; None = unknown."""
    if not session_id:
        return None
    try:
        import store_backend
        if not store_backend.enabled():
            return None
        return store_backend.get_store().session_level(session_id)
    except Exception:  # noqa: BLE001
        return None


def redact_run(run: dict, cap: int | None) -> dict:
    """An agent run whose session obtained notes above ``cap``: the row stays (status, cost,
    when), its deliverable / outputs / error are withheld."""
    if cap is None or not run:
        return run
    lvl = session_level(run.get("session_id"))
    if lvl is None or lvl <= cap:
        return run
    return {**run, "deliverable": "", "outputs": {}, "error": "",
            "classified": _lv().ident(lvl),
            "classified_note": "classified above your clearance"}


def env_cap() -> int | None:
    """Clearance of an MCP server's session in its project (SOKKAN_SESSION_SCOPE)."""
    p = os.environ.get("SOKKAN_SESSION_PROJECT")
    if p is None:
        return None
    raw = [e.strip() for e in (os.environ.get("SOKKAN_SESSION_SCOPE") or "").split(",")]
    caps = _sc().caps(_sc().normalize(raw)) or {}
    return caps.get(p.strip(), _lv().DEFAULT)


def log_access(via: str, notes, *, actor: str | None = None, session_id: str | None = None,
               query: str | None = None) -> None:
    """Audited recall for the app's own paths (cockpit, Nina, brief, Teams)."""
    try:
        import store_backend
        if store_backend.enabled() and notes:
            store_backend.get_store().log_access(via, notes, actor=actor, session_id=session_id,
                                                 query=query)
    except Exception as e:  # noqa: BLE001 — a read never fails on its log line
        print(f"[classification] access log failed: {e!r}")


# ---- (re)classification ---------------------------------------------------------------------
class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)


def set_frontmatter_level(text: str, level: int) -> str:
    """The note text with ``classification: <id>`` in its frontmatter (added or replaced)."""
    ident = _lv().ident(level)
    m = _FM.search(text)
    if not m:
        return f"---\nclassification: {ident}\n---\n{text}"
    block = m.group(1)
    if re.search(r"^classification:.*$", block, re.M):
        block = re.sub(r"^classification:.*$", f"classification: {ident}", block, count=1,
                       flags=re.M)
    else:
        lines = block.split("\n")
        at = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("description:")),
                  len(lines))
        lines.insert(at, f"classification: {ident}")
        block = "\n".join(lines)
    return text[:m.start()] + f"---\n{block}\n---\n" + text[m.end():]


def check_change(user: dict, project: str, current: int, new: int, reason: str) -> None:
    """Who may move an object from ``current`` to ``new`` (raises Refused)."""
    c = clearance(user, project)
    if c is None or c < current:
        raise Refused(404, "not found")
    role = projects.effective_role(user, project)
    if projects.prank(role) < projects.prank("dev"):
        raise Refused(403, "role dev required to classify")
    if new < current:
        if projects.prank(role) < projects.prank("maintainer"):
            raise Refused(403, "lowering a level takes a maintainer or admin of the project "
                               "cleared for the current level")
        if not (reason or "").strip():
            raise Refused(400, "a reason is required to lower a level (it is journaled)")


def set_note_level(user: dict, project: str, name: str, level, reason: str = "") -> dict:
    """Reclassify a note: rewrite its file's frontmatter and its level + floor in the store.
    Lowering = a cleared maintainer/admin with a reason; journaled either way."""
    import audit
    import store_backend
    if not store_backend.enabled():
        raise Refused(503, "classification of notes needs the memory store (3.0)")
    new = _parse_strict(level)
    st = store_backend.get_store()
    cur = st.note_level(name, project)
    if cur is None:
        raise Refused(404, "not found")
    check_change(user, project, cur, new, reason)
    path = _note_path(st, name, project)
    if path is not None and path.exists():
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(set_frontmatter_level(path.read_text(encoding="utf-8"), new),
                       encoding="utf-8")
        tmp.replace(path)
    st.set_level(name, new, project=project, by=user.get("email") or "", reason=reason)
    audit.log(user.get("email") or "", "classification.note.lower" if new < cur else
              "classification.note.set", f"{project}/{name}",
              f"{_lv().ident(cur)} → {_lv().ident(new)}" + (f" — {reason}" if reason else ""))
    return {"note": name, "project": project, "level": _lv().ident(new),
            "previous": _lv().ident(cur)}


def _note_path(st, name, project):
    from pathlib import Path
    try:
        rec = st.get_note(name, project)
    except Exception:  # noqa: BLE001
        return None
    return Path(rec.source_path) if rec and rec.source_path else None


def set_card_level(user: dict, card: dict, level, reason: str = "") -> dict:
    import audit
    import board
    new = _parse_strict(level)
    cur = int(card.get("level") if card.get("level") is not None else _lv().DEFAULT)
    project = card.get("project") or projects.DEFAULT_PROJECT
    check_change(user, project, cur, new, reason)
    out = board.set_card_level(card["id"], new, user=user.get("email") or "", reason=reason,
                               origin={"via": "web"})
    audit.log(user.get("email") or "", "classification.card.lower" if new < cur else
              "classification.card.set", f"{project}/card#{card['id']}",
              f"{_lv().ident(cur)} → {_lv().ident(new)}" + (f" — {reason}" if reason else ""))
    return out


# ---- audited recall -----------------------------------------------------------------------
def access_log(project: str, *, reader_clearance: int | None, actor: str = "", note: str = "",
               session: str = "", since_days: int = 30, limit: int = 1000,
               stats: dict | None = None) -> list[dict]:
    """Who obtained which note of ``project`` (and of nothing else), via which path. The
    entries about notes above the reader's own clearance are left out — and COUNTED in
    ``stats["hidden"]`` (3.4): an auditor knows that reads they may not see exist."""
    import store_backend
    if not store_backend.enabled():
        return []
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=max(1, min(int(since_days), 3650)))
    rows = store_backend.get_store().access_log(
        projects=[project], actor=actor or None, note=note or None, session_id=session or None,
        since=since, limit=limit)
    owners: dict[str, str] = {}
    import board
    out = []
    hidden = 0
    for r in rows:
        if reader_clearance is not None and int(r["level"]) > reader_clearance:
            hidden += 1
            continue
        if not r.get("actor") and r.get("session_id"):
            sid = r["session_id"]
            if sid not in owners:
                try:
                    owners[sid] = board.get_session_owner(sid)
                except Exception:  # noqa: BLE001
                    owners[sid] = ""
            r["actor"] = owners[sid] or None
            r["actor_source"] = "session owner"
        r["level"] = _lv().ident(r["level"])
        out.append(r)
    if stats is not None:
        stats["hidden"] = hidden
    return out


def access_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["at", "actor", "via", "project", "note_name", "level", "session_id", "query"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) if r.get(c) is not None else "" for c in cols])
    return buf.getvalue()


def describe() -> dict:
    """Public description for the cockpit: the scale (with the customer's labels)."""
    return {"enabled": enabled(), "scale": _lv().scale(), "default": _lv().ident(_lv().DEFAULT)}
