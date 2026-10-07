#!/usr/bin/env python3
"""revocation.py — SOKKAN 3.2 lot 6: a person who leaves loses access NOW.

Feature `revocation` (requires `sso_teams`). Four paths, one effect (`revoke`):

* **SCIM 2.0** (`/api/scim/v2`, bearer `SOKKAN_SCIM_TOKEN`): the IdP (Entra ID, Authentik,
  Okta…) provisions Users (create / deactivate / delete) and Groups (membership = SOKKAN SSO
  teams). `active=false` or DELETE → `revoke`. A group membership removed → the person's
  sessions and agents in projects they no longer reach are closed / paused (`reconcile`).
* **« Revoke now »** (admin, `POST /api/admin/users/{email}/revoke`): same effect, by hand.
* **SSO login**: teams are recomputed from the `groups` claim, then `reconcile`.
* **Scheduler**: before each run, the owner's access is checked (`owner_may_run`); an agent
  never outlives its owner's access (paused + notification).

`revoke(email)`:
  1. account disabled + every cockpit cookie issued before now refused (`cookie_ok`);
  2. the person's live WebSockets closed (chat panes, terminal);
  3. agents they own paused, their queued / running runs cancelled;
  4. their live SDK sessions stopped cleanly (interrupt, then close);
  5. forge tokens erased (`forge_links`: token + refresh token blanked, `revoked_at`) — and
     revoked at the forge first when the provider module offers it (lot 5);
  6. `access_cache` purged, SSO team memberships removed;
  7. audit `user.revoke` with the counts.

State lives in ``$SOKKAN_DATA_DIR/identity.db`` (created on first use only: an instance that
never revoked anyone has no file, and the per-request check is a stat).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

DB: Path | None = None            # tests set it; None = $SOKKAN_DATA_DIR/identity.db
_lock = threading.Lock()
_init_for: Path | None = None
_loop: asyncio.AbstractEventLoop | None = None
_sockets: dict[str, set] = {}     # email → live WebSockets (chat panes, terminals)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    email TEXT PRIMARY KEY,
    disabled_at REAL,
    disabled_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    sessions_valid_after REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scim_users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    user_name TEXT NOT NULL UNIQUE,
    external_id TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scim_groups (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL UNIQUE,
    external_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scim_group_members (
    group_id TEXT NOT NULL REFERENCES scim_groups(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES scim_users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
"""


def _path() -> Path:
    if DB is not None:
        return DB
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
                ) / "identity.db"


def _con(create: bool = True) -> sqlite3.Connection | None:
    global _init_for
    p = _path()
    if not create and not p.exists():
        return None
    with _lock:
        if _init_for != p or not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(p)
            c.executescript(_SCHEMA)
            c.commit()
            c.close()
            _init_for = p
    con = sqlite3.connect(p, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _norm(email: str) -> str:
    return (email or "").lower().strip()


def enabled() -> bool:
    import features
    return features.enabled("revocation")


def scim_token() -> str:
    return (os.environ.get("SOKKAN_SCIM_TOKEN") or "").strip()


def readiness() -> str | None:
    if not scim_token():
        return "SOKKAN_SCIM_TOKEN not set: SCIM endpoint closed (« Revoke now » works)"
    return None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """The API's event loop (lifespan): sync routes schedule session closes on it."""
    global _loop
    _loop = loop


# ---- account state (read on every request: cheap, and absent file = nothing) ----------

def _state(email: str) -> dict | None:
    con = _con(create=False)
    if con is None:
        return None
    try:
        r = con.execute("SELECT * FROM account_state WHERE email=?", (_norm(email),)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def is_disabled(email: str) -> bool:
    s = _state(email)
    return bool(s and s.get("disabled_at"))


def cookie_ok(email: str, iat: float) -> bool:
    """A cockpit cookie is honoured only if the account is not disabled and the cookie
    was issued after the person's last revocation."""
    s = _state(email)
    if not s:
        return True
    if s.get("disabled_at"):
        return False
    return float(iat or 0) > float(s.get("sessions_valid_after") or 0)


def _set_state(email: str, *, disabled: bool | None, by: str = "", reason: str = "",
               cut_sessions: bool = False) -> None:
    now = time.time()
    con = _con()
    assert con is not None
    with con:
        con.execute("INSERT OR IGNORE INTO account_state(email, updated_at) VALUES(?,?)",
                    (_norm(email), now))
        if disabled is True:
            con.execute("UPDATE account_state SET disabled_at=?, disabled_by=?, reason=?, "
                        "updated_at=? WHERE email=?", (now, by, reason[:300], now, _norm(email)))
        elif disabled is False:
            con.execute("UPDATE account_state SET disabled_at=NULL, disabled_by='', reason='', "
                        "updated_at=? WHERE email=?", (now, _norm(email)))
        if cut_sessions:
            con.execute("UPDATE account_state SET sessions_valid_after=?, updated_at=? "
                        "WHERE email=?", (now, now, _norm(email)))
    con.close()


def disabled_accounts() -> list[dict]:
    con = _con(create=False)
    if con is None:
        return []
    try:
        return [dict(r) for r in con.execute(
            "SELECT email, disabled_at, disabled_by, reason FROM account_state "
            "WHERE disabled_at IS NOT NULL ORDER BY disabled_at DESC")]
    finally:
        con.close()


# ---- live connections -----------------------------------------------------------------

def track(email: str, ws) -> None:
    _sockets.setdefault(_norm(email), set()).add(ws)


def untrack(email: str, ws) -> None:
    s = _sockets.get(_norm(email))
    if s is not None:
        s.discard(ws)
        if not s:
            _sockets.pop(_norm(email), None)


async def _close_sockets(email: str) -> int:
    n = 0
    for ws in list(_sockets.pop(_norm(email), set())):
        try:
            await ws.close(code=4401)
            n += 1
        except Exception:  # noqa: BLE001 — already gone
            pass
    return n


# ---- owner checks (scheduler, reconcile) ----------------------------------------------

def _person(email: str) -> dict:
    import iam
    return iam.get_user(email)


def project_role(email: str, project: str) -> str | None:
    import projects
    if is_disabled(email):
        return None
    return projects.effective_role(_person(email), project or projects.DEFAULT_PROJECT)


def owner_may_run(a: dict) -> tuple[bool, str]:
    """May this agent's owner still run it? (dev+ in the agent's project, not disabled.)"""
    if not enabled():
        return True, ""
    import projects
    owner = a.get("owner") or ""
    if is_disabled(owner):
        return False, f"owner {owner} was revoked"
    role = project_role(owner, a.get("project") or "default")
    if projects.prank(role) < projects.prank("dev"):
        return False, (f"owner {owner} no longer has the dev role in project "
                       f"{a.get('project') or 'default'}")
    return True, ""


def pause_agent(a: dict, why: str, by: str = "sokkan") -> bool:
    """Pause an agent whose owner lost access; cancel its queued / running runs; notify."""
    import agents
    import agents_runtime
    import audit
    cur = agents.get(a["id"]) or a
    changed = False
    if cur.get("status") == "active":
        agents.set_next_run(a["id"], None, status="paused")
        changed = True
    rt = agents_runtime.get_runtime()
    for st in ("queued", "running"):
        for r in agents.runs_with_status(st):
            if r["agent_id"] != a["id"]:
                continue
            if rt is not None and rt.cancel(r["id"]):
                continue
            agents.update_run(r["id"], status="cancelled", error=f"agent paused: {why}",
                              waiting_approval=0)
    if changed:
        audit.log(by, "agent.pause.owner_access", a["name"], why)
        try:
            import notify
            notify.send(f"SOKKAN — ⏸️ agent {a['name']} paused",
                        f"{why}. The agent stays paused until someone with access resumes "
                        "it or takes it over.", notify.PUBLIC_URL, "agent")
        except Exception:  # noqa: BLE001 — a failed notification never blocks the pause
            pass
    return changed


def _owned_agents(email: str) -> list[dict]:
    import agents
    con = agents._con()
    try:
        rows = con.execute("SELECT id FROM agents WHERE owner=? AND status!='archived'",
                           (_norm(email),)).fetchall()
    finally:
        con.close()
    return [a for a in (agents.get(r["id"]) for r in rows) if a]


async def _stop_session(sid: str, why: str) -> None:
    import agentchat
    s = agentchat.peek(sid)
    if s is None:
        return
    try:
        s._emit({"type": "error", "message": f"Session closed: {why}"})
        if s._busy:
            await asyncio.wait_for(s.interrupt(), 10)
    except Exception:  # noqa: BLE001
        pass
    await agentchat.drop(sid)


def _sessions_of(email: str) -> list[str]:
    """Live interactive SDK sessions opened by the person (agent runs excluded: they end
    with their agent's runs)."""
    import agentchat
    return [sid for sid, s in list(agentchat._registry.items())
            if _norm(s.user) == _norm(email) and not s.policy]


def _erase_forge_tokens(email: str) -> int:
    import projects
    rows = []
    con = projects._con()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM forge_links WHERE email=? AND revoked_at IS NULL", (_norm(email),))]
        # revoke at the forge first when the provider can (lot 5); erase locally always
        try:
            import forge  # type: ignore  # lot 5
            fn = getattr(forge, "revoke_link", None)
        except Exception:  # noqa: BLE001
            fn = None
        for r in rows:
            if fn is not None:
                try:
                    fn(r)
                except Exception as e:  # noqa: BLE001
                    print(f"[revocation] forge revoke failed for {email}: {e!r}", file=sys.stderr)
        with con:
            con.execute("UPDATE forge_links SET token_enc='', refresh_enc='', revoked_at=? "
                        "WHERE email=? AND revoked_at IS NULL", (time.time(), _norm(email)))
            con.execute("DELETE FROM access_cache WHERE email=?", (_norm(email),))
    finally:
        con.close()
    return len(rows)


def _drop_sso_teams(email: str) -> None:
    import projects
    con = projects._con()
    try:
        with con:
            con.execute("DELETE FROM team_members WHERE email=?", (_norm(email),))
    finally:
        con.close()


async def revoke(email: str, by: str, reason: str = "", disable: bool = True) -> dict:
    """The person loses access now (see module doc). Idempotent."""
    import audit
    email = _norm(email)
    why = reason or "access revoked"
    _set_state(email, disabled=True if disable else None, by=by, reason=why, cut_sessions=True)
    sockets = await _close_sockets(email)
    paused = 0
    for a in _owned_agents(email):
        if pause_agent(a, f"owner {email}: {why}", by=by):
            paused += 1
    sessions = _sessions_of(email)
    for sid in sessions:
        await _stop_session(sid, why)
    tokens = _erase_forge_tokens(email)
    if disable:
        _drop_sso_teams(email)
    out = {"email": email, "disabled": disable, "sockets_closed": sockets,
           "sessions_stopped": len(sessions), "agents_paused": paused,
           "forge_tokens_erased": tokens}
    audit.log(by, "user.revoke", email,
              f"{why} · sessions {len(sessions)} · agents {paused} · forge tokens {tokens}")
    return out


def reinstate(email: str, by: str) -> None:
    """Re-enable an account (its agents stay paused: someone resumes them on purpose)."""
    import audit
    _set_state(email, disabled=False, by=by)
    audit.log(by, "user.reinstate", _norm(email), "")


async def reconcile(email: str, by: str = "sokkan") -> dict:
    """After the person's teams changed (SSO login, SCIM group push): close their live
    sessions in projects they no longer reach, pause the agents they may no longer run."""
    import audit
    import board
    email = _norm(email)
    stopped = []
    for sid in _sessions_of(email):
        proj = board.get_session_project(sid) or "default"
        if project_role(email, proj) is None:
            await _stop_session(sid, f"you no longer have access to project {proj}")
            stopped.append(sid)
    paused = 0
    if enabled():
        for a in _owned_agents(email):
            if a.get("status") != "active":
                continue
            ok, why = owner_may_run(a)
            if not ok and pause_agent(a, why, by=by):
                paused += 1
    if stopped or paused:
        audit.log(by, "user.reconcile", email, f"sessions {len(stopped)} · agents {paused}")
    return {"sessions_stopped": len(stopped), "agents_paused": paused}


def run_soon(coro) -> None:
    """Run a coroutine from sync code: on the API loop when bound, else right here."""
    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(coro)
        return
    except RuntimeError:
        pass
    if _loop is not None and _loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, _loop)
    else:
        asyncio.run(coro)


# ---- SCIM 2.0 -------------------------------------------------------------------------
SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
_FILTER = re.compile(r'^\s*([A-Za-z.]+)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.I)


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str = ""):
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def group_team_key(g: dict) -> str:
    """Which SOKKAN team a SCIM group is: `sso:<displayName>` by default — the name the
    `groups` claim carries (Authentik). Entra ID sends group object IDs in the claim
    unless configured otherwise: SOKKAN_SCIM_GROUP_KEY=externalId then."""
    key = (os.environ.get("SOKKAN_SCIM_GROUP_KEY") or "displayName").strip()
    v = g.get("external_id") if key == "externalId" else g.get("display_name")
    return f"sso:{(v or g.get('display_name') or '').strip()}"


def _user_out(r: dict, base: str) -> dict:
    return {"schemas": [SCIM_USER], "id": r["id"], "externalId": r["external_id"] or None,
            "userName": r["user_name"], "displayName": r["display_name"] or r["email"],
            "name": {"formatted": r["display_name"] or r["email"]},
            "emails": [{"value": r["email"], "type": "work", "primary": True}],
            "active": bool(r["active"]),
            "meta": {"resourceType": "User", "created": _iso(r["created_at"]),
                     "lastModified": _iso(r["updated_at"]),
                     "location": f"{base}/Users/{r['id']}"}}


def _group_out(g: dict, members: list[dict], base: str) -> dict:
    return {"schemas": [SCIM_GROUP], "id": g["id"], "externalId": g["external_id"] or None,
            "displayName": g["display_name"],
            "members": [{"value": m["id"], "display": m["email"],
                         "$ref": f"{base}/Users/{m['id']}"} for m in members],
            "meta": {"resourceType": "Group", "created": _iso(g["created_at"]),
                     "lastModified": _iso(g["updated_at"]),
                     "location": f"{base}/Groups/{g['id']}"}}


def _email_of(body: dict) -> str:
    emails = body.get("emails") or []
    prim = [e for e in emails if isinstance(e, dict) and e.get("primary")] or \
        [e for e in emails if isinstance(e, dict)]
    v = (prim[0].get("value") if prim else "") or body.get("userName") or ""
    return _norm(str(v))


def _list(resources: list[dict], start: int = 1, count: int | None = None) -> dict:
    start = max(1, start)
    page = resources[start - 1:] if count is None else resources[start - 1:start - 1 + max(0, count)]
    return {"schemas": [SCIM_LIST], "totalResults": len(resources), "startIndex": start,
            "itemsPerPage": len(page), "Resources": page}


def _filter(flt: str | None) -> tuple[str, str] | None:
    if not flt:
        return None
    m = _FILTER.match(flt)
    if not m:
        raise ScimError(400, f"unsupported filter: {flt}", "invalidFilter")
    return m.group(1).lower(), m.group(2).replace('\\"', '"')


class Scim:
    """SCIM resources over identity.db; side effects through revoke / reconcile."""

    def __init__(self, base: str):
        self.base = base

    # -- users --
    def _user(self, uid: str) -> dict:
        con = _con()
        r = con.execute("SELECT * FROM scim_users WHERE id=?", (uid,)).fetchone()
        con.close()
        if not r:
            raise ScimError(404, f"User {uid} not found")
        return dict(r)

    def list_users(self, flt: str | None, start: int = 1, count: int | None = None) -> dict:
        f = _filter(flt)
        con = _con()
        rows = [dict(r) for r in con.execute("SELECT * FROM scim_users ORDER BY created_at")]
        con.close()
        if f:
            attr, val = f
            col = {"username": "user_name", "externalid": "external_id",
                   "emails.value": "email", "emails": "email", "id": "id"}.get(attr)
            if col is None:
                raise ScimError(400, f"unsupported filter attribute: {attr}", "invalidFilter")
            rows = [r for r in rows if str(r[col]).lower() == val.lower()]
        return _list([_user_out(r, self.base) for r in rows], start, count)

    def get_user(self, uid: str) -> dict:
        return _user_out(self._user(uid), self.base)

    async def create_user(self, body: dict, by: str) -> dict:
        email = _email_of(body)
        user_name = str(body.get("userName") or email).strip()
        if not email or "@" not in email:
            raise ScimError(400, "userName or emails[].value must be an email", "invalidValue")
        now = time.time()
        uid = uuid.uuid4().hex
        con = _con()
        try:
            with con:
                con.execute("INSERT INTO scim_users(id, email, user_name, external_id, display_name,"
                            " active, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                            (uid, email, user_name, str(body.get("externalId") or ""),
                             str(body.get("displayName") or (body.get("name") or {}).get(
                                 "formatted") or ""), 1, now, now))
        except sqlite3.IntegrityError:
            raise ScimError(409, f"User {user_name} already exists", "uniqueness")
        finally:
            con.close()
        import audit
        audit.log(by, "scim.user.create", email, "")
        active = _truthy(body.get("active", True))
        if active:
            if is_disabled(email):
                reinstate(email, by)
        else:
            await self._set_active(uid, False, by)
        return self.get_user(uid)

    async def _set_active(self, uid: str, active: bool, by: str) -> None:
        u = self._user(uid)
        con = _con()
        with con:
            con.execute("UPDATE scim_users SET active=?, updated_at=? WHERE id=?",
                        (int(active), time.time(), uid))
        con.close()
        if not active:
            await revoke(u["email"], by, "deactivated by the identity provider (SCIM)")
        elif is_disabled(u["email"]):
            reinstate(u["email"], by)

    def _set_attrs(self, uid: str, attrs: dict) -> None:
        cols = {k: v for k, v in attrs.items() if v is not None}
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        con = _con()
        try:
            with con:
                con.execute(f"UPDATE scim_users SET {sets}, updated_at=? WHERE id=?",
                            (*cols.values(), time.time(), uid))
        except sqlite3.IntegrityError:
            raise ScimError(409, "userName already used", "uniqueness")
        finally:
            con.close()

    async def replace_user(self, uid: str, body: dict, by: str) -> dict:
        u = self._user(uid)
        email = _email_of(body) or u["email"]
        if email != u["email"]:
            raise ScimError(400, "changing a user's email is not supported; deprovision and "
                                 "provision again", "mutability")
        self._set_attrs(uid, {"user_name": body.get("userName"),
                              "external_id": body.get("externalId"),
                              "display_name": body.get("displayName")})
        if "active" in body and _truthy(body["active"]) != bool(u["active"]):
            await self._set_active(uid, _truthy(body["active"]), by)
        return self.get_user(uid)

    async def patch_user(self, uid: str, body: dict, by: str) -> dict:
        u = self._user(uid)
        for op in body.get("Operations") or []:
            kind = str(op.get("op") or "").lower()
            path = str(op.get("path") or "")
            val = op.get("value")
            if kind not in ("add", "replace", "remove"):
                raise ScimError(400, f"unsupported op {kind}", "invalidSyntax")
            items = val if (not path and isinstance(val, dict)) else {path: val}
            for k, v in items.items():
                k = k.lower()
                if k == "active":
                    want = False if kind == "remove" else _truthy(v)
                    if want != bool(self._user(uid)["active"]):
                        await self._set_active(uid, want, by)
                elif k == "username":
                    self._set_attrs(uid, {"user_name": str(v)})
                elif k == "displayname" or k == "name.formatted":
                    self._set_attrs(uid, {"display_name": str(v or "")})
                elif k == "externalid":
                    self._set_attrs(uid, {"external_id": str(v or "")})
                # other attributes (title, phone…) are accepted and ignored
        del u
        return self.get_user(uid)

    async def delete_user(self, uid: str, by: str) -> None:
        u = self._user(uid)
        await revoke(u["email"], by, "deprovisioned by the identity provider (SCIM delete)")
        con = _con()
        with con:
            con.execute("DELETE FROM scim_users WHERE id=?", (uid,))
        con.close()
        import audit
        audit.log(by, "scim.user.delete", u["email"], "")

    # -- groups --
    def _group(self, gid: str) -> dict:
        con = _con()
        r = con.execute("SELECT * FROM scim_groups WHERE id=?", (gid,)).fetchone()
        con.close()
        if not r:
            raise ScimError(404, f"Group {gid} not found")
        return dict(r)

    def _members(self, gid: str) -> list[dict]:
        con = _con()
        rows = [dict(r) for r in con.execute(
            "SELECT u.* FROM scim_group_members m JOIN scim_users u ON u.id = m.user_id "
            "WHERE m.group_id=? ORDER BY u.email", (gid,))]
        con.close()
        return rows

    def list_groups(self, flt: str | None, start: int = 1, count: int | None = None,
                    excluded: str = "") -> dict:
        f = _filter(flt)
        con = _con()
        rows = [dict(r) for r in con.execute("SELECT * FROM scim_groups ORDER BY created_at")]
        con.close()
        if f:
            attr, val = f
            col = {"displayname": "display_name", "externalid": "external_id", "id": "id"}.get(attr)
            if col is None:
                raise ScimError(400, f"unsupported filter attribute: {attr}", "invalidFilter")
            rows = [r for r in rows if str(r[col]).lower() == val.lower()]
        skip_members = "members" in (excluded or "")
        return _list([_group_out(g, [] if skip_members else self._members(g["id"]), self.base)
                      for g in rows], start, count)

    def get_group(self, gid: str) -> dict:
        g = self._group(gid)
        return _group_out(g, self._members(gid), self.base)

    async def _sync_team(self, g: dict, before: set[str], by: str) -> None:
        """Mirror the group's members into the SOKKAN team; reconcile who left."""
        import projects
        tid = group_team_key(g)
        after = {m["email"] for m in self._members(g["id"])}
        now = time.time()
        con = projects._con()
        try:
            with con:
                con.execute("INSERT INTO teams(id, name, source, external_id, synced_at) "
                            "VALUES(?,?,'sso',?,?) ON CONFLICT(id) DO UPDATE SET "
                            "synced_at=excluded.synced_at", (tid, g["display_name"],
                                                             g["external_id"], now))
                for e in before - after:
                    con.execute("DELETE FROM team_members WHERE team_id=? AND email=?", (tid, e))
                for e in after:
                    con.execute("INSERT OR REPLACE INTO team_members(team_id, email, synced_at) "
                                "VALUES(?,?,?)", (tid, e, now))
        finally:
            con.close()
        import audit
        for e in sorted(before - after):
            audit.log(by, "scim.group.remove", tid, e)
            await reconcile(e, by)
        for e in sorted(after - before):
            audit.log(by, "scim.group.add", tid, e)

    def _set_members(self, gid: str, user_ids: list[str], mode: str) -> None:
        con = _con()
        with con:
            if mode == "replace":
                con.execute("DELETE FROM scim_group_members WHERE group_id=?", (gid,))
            for uid in user_ids:
                if mode == "remove":
                    con.execute("DELETE FROM scim_group_members WHERE group_id=? AND user_id=?",
                                (gid, uid))
                else:
                    if con.execute("SELECT 1 FROM scim_users WHERE id=?", (uid,)).fetchone():
                        con.execute("INSERT OR IGNORE INTO scim_group_members(group_id, user_id)"
                                    " VALUES(?,?)", (gid, uid))
            con.execute("UPDATE scim_groups SET updated_at=? WHERE id=?", (time.time(), gid))
        con.close()

    async def create_group(self, body: dict, by: str) -> dict:
        name = str(body.get("displayName") or "").strip()
        if not name:
            raise ScimError(400, "displayName is required", "invalidValue")
        gid, now = uuid.uuid4().hex, time.time()
        con = _con()
        try:
            with con:
                con.execute("INSERT INTO scim_groups(id, display_name, external_id, created_at,"
                            " updated_at) VALUES(?,?,?,?,?)",
                            (gid, name, str(body.get("externalId") or ""), now, now))
        except sqlite3.IntegrityError:
            raise ScimError(409, f"Group {name} already exists", "uniqueness")
        finally:
            con.close()
        self._set_members(gid, [str(m.get("value")) for m in body.get("members") or []
                                if isinstance(m, dict)], "add")
        await self._sync_team(self._group(gid), set(), by)
        return self.get_group(gid)

    async def replace_group(self, gid: str, body: dict, by: str) -> dict:
        g = self._group(gid)
        before = {m["email"] for m in self._members(gid)}
        if body.get("displayName") and body["displayName"] != g["display_name"]:
            raise ScimError(400, "renaming a group is not supported (it names a team that "
                                 "grants project roles)", "mutability")
        self._set_members(gid, [str(m.get("value")) for m in body.get("members") or []
                                if isinstance(m, dict)], "replace")
        await self._sync_team(self._group(gid), before, by)
        return self.get_group(gid)

    async def patch_group(self, gid: str, body: dict, by: str) -> dict:
        g = self._group(gid)
        before = {m["email"] for m in self._members(gid)}
        for op in body.get("Operations") or []:
            kind = str(op.get("op") or "").lower()
            path = str(op.get("path") or "")
            val = op.get("value")
            m = re.match(r'^members\[value eq "([^"]+)"\]$', path, re.I)
            if m and kind == "remove":
                self._set_members(gid, [m.group(1)], "remove")
            elif path.lower() == "members":
                ids = [str(x.get("value")) for x in (val or []) if isinstance(x, dict)]
                if kind == "remove" and not ids:
                    self._set_members(gid, [], "replace")
                else:
                    self._set_members(gid, ids, {"add": "add", "remove": "remove",
                                                 "replace": "replace"}.get(kind, "add"))
            elif (not path and isinstance(val, dict)) or path.lower() == "displayname":
                new = val.get("displayName") if isinstance(val, dict) else val
                if new and new != g["display_name"]:
                    raise ScimError(400, "renaming a group is not supported", "mutability")
                if isinstance(val, dict) and val.get("members") is not None:
                    self._set_members(gid, [str(x.get("value")) for x in val["members"]
                                            if isinstance(x, dict)],
                                      "replace" if kind == "replace" else "add")
            elif path.lower() == "externalid":
                pass
            else:
                raise ScimError(400, f"unsupported patch path: {path}", "invalidPath")
        await self._sync_team(self._group(gid), before, by)
        return self.get_group(gid)

    async def delete_group(self, gid: str, by: str) -> None:
        g = self._group(gid)
        before = {m["email"] for m in self._members(gid)}
        self._set_members(gid, [], "replace")
        await self._sync_team(g, before, by)
        con = _con()
        with con:
            con.execute("DELETE FROM scim_groups WHERE id=?", (gid,))
        con.close()


def service_provider_config() -> dict:
    return {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True}, "bulk": {"supported": False, "maxOperations": 0,
                                                   "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 500},
            "changePassword": {"supported": False}, "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Bearer token",
                                       "description": "SOKKAN_SCIM_TOKEN", "primary": True}]}


def resource_types(base: str) -> dict:
    rts = [{"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": n,
            "name": n, "endpoint": f"/{n}s", "schema": s,
            "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/{n}"}}
           for n, s in (("User", SCIM_USER), ("Group", SCIM_GROUP))]
    return _list(rts)


# ---- HTTP (FastAPI router) ------------------------------------------------------------

class RevokeIn(BaseModel):
    reason: str = ""


def router(require):
    """SCIM endpoint (own bearer auth, listed in app._AUTH_FREE) + admin routes."""
    r = APIRouter()
    prefix = "/api/scim/v2"

    def _err(status: int, detail: str, scim_type: str = "") -> JSONResponse:
        body = {"schemas": [SCIM_ERROR], "status": str(status), "detail": detail}
        if scim_type:
            body["scimType"] = scim_type
        return JSONResponse(body, status_code=status, media_type="application/scim+json")

    def _ok(body: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(body, status_code=status, media_type="application/scim+json")

    def _base(request: Request) -> str:
        pub = (os.environ.get("SOKKAN_PUBLIC_URL") or "").rstrip("/")
        return (pub or str(request.base_url).rstrip("/")) + prefix

    def _auth(request: Request) -> JSONResponse | None:
        tok = scim_token()
        if not enabled() or not tok:
            return _err(404, "SCIM is not enabled on this instance")
        got = request.headers.get("authorization") or ""
        if not got.lower().startswith("bearer ") or not hmac.compare_digest(
                got[7:].strip().encode(), tok.encode()):
            return _err(401, "invalid SCIM bearer token")
        return None

    async def _body(request: Request) -> dict:
        try:
            b = json.loads(await request.body() or b"{}")
        except ValueError:
            raise ScimError(400, "invalid JSON", "invalidSyntax")
        if not isinstance(b, dict):
            raise ScimError(400, "a JSON object is expected", "invalidSyntax")
        return b

    async def _handle(request: Request, fn):
        bad = _auth(request)
        if bad is not None:
            return bad
        try:
            return await fn(Scim(_base(request)))
        except ScimError as e:
            return _err(e.status, e.detail, e.scim_type)

    def _paging(request: Request) -> tuple[str | None, int, int | None]:
        q = request.query_params
        try:
            start = int(q.get("startIndex") or 1)
            count = int(q["count"]) if q.get("count") else None
        except ValueError:
            raise ScimError(400, "startIndex/count must be integers", "invalidValue")
        return q.get("filter"), start, count

    BY = "scim"

    @r.get(prefix + "/ServiceProviderConfig")
    async def scim_spc(request: Request):
        async def go(_s):
            return _ok(service_provider_config())
        return await _handle(request, go)

    @r.get(prefix + "/ResourceTypes")
    async def scim_rt(request: Request):
        async def go(s):
            return _ok(resource_types(s.base))
        return await _handle(request, go)

    @r.get(prefix + "/Schemas")
    async def scim_schemas(request: Request):
        async def go(_s):
            return _ok(_list([{"id": SCIM_USER, "name": "User"},
                              {"id": SCIM_GROUP, "name": "Group"}]))
        return await _handle(request, go)

    @r.get(prefix + "/Users")
    async def scim_users(request: Request):
        async def go(s):
            f, start, count = _paging(request)
            return _ok(s.list_users(f, start, count))
        return await _handle(request, go)

    @r.post(prefix + "/Users")
    async def scim_user_create(request: Request):
        async def go(s):
            return _ok(await s.create_user(await _body(request), BY), 201)
        return await _handle(request, go)

    @r.get(prefix + "/Users/{uid}")
    async def scim_user_get(uid: str, request: Request):
        async def go(s):
            return _ok(s.get_user(uid))
        return await _handle(request, go)

    @r.put(prefix + "/Users/{uid}")
    async def scim_user_put(uid: str, request: Request):
        async def go(s):
            return _ok(await s.replace_user(uid, await _body(request), BY))
        return await _handle(request, go)

    @r.patch(prefix + "/Users/{uid}")
    async def scim_user_patch(uid: str, request: Request):
        async def go(s):
            return _ok(await s.patch_user(uid, await _body(request), BY))
        return await _handle(request, go)

    @r.delete(prefix + "/Users/{uid}")
    async def scim_user_delete(uid: str, request: Request):
        async def go(s):
            await s.delete_user(uid, BY)
            return Response(status_code=204)
        return await _handle(request, go)

    @r.get(prefix + "/Groups")
    async def scim_groups(request: Request):
        async def go(s):
            f, start, count = _paging(request)
            return _ok(s.list_groups(f, start, count,
                                     request.query_params.get("excludedAttributes") or ""))
        return await _handle(request, go)

    @r.post(prefix + "/Groups")
    async def scim_group_create(request: Request):
        async def go(s):
            return _ok(await s.create_group(await _body(request), BY), 201)
        return await _handle(request, go)

    @r.get(prefix + "/Groups/{gid}")
    async def scim_group_get(gid: str, request: Request):
        async def go(s):
            return _ok(s.get_group(gid))
        return await _handle(request, go)

    @r.put(prefix + "/Groups/{gid}")
    async def scim_group_put(gid: str, request: Request):
        async def go(s):
            return _ok(await s.replace_group(gid, await _body(request), BY))
        return await _handle(request, go)

    @r.patch(prefix + "/Groups/{gid}")
    async def scim_group_patch(gid: str, request: Request):
        async def go(s):
            return _ok(await s.patch_group(gid, await _body(request), BY))
        return await _handle(request, go)

    @r.delete(prefix + "/Groups/{gid}")
    async def scim_group_delete(gid: str, request: Request):
        async def go(s):
            await s.delete_group(gid, BY)
            return Response(status_code=204)
        return await _handle(request, go)

    # -- admin --
    def _need_feature() -> None:
        if not enabled():
            raise HTTPException(404, "feature `revocation` is off on this instance")

    @r.get("/api/admin/revocation")
    def admin_revocation(_u: dict = Depends(require("admin"))) -> dict:
        _need_feature()
        return {"disabled": disabled_accounts(),
                "scim": {"enabled": bool(scim_token()), "url": "/api/scim/v2"}}

    @r.post("/api/admin/users/{email}/revoke")
    async def admin_revoke_now(email: str, body: RevokeIn | None = None,
                               u: dict = Depends(require("admin"))) -> dict:
        _need_feature()
        import iam
        target = _norm(email)
        if target == _norm(u["email"]):
            raise HTTPException(400, "you cannot revoke your own access")
        if iam.get_user(target).get("role") == "owner":
            raise HTTPException(403, "the instance owner cannot be revoked from the cockpit")
        return await revoke(target, u["email"], (body.reason if body else "") or
                            "« Revoke now » by an administrator")

    @r.post("/api/admin/users/{email}/reinstate")
    def admin_reinstate(email: str, u: dict = Depends(require("admin"))) -> dict:
        _need_feature()
        reinstate(email, u["email"])
        return {"email": _norm(email), "disabled": False}

    return r
