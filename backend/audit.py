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
    return con


def log(user: str, action: str, resource: str = "", detail: str = "") -> None:
    """Best-effort : l'audit ne doit jamais faire échouer l'action elle-même."""
    try:
        con = _con()
        con.execute(
            "INSERT INTO events(ts, user, action, resource, detail) VALUES(?,?,?,?,?)",
            (time.time(), user or "", action, resource, (detail or "")[:2000]),
        )
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


def recent(limit: int = 200, q: str = "") -> list[dict]:
    con = _con()
    if q:
        like = f"%{q}%"
        rows = con.execute(
            "SELECT ts, user, action, resource, detail FROM events"
            " WHERE user LIKE ? OR action LIKE ? OR resource LIKE ? OR detail LIKE ?"
            " ORDER BY ts DESC LIMIT ?",
            (like, like, like, like, min(limit, 1000)),
        )
    else:
        rows = con.execute(
            "SELECT ts, user, action, resource, detail FROM events ORDER BY ts DESC LIMIT ?",
            (min(limit, 1000),),
        )
    out = [dict(r) for r in rows]
    con.close()
    return out
