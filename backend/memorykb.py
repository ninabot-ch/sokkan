#!/usr/bin/env python3
"""memorykb.py — SOKKAN P4 : métadonnées de la base mémoire (onglet Mémoire-KB).

Lit le store RAG P0 (`memory.db`) pour lister les notes, leurs liens [[…]] et des
stats (« comment c'est construit »). La RECHERCHE sémantique réutilise directement
la logique du serveur MCP (`memory_search_server`) → une seule source de ranking.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

MEM_DB = Path(os.environ.get("SOKKAN_MEMORY_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "memory.db")))
LINK_RE = re.compile(r"\[\[([a-z0-9_\-]+)\]\]", re.I)


def _con() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True)


def list_notes() -> list[dict]:
    if not MEM_DB.exists():
        return []
    con = _con()
    rows = con.execute(
        "SELECT name, description, type, mtime, source_path FROM notes ORDER BY name"
    ).fetchall()
    counts = dict(con.execute("SELECT note_name, COUNT(*) FROM chunks GROUP BY note_name").fetchall())
    con.close()
    names = {r[0] for r in rows}
    incoming: dict[str, set] = {n: set() for n in names}
    out = []
    for name, desc, typ, mtime, path in rows:
        links: list[str] = []
        try:
            body = Path(path).read_text(encoding="utf-8", errors="replace")
            links = sorted({m for m in LINK_RE.findall(body) if m != name})
        except OSError:
            pass
        for tgt in links:
            if tgt in incoming:
                incoming[tgt].add(name)
        out.append({"name": name, "description": desc, "type": typ, "mtime": mtime,
                    "chunks": counts.get(name, 0), "links": links})
    for o in out:  # liens entrants (qui me cite)
        o["backlinks"] = sorted(incoming.get(o["name"], set()))
    return out


def stats() -> dict:
    if not MEM_DB.exists():
        return {"notes": 0, "chunks": 0, "model": None, "last_mtime": None}
    con = _con()
    n = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    c = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    m = con.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    last = con.execute("SELECT MAX(mtime) FROM notes").fetchone()[0]
    con.close()
    return {"notes": n, "chunks": c, "model": m[0] if m else None, "last_mtime": last}
