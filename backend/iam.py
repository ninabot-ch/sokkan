#!/usr/bin/env python3
"""iam.py — SOKKAN : IAM interne (users + rôles).

Identité = l'utilisateur authentifié par **Authentik via CF Access** (header
`Cf-Access-Authenticated-User-Email` injecté au edge). SOKKAN ne gère QUE ses
propres rôles (il n'écrit jamais sur Cloudflare — ça reste le job de Claude Code).

Rôles (croissant) : viewer < dev < admin < owner.
- viewer : lecture seule (chat/preview/mémoire/infra)
- dev    : + spawn/envoi/board/preview-env (le travail)
- admin  : + gestion des users
- owner  : + ne peut être supprimé
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.environ.get("SOKKAN_IAM_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "iam.db")))
ROLES = ["viewer", "dev", "admin", "owner"]
# premier utilisateur = owner, défini par l'environnement (ou fallback local)
SEED = {
    os.environ.get("SOKKAN_OWNER_EMAIL", "owner@localhost"):
        ("owner", os.environ.get("SOKKAN_OWNER_NAME", "Owner")),
}


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, role TEXT, name TEXT, created_at REAL)"
    )
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO users(email, role, name, created_at) VALUES(?,?,?,?)",
            [(e, r, n, time.time()) for e, (r, n) in SEED.items()],
        )
        con.commit()
    return con


def get_user(email: str) -> dict:
    email = (email or "").lower().strip()
    con = _con()
    row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    con.close()
    if row:
        return {"email": row["email"], "role": row["role"], "name": row["name"], "known": True}
    # email autorisé au edge mais pas encore enregistré → viewer par défaut
    return {"email": email or "anonyme", "role": "viewer", "name": email or "anonyme", "known": False}


def list_users() -> list[dict]:
    con = _con()
    rows = [dict(r) for r in con.execute("SELECT * FROM users ORDER BY created_at")]
    con.close()
    return rows


def upsert_user(email: str, role: str, name: str = "") -> dict:
    email = email.lower().strip()
    if role not in ROLES:
        raise ValueError("rôle invalide")
    con = _con()
    con.execute(
        "INSERT INTO users(email, role, name, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(email) DO UPDATE SET role=excluded.role, name=excluded.name",
        (email, role, name or email, time.time()),
    )
    con.commit()
    con.close()
    return get_user(email)


def delete_user(email: str) -> None:
    email = email.lower().strip()
    con = _con()
    row = con.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
    if row and row["role"] == "owner":
        con.close()
        raise ValueError("impossible de supprimer un owner")
    con.execute("DELETE FROM users WHERE email=?", (email,))
    con.commit()
    con.close()
