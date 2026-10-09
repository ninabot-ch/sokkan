#!/usr/bin/env python3
"""projectgate.py — SOKKAN 3.2 lot 3: every cockpit request is about ONE project.

The middleware (`app.require_auth`) calls `resolve(request, user)` once the identity is
known. It decides which project the request is about and with which role:

* a route that names an object (session, card, agent, run, quarantined note) is about THAT
  object's project — whatever the request says (a card of project X is never reachable
  through project Y);
* a list or a creation is about the project the cockpit selected: header
  ``x-sokkan-project`` (or ``?project=``), ``default`` when absent;
* terminal / tmux / preview routes are about the ``default`` project (decision of 07.10:
  no raw terminal elsewhere before the sandbox);
* Operate infrastructure routes need the ops team or an instance admin (`projects.is_ops`);
* instance administration keeps the instance role (no project context).

The person's role in the project then REPLACES their role for the rest of the request
(`current()` is read by `auth.current_user`), mapped onto the instance scale the existing
checks use: viewer → viewer, dev → dev, maintainer → admin, admin → admin. So the 3.1
checks (`require("dev")`, Crew ownership rules, board viewer rules…) apply per project
without being rewritten. No role in the project → 404 (the project's objects do not
exist for that person), never a hint.
"""
from __future__ import annotations

import contextvars
import re

import projects

_CURRENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "sokkan_project_ctx", default=None)

PROJECT_TO_INSTANCE = {"viewer": "viewer", "dev": "dev", "maintainer": "admin",
                       "admin": "admin"}
HEADER = "x-sokkan-project"

# (methods or "*", path regex, object kind) — the object's project decides
_OBJECT_ROUTES = [
    ("*", re.compile(r"^/api/sessions/(?P<id>[^/]+)(/.*)?$"), "session"),
    ("*", re.compile(r"^/api/board/card/(?P<id>\d+)(/.*)?$"), "card"),
    ("*", re.compile(r"^/api/agents/runs/(?P<id>\d+)(/.*)?$"), "run"),
    ("*", re.compile(r"^/api/agents/(?P<id>\d+)(/.*)?$"), "agent"),
]
# the selected project (header / query)
_PROJECT_PREFIXES = (
    "/api/sessions", "/api/spawn", "/api/board", "/api/tags", "/api/agents",
    "/api/agent/", "/api/memory/notes", "/api/memory/search", "/api/memory/note/",
    "/api/memory/recall-log", "/api/memory/quarantine", "/api/memory/digest",
    "/api/memory/stats", "/api/corthexis", "/api/runbooks", "/api/assistant",
    "/api/bindings", "/api/usage", "/api/vault", "/api/playbooks", "/api/budgets",
    "/api/classification", "/api/alerting",
)
# raw terminal, tmux, previews of the instance's repositories: default project only
# (/term itself = the instance role, which IS the role in the default project)
_DEFAULT_ONLY = ("/api/send", "/api/tmux", "/api/preview")
# Operate / infrastructure: ops team or instance admin
_OPS_PREFIXES = ("/api/observability", "/api/infra")
_OPS_FREE = ("/api/observability/alert",)     # own token auth (alert receiver)


def current() -> dict | None:
    """The project context of the request being served (None = instance-level route)."""
    return _CURRENT.get()


def requested_project(request) -> str:
    p = (request.headers.get(HEADER) or request.query_params.get("project") or "").strip()
    return p or projects.DEFAULT_PROJECT


def object_project(kind: str, oid: str) -> str | None:
    """Project of an object, None = unknown object (the route answers 404 itself)."""
    try:
        if kind == "session":
            import board
            return board.get_session_project(oid)
        if kind == "card":
            import board
            c = board.get_card(int(oid))
            return (c or {}).get("project") or (projects.DEFAULT_PROJECT if c else None)
        if kind == "agent":
            import agents
            a = agents.get(int(oid))
            return (a or {}).get("project") or (projects.DEFAULT_PROJECT if a else None)
        if kind == "run":
            import agents
            r = agents.get_run(int(oid))
            a = agents.get(r["agent_id"]) if r else None
            return (a or {}).get("project") or (projects.DEFAULT_PROJECT if a else None)
    except (ValueError, TypeError):
        return None
    return None


def project_user(user: dict, slug: str) -> dict | None:
    """The person as seen inside ``slug`` (role mapped), None = no access."""
    role = projects.effective_role(user, slug)
    if role is None:
        return None
    pu = {**user, "role": PROJECT_TO_INSTANCE[role], "project": slug,
          "project_role": role, "instance_role": user.get("role")}
    import classification
    if classification.enabled():
        # 3.4: the person's clearance in this project, for every filter of the request
        pu["clearance"] = classification.clearance(user, slug)
    return pu


def object_level(kind: str, oid: str) -> int | None:
    """Classification of an object (3.4): a card's own level; a session or a run = the
    highest level of the notes its session obtained (its transcript holds them)."""
    try:
        if kind == "card":
            import board
            c = board.get_card(int(oid))
            return None if not c else int(c.get("level") if c.get("level") is not None else 2)
        import classification
        if kind == "session":
            return classification.session_level(oid)
        if kind == "run":
            import agents
            r = agents.get_run(int(oid))
            return classification.session_level(r.get("session_id")) if r else None
    except (ValueError, TypeError):
        return None
    return None


def above_clearance(pu: dict, kind: str, oid: str) -> bool:
    c = pu.get("clearance")
    if c is None:
        return False
    lvl = object_level(kind, oid)
    return lvl is not None and lvl > c


class Denied(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def resolve(request, user: dict) -> contextvars.Token | None:
    """Set the request's project context, or raise Denied. Returns the contextvar token
    (None = instance-level route, nothing set)."""
    path = request.url.path
    if path.startswith(_OPS_PREFIXES) and not path.startswith(_OPS_FREE):
        if not projects.is_ops(user):
            raise Denied(403, "Operate is for the ops team and the instance admins")
        return None
    slug = None
    obj = None
    for _m, rx, kind in _OBJECT_ROUTES:
        m = rx.match(path)
        if m:
            obj = (kind, m.group("id"))
            slug = object_project(kind, m.group("id"))
            if slug is None:
                if kind == "session" and not projects.multi_project():
                    slug = projects.DEFAULT_PROJECT   # a session SOKKAN does not own (3.1)
                else:
                    # unknown object: the route itself answers 404 — but in the
                    # requested project's context, never with the instance role
                    slug = requested_project(request)
            break
    if slug is None:
        if path.startswith(_DEFAULT_ONLY):
            slug = projects.DEFAULT_PROJECT
        elif path.startswith(_PROJECT_PREFIXES):
            slug = requested_project(request)
        else:
            return None
    pu = project_user(user, slug)
    if pu is None:
        raise Denied(404, f"no project '{slug}' for you")
    if obj is not None and above_clearance(pu, *obj):
        # 3.4: an object above the person's clearance does not exist for them
        raise Denied(404, "not found")
    return _CURRENT.set(pu)


def reset(token) -> None:
    if token is not None:
        _CURRENT.reset(token)


def ws_user(user: dict, slug: str | None, session_id: str | None = None) -> dict | None:
    """WebSocket routes (no middleware): the person inside the session's project (3.4: and
    cleared for what the session obtained)."""
    if slug is None:
        if projects.multi_project():
            return None
        slug = projects.DEFAULT_PROJECT
    pu = project_user(user, slug)
    if pu is not None and session_id and above_clearance(pu, "session", session_id):
        return None
    return pu
