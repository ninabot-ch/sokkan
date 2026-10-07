#!/usr/bin/env python3
"""audit.py — SOKKAN : journal global des actions (qui a fait quoi, quand).

Toute mutation passée par l'API (spawn/kill de session, envoi de prompt,
cartes du board, users IAM, preview start/stop/trigger) est journalisée en
sqlite. Ce n'est PAS du logging de contenu (les conversations restent dans
les transcripts) — c'est la piste d'audit des ACTIONS, pour comprendre et
revenir en arrière si besoin. Consommé par l'onglet Journal.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.environ.get("SOKKAN_AUDIT_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "audit.db")))


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, user TEXT DEFAULT '',
            action TEXT NOT NULL, resource TEXT DEFAULT '', detail TEXT DEFAULT ''
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)")
    if "project" not in {r[1] for r in con.execute("PRAGMA table_info(events)")}:
        # 3.2 lot 4: the project an action was about ('' = instance-level / before 3.2)
        con.execute("ALTER TABLE events ADD COLUMN project TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS ix_events_project ON events(project, ts)")
    return con


def _request_project() -> str:
    """Project of the cockpit request being served (projectgate), '' outside one."""
    try:
        import projectgate
        return (projectgate.current() or {}).get("project") or ""
    except Exception:  # noqa: BLE001 — a process without the gate (MCP server, script)
        return ""


def log(user: str, action: str, resource: str = "", detail: str = "",
        project: str | None = None) -> None:
    """Best-effort : l'audit ne doit jamais faire échouer l'action elle-même.
    `project` (3.2 lot 4) : défaut = le projet de la requête en cours (projectgate) ;
    un serveur MCP passe celui de sa session (SOKKAN_SESSION_PROJECT)."""
    if project is None:
        project = _request_project() or (os.environ.get("SOKKAN_SESSION_PROJECT") or "").strip()
    try:
        con = _con()
        con.execute(
            "INSERT INTO events(ts, user, action, resource, detail, project) VALUES(?,?,?,?,?,?)",
            (time.time(), user or "", action, resource, (detail or "")[:2000], project or ""),
        )
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


def recent(limit: int = 200, q: str = "", project: str | None = None) -> list[dict]:
    """`project` (3.2 lot 4): only the events of that project (the journal a project admin
    sees); None = every event (instance admins)."""
    con = _con()
    where, args = [], []
    if project is not None:
        where.append("project = ?")
        args.append(project)
    if q:
        like = f"%{q}%"
        where.append("(user LIKE ? OR action LIKE ? OR resource LIKE ? OR detail LIKE ?)")
        args += [like] * 4
    rows = con.execute(
        "SELECT ts, user, action, resource, detail, project FROM events"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY ts DESC LIMIT ?", (*args, min(limit, 1000)))
    out = [dict(r) for r in rows]
    con.close()
    return out
