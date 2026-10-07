#!/usr/bin/env python3
"""projects.py — SOKKAN 3.2 multi-user: projects, teams, grants (lot 1: data model).

A **project** is a perimeter: repositories + resources (database, cluster…) + its CortHeXis
memory + its Crew agents + its vault secrets + its budgets + its board. Who may do what in a
project comes from ONE access source, chosen per project (docs/MULTIUSER.md):

* ``instance``  — the instance role (iam.py) applies to the project. The ``default`` project
  created by the migration uses it, so a 3.1 install behaves exactly as before;
* ``sso_group`` — grants to users or to teams (= IdP groups synced at login);
* ``forge``     — the person's access level on the project's repositories, read with their
  own forge account (GitLab first). Lot 3: until then a forge project only grants what a
  fresh ``access_cache`` row says, i.e. nothing.

Lot 1 builds the tables, the idempotent "default project" migration and the read side
(``effective_role``, ``readable_projects``, ``recall_scope``) used by the memory recall
filter. Nothing here is exposed in the UI yet; no second project can be created by the API.

Storage: ``$SOKKAN_DATA_DIR/projects.db`` (SQLite, WAL: the API and the MCP servers read it).
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

DB = Path(os.environ.get("SOKKAN_PROJECTS_DB", os.path.join(
    os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
    "projects.db")))
SCHEMA_VERSION = 1
DEFAULT_PROJECT = "default"           # = core.contract.DEFAULT_PROJECT (memory side)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# project roles, increasing. GitLab: Guest/Reporter → viewer, Developer → dev,
# Maintainer → maintainer, Owner → admin (see FORGE_LEVELS).
PROJECT_ROLES = ["viewer", "dev", "maintainer", "admin"]
ACCESS_SOURCES = ("instance", "sso_group", "forge")
# instance role → project role, for a project whose access source is `instance`
INSTANCE_TO_PROJECT = {"viewer": "viewer", "dev": "dev", "admin": "admin", "owner": "admin"}
# GitLab access levels (API `access_level`) → project role (used from lot 3)
FORGE_LEVELS = {10: "viewer", 20: "viewer", 30: "dev", 40: "maintainer", 50: "admin"}
FORGE_PROVIDERS = ("gitlab", "github", "gitea", "bitbucket", "azure-devops")
RESOURCE_KINDS = ("database", "cluster", "host", "bucket", "service", "other")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    access_source TEXT NOT NULL DEFAULT 'sso_group'
        CHECK (access_source IN ('instance', 'sso_group', 'forge')),
    budget_day_usd REAL NOT NULL DEFAULT 0,
    budget_month_usd REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    archived_at REAL
);
-- teams = IdP groups (source 'sso', synced at each login from the groups claim) or local
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,                 -- 'sso:<group>' | 'local:<slug>'
    name TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('sso', 'local')),
    external_id TEXT NOT NULL DEFAULT '',
    synced_at REAL
);
CREATE TABLE IF NOT EXISTS team_members (
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    synced_at REAL NOT NULL,
    PRIMARY KEY (team_id, email)
);
CREATE INDEX IF NOT EXISTS ix_team_members_email ON team_members(email);
-- who has which role in a project (access source `sso_group`; also overrides for `forge`)
CREATE TABLE IF NOT EXISTS project_grants (
    project TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    principal_kind TEXT NOT NULL CHECK (principal_kind IN ('user', 'team')),
    principal TEXT NOT NULL,             -- email | team id
    role TEXT NOT NULL CHECK (role IN ('viewer', 'dev', 'maintainer', 'admin')),
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project, principal_kind, principal)
);
CREATE TABLE IF NOT EXISTS project_repos (
    project TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,              -- https://gitlab.example.org
    repo_path TEXT NOT NULL,             -- group/subgroup/repo
    external_id TEXT NOT NULL DEFAULT '',
    default_branch TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project, provider, base_url, repo_path)
);
-- non-secret description of a resource; its credentials are vault NAMES of the project
CREATE TABLE IF NOT EXISTS project_resources (
    project TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',   -- JSON, never a secret value
    secret_names TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (project, kind, name)
);
-- a person's forge account (lot 3): tokens Fernet-encrypted with a key of their own
CREATE TABLE IF NOT EXISTS forge_links (
    email TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    forge_user_id TEXT NOT NULL DEFAULT '',
    forge_username TEXT NOT NULL DEFAULT '',
    token_enc TEXT NOT NULL DEFAULT '',
    refresh_enc TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '',
    expires_at REAL,
    linked_at REAL NOT NULL,
    last_refresh_at REAL,
    revoked_at REAL,
    PRIMARY KEY (email, provider, base_url)
);
-- resolved role per (person, project): forge reads are cached, never trusted past expires_at
CREATE TABLE IF NOT EXISTS access_cache (
    email TEXT NOT NULL,
    project TEXT NOT NULL,
    role TEXT,                           -- NULL = no access (cached negative)
    source TEXT NOT NULL,
    computed_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (email, project)
);
"""

_init_lock = threading.Lock()
_initialized_for: str | None = None


def valid_slug(slug: str | None) -> bool:
    return bool(slug) and bool(SLUG_RE.match(str(slug)))


def prank(role: str | None) -> int:
    return PROJECT_ROLES.index(role) if role in PROJECT_ROLES else -1


def _default_name() -> str:
    try:
        import instance
        return (instance.info().get("org_name") or "").strip() or "Default project"
    except Exception:  # noqa: BLE001 — the name is cosmetic
        return "Default project"


def init(force: bool = False) -> None:
    """DDL + the 3.2 migration, once per process and DB path. Idempotent: the ``default``
    project is created if missing (access source ``instance``: the instance roles keep
    applying). Nothing else is touched — sessions, cards, agents and memory notes get their
    ``project`` column (default 'default') from their own modules."""
    global _initialized_for
    with _init_lock:
        if _initialized_for == str(DB) and not force:
            return
        DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA)
        if not con.execute("SELECT 1 FROM projects WHERE slug=?", (DEFAULT_PROJECT,)).fetchone():
            con.execute(
                "INSERT INTO projects(slug, name, description, access_source, created_at,"
                " created_by) VALUES(?,?,?,?,?,?)",
                (DEFAULT_PROJECT, _default_name(),
                 "Everything this instance held before 3.2 (migration).", "instance",
                 time.time(), "migration-3.2"))
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),))
        con.commit()
        con.close()
        _initialized_for = str(DB)


def _con() -> sqlite3.Connection:
    init()
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


# ---- projects -------------------------------------------------------------------------

def get(slug: str) -> dict | None:
    con = _con()
    r = con.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
    con.close()
    return dict(r) if r else None


def list_projects(include_archived: bool = False) -> list[dict]:
    con = _con()
    sql = "SELECT * FROM projects" + ("" if include_archived else " WHERE archived_at IS NULL")
    rows = [dict(r) for r in con.execute(sql + " ORDER BY slug = 'default' DESC, slug")]
    con.close()
    return rows


def create(slug: str, name: str, *, access_source: str = "sso_group", created_by: str = "",
           description: str = "") -> dict:
    """Lot 1: used by tests and the CLI only (no API route — see docs/MULTIUSER.md)."""
    if not valid_slug(slug):
        raise ValueError("project slug: lowercase letters, digits and '-', 1-63 chars")
    if access_source not in ACCESS_SOURCES:
        raise ValueError(f"access_source must be one of {', '.join(ACCESS_SOURCES)}")
    con = _con()
    try:
        con.execute(
            "INSERT INTO projects(slug, name, description, access_source, created_at, created_by)"
            " VALUES(?,?,?,?,?,?)",
            (slug, (name or slug).strip()[:120], description, access_source, time.time(),
             created_by))
        con.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"project already exists: {slug}")
    finally:
        con.close()
    return get(slug)  # type: ignore[return-value]


# ---- teams and grants -----------------------------------------------------------------

def sync_sso_groups(email: str, groups: list[str]) -> None:
    """Replace the person's SSO team memberships by the IdP's `groups` claim (called at
    login, lot 2). A group that disappears from the claim = membership removed at the next
    login; the revocation of a leaver is covered by the session TTL + SCIM/deprovisioning
    (docs/MULTIUSER.md, "Revocation")."""
    email = (email or "").lower().strip()
    now = time.time()
    con = _con()
    with con:
        con.execute("DELETE FROM team_members WHERE email=? AND team_id LIKE 'sso:%'", (email,))
        for g in sorted({str(g).strip() for g in groups if str(g).strip()}):
            tid = f"sso:{g}"
            con.execute("INSERT INTO teams(id, name, source, external_id, synced_at) "
                        "VALUES(?,?,'sso',?,?) ON CONFLICT(id) DO UPDATE SET synced_at="
                        "excluded.synced_at", (tid, g, g, now))
            con.execute("INSERT OR REPLACE INTO team_members(team_id, email, synced_at) "
                        "VALUES(?,?,?)", (tid, email, now))
    con.close()


def grant(project: str, principal_kind: str, principal: str, role: str,
          created_by: str = "") -> None:
    if role not in PROJECT_ROLES:
        raise ValueError(f"role must be one of {', '.join(PROJECT_ROLES)}")
    if principal_kind not in ("user", "team"):
        raise ValueError("principal_kind: user | team")
    if principal_kind == "user":
        principal = principal.lower().strip()
    con = _con()
    with con:
        con.execute("INSERT OR REPLACE INTO project_grants(project, principal_kind, principal,"
                    " role, created_at, created_by) VALUES(?,?,?,?,?,?)",
                    (project, principal_kind, principal, role, time.time(), created_by))
    con.close()


def revoke(project: str, principal_kind: str, principal: str) -> None:
    con = _con()
    with con:
        con.execute("DELETE FROM project_grants WHERE project=? AND principal_kind=? AND "
                    "principal=?", (project, principal_kind, principal))
    con.close()


# ---- resolution -----------------------------------------------------------------------

def _best(roles) -> str | None:
    roles = [r for r in roles if r in PROJECT_ROLES]
    return max(roles, key=prank) if roles else None


def effective_role(user: dict, project: str, now: float | None = None) -> str | None:
    """The person's role in ``project`` (None = no access). Fail-closed: unknown or
    archived project, unknown person under ``DEFAULT_ROLE=none``, forge without a fresh
    cache row → None."""
    p = get(project)
    if p is None or p.get("archived_at"):
        return None
    email = (user.get("email") or "").lower().strip()
    if p["access_source"] == "instance":
        return INSTANCE_TO_PROJECT.get(user.get("role") or "")
    con = _con()
    try:
        rows = con.execute(
            "SELECT role FROM project_grants WHERE project=? AND ("
            " (principal_kind='user' AND principal=?) OR"
            " (principal_kind='team' AND principal IN"
            "   (SELECT team_id FROM team_members WHERE email=?)))",
            (project, email, email)).fetchall()
        roles = [r["role"] for r in rows]
        if p["access_source"] == "forge":
            now = time.time() if now is None else now
            c = con.execute("SELECT role FROM access_cache WHERE email=? AND project=? AND "
                            "expires_at > ?", (email, project, now)).fetchone()
            if c and c["role"]:
                roles.append(c["role"])
    finally:
        con.close()
    return _best(roles)


def can(user: dict, project: str, min_role: str) -> bool:
    return prank(effective_role(user, project)) >= prank(min_role)


def readable_projects(user: dict) -> list[str]:
    """Slugs of the projects the person can at least read."""
    return [p["slug"] for p in list_projects() if effective_role(user, p["slug"]) is not None]


def recall_scope(project: str | None) -> tuple[str, ...]:
    """Memory scope of a session or a run bound to ``project``: that project only. A missing
    project means a session created before 3.2 → the default project. An unknown or
    invalid project gives an EMPTY scope (no recall), never a wider one."""
    slug = (project or DEFAULT_PROJECT).strip()
    if not valid_slug(slug) or get(slug) is None:
        return ()
    return (slug,)


def multi_project() -> bool:
    """More than the default project exists (lot 1: never through the API)."""
    return len(list_projects()) > 1


def session_scope(session_project: str | None) -> tuple[str, ...]:
    """Recall scope of a session from its stored project. A session SOKKAN does not know
    (None) keeps the pre-3.2 behaviour — the default project — only while the instance has
    a single project; with several projects it gets no recall at all (fail-closed)."""
    if session_project is None:
        return (DEFAULT_PROJECT,) if not multi_project() else ()
    return recall_scope(session_project)
