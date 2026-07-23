"""SQLite storage — deliberately simple (see memory note `decision-sqlite-storage`)."""
from __future__ import annotations

import os
import sqlite3
import time

DB_PATH = os.environ.get("NOTES_DB", "notes.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL)""")
    return conn


def list_notes() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_note(note_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row) if row else None


def add_note(title: str, body: str) -> dict:
    with _conn() as conn:
        cur = conn.execute("INSERT INTO notes (title, body, created_at) VALUES (?, ?, ?)",
                           (title, body, time.time()))
        return get_note(cur.lastrowid) or {}
