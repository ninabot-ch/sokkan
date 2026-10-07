#!/usr/bin/env python3
"""sharing.py — SOKKAN 3.2 `shared_review`: share a session or a preview for review.

A share puts ONE object (a session, or a preview = a URL of the instance's dev servers)
in front of ONE person or ONE team of the object's project, read or read-write, until an
optional expiry or a revocation. It is an invitation, never a key:

* it can only name people / teams that already have a role in the object's project (a
  non-member cannot be a recipient, and sees nothing — the API answers as if the share did
  not exist);
* write is refused at creation for a recipient whose project role is below dev (a viewer
  never gets write), and downgraded to read at use time if their role dropped since;
* a write share can carry the session's delegated HITL approval: the owner « asks X to
  validate » — X sees the pending tool approvals of that session and decides them;
* everything is logged in the journal (create, approver, view, decision, revoke).

Previews are served by the default project only (decision of 07.10: no raw terminal /
preview elsewhere before the sandbox), so a preview share lives in `default`.
Store: ``$SOKKAN_DATA_DIR/shares.db`` (SQLite).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import projects

DB = Path(os.environ.get("SOKKAN_SHARES_DB", os.path.join(
    os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "shares.db")))
KINDS = ("session", "preview")
ACCESS = ("read", "write")
MAX_TTL_S = 90 * 86400


class ShareError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
            title TEXT DEFAULT '', preview TEXT DEFAULT '',
            principal_kind TEXT NOT NULL, principal TEXT NOT NULL,
            access TEXT NOT NULL, approver INTEGER NOT NULL DEFAULT 0,
            note TEXT DEFAULT '', created_by TEXT NOT NULL, created_at REAL NOT NULL,
            expires_at REAL, revoked_at REAL, revoked_by TEXT DEFAULT ''
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_shares_target ON shares(kind, target)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_shares_principal ON shares(principal_kind, principal)")
    return con


# ---- roles ----------------------------------------------------------------------------

def _person(email: str) -> dict:
    import iam
    return iam.get_user(email)


def role_of(email: str, project: str) -> str | None:
    """Project role of a person given by email (instance role + grants + teams)."""
    p = _person(email)
    return projects.effective_role({"email": p["email"], "role": p["role"]}, project)


def _team_role(team: str, project: str) -> str | None:
    roles = [g["role"] for g in projects.list_grants(project)
             if g["principal_kind"] == "team" and g["principal"] == team]
    return max(roles, key=projects.prank) if roles else None


def _known_person(email: str) -> bool:
    if _person(email).get("known"):
        return True
    return bool(projects.team_ids(email))


def object_project(kind: str, target: str) -> str | None:
    if kind == "session":
        import board
        return board.get_session_project(target)
    if kind == "preview":
        return projects.DEFAULT_PROJECT
    return None


# ---- create / revoke ------------------------------------------------------------------

def create(user: dict, kind: str, target: str, principal_kind: str, principal: str,
           access: str = "read", expires_in_s: int | None = None, approver: bool = False,
           note: str = "", title: str = "", preview: dict | None = None) -> dict:
    if kind not in KINDS:
        raise ShareError(400, "kind: session | preview")
    if access not in ACCESS:
        raise ShareError(400, "access: read | write")
    if principal_kind not in ("user", "team"):
        raise ShareError(400, "principal_kind: user | team")
    target = (target or "").strip()
    if not target or len(target) > 2000:
        raise ShareError(400, "target required")
    if kind == "preview":
        if not target.startswith(("http://", "https://")):
            raise ShareError(400, "a preview share needs the preview URL (http/https)")
    elif "/" in target or ".." in target:
        raise ShareError(400, "invalid session id")
    slug = object_project(kind, target)
    if slug is None:
        raise ShareError(404, "not found")
    my_role = projects.effective_role(user, slug)
    if my_role is None:
        raise ShareError(404, "not found")            # never a hint about another project
    if projects.prank(my_role) < projects.prank("dev"):
        raise ShareError(403, "sharing needs the dev role in the project")
    principal = principal.strip()
    if principal_kind == "user":
        principal = principal.lower()
        if principal == (user.get("email") or "").lower():
            raise ShareError(400, "you cannot share with yourself")
        rrole = role_of(principal, slug) if _known_person(principal) else None
    else:
        rrole = _team_role(principal, slug)
    if rrole is None:
        raise ShareError(400, f"{principal} has no role in project '{slug}' — share with a "
                              "person or a team of the project")
    if access == "write" and projects.prank(rrole) < projects.prank("dev"):
        raise ShareError(400, f"{principal} is {rrole} in '{slug}': a viewer never gets write")
    if approver and access != "write":
        raise ShareError(400, "delegated approval needs a write share")
    if approver and kind != "session":
        raise ShareError(400, "delegated approval is for a session")
    now = time.time()
    exp = None
    if expires_in_s:
        if not 60 <= int(expires_in_s) <= MAX_TTL_S:
            raise ShareError(400, "expires_in_s: 60 s to 90 days")
        exp = now + int(expires_in_s)
    pv = json.dumps({k: str(v)[:500] for k, v in (preview or {}).items()
                     if k in ("path", "env", "label")}) if kind == "preview" else ""
    con = _con()
    with con:
        cur = con.execute(
            "INSERT INTO shares(project, kind, target, title, preview, principal_kind, principal,"
            " access, approver, note, created_by, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, kind, target, (title or "")[:120], pv, principal_kind, principal, access,
             int(bool(approver)), (note or "")[:500], user.get("email") or "", now, exp))
        sid = cur.lastrowid
    con.close()
    return get(sid)


def get(share_id: int) -> dict | None:
    con = _con()
    r = con.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone()
    con.close()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    d["approver"] = bool(d["approver"])
    d["preview"] = json.loads(d["preview"]) if d.get("preview") else None
    return d


def active(s: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return not s.get("revoked_at") and (not s.get("expires_at") or s["expires_at"] > now)


def can_manage(user: dict, s: dict) -> bool:
    """The creator, or a maintainer/admin of the project, manages (revokes) a share."""
    role = projects.effective_role(user, s["project"])
    if role is None:
        return False
    return s["created_by"] == (user.get("email") or "") or \
        projects.prank(role) >= projects.prank("maintainer")


def revoke(user: dict, share_id: int) -> dict:
    s = get(share_id)
    if s is None or not can_manage(user, s):
        raise ShareError(404, "share not found")
    if s["revoked_at"]:
        return s
    con = _con()
    with con:
        con.execute("UPDATE shares SET revoked_at=?, revoked_by=? WHERE id=?",
                    (time.time(), user.get("email") or "", share_id))
    con.close()
    return get(share_id)


def set_approver(user: dict, share_id: int, on: bool) -> dict:
    s = get(share_id)
    if s is None or not can_manage(user, s) or not active(s):
        raise ShareError(404, "share not found")
    if on and (s["access"] != "write" or s["kind"] != "session"):
        raise ShareError(400, "delegated approval needs a write share of a session")
    con = _con()
    with con:
        con.execute("UPDATE shares SET approver=? WHERE id=?", (int(bool(on)), share_id))
    con.close()
    return get(share_id)


def list_for_object(user: dict, kind: str, target: str) -> list[dict]:
    """Shares of one object, for people of its project with dev+ (who could share it)."""
    slug = object_project(kind, target)
    if slug is None or not projects.can(user, slug, "dev"):
        raise ShareError(404, "not found")
    con = _con()
    rows = [_row(r) for r in con.execute(
        "SELECT * FROM shares WHERE kind=? AND target=? ORDER BY created_at DESC", (kind, target))]
    con.close()
    return rows


# ---- the recipient's view -------------------------------------------------------------

def for_recipient(user: dict, s: dict) -> dict | None:
    """The share as the recipient may use it, None = not theirs (or no longer valid).
    Effective access = the share's, capped by the CURRENT project role."""
    if not active(s):
        return None
    email = (user.get("email") or "").lower()
    if s["principal_kind"] == "user":
        if s["principal"] != email:
            return None
    elif s["principal"] not in projects.team_ids(email):
        return None
    role = projects.effective_role(user, s["project"])
    if role is None:                       # left the project: nothing, not even a title
        return None
    write = s["access"] == "write" and projects.prank(role) >= projects.prank("dev")
    return {**s, "effective_access": "write" if write else "read", "my_role": role,
            "approver": bool(s["approver"] and write)}


def inbox(user: dict) -> list[dict]:
    email = (user.get("email") or "").lower()
    teams = projects.team_ids(email)
    con = _con()
    q = "SELECT * FROM shares WHERE revoked_at IS NULL AND ((principal_kind='user' AND principal=?)"
    args: list = [email]
    if teams:
        q += f" OR (principal_kind='team' AND principal IN ({','.join('?' * len(teams))}))"
        args += teams
    q += ") ORDER BY created_at DESC"
    rows = [_row(r) for r in con.execute(q, args)]
    con.close()
    out = []
    for s in rows:
        v = for_recipient(user, s)
        if v is not None:
            if v["approver"]:
                v["pending"] = pending_approvals(v["target"])
            out.append(v)
    return out


def recipient_share(user: dict, share_id: int) -> dict:
    s = get(share_id)
    v = for_recipient(user, s) if s else None
    if v is None:
        raise ShareError(404, "share not found")
    return v


# ---- delegated HITL approval ------------------------------------------------------------

def pending_approvals(session_id: str) -> list[dict]:
    """Tool approvals the session is waiting for (live chat session in this API)."""
    import agentchat
    sess = agentchat.peek(session_id)
    if sess is None:
        return []
    waiting = set(getattr(sess, "_perms", {}) or {})
    out = []
    for ev in list(getattr(sess, "events", []) or []):
        if ev.get("type") == "permission" and ev.get("id") in waiting:
            out.append({"id": ev["id"], "tool": ev.get("tool", ""), "title": ev.get("title", "")})
    return out


def decide(user: dict, share_id: int, permission_id: str, allow: bool, message: str = "") -> dict:
    v = recipient_share(user, share_id)
    if v["kind"] != "session" or not v["approver"]:
        raise ShareError(403, "this share does not delegate the approval to you")
    pend = {p["id"]: p for p in pending_approvals(v["target"])}
    if permission_id not in pend:
        raise ShareError(404, "no such pending approval (already decided?)")
    import agentchat
    sess = agentchat.peek(v["target"])
    who = user.get("email") or ""
    sess.resolve_permission(permission_id, {
        "decision": "allow" if allow else "deny",
        "message": None if allow else (message or f"Denied by {who} (delegated review)")})
    return {"ok": True, "decision": "allow" if allow else "deny", "tool": pend[permission_id]["tool"],
            "title": pend[permission_id]["title"], "session_id": v["target"]}
