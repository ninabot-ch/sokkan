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
import threading
import time
from pathlib import Path

DB = Path(os.environ.get("SOKKAN_IAM_DB", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "iam.db")))
ROLES = ["viewer", "dev", "admin", "owner"]
# rôle attribué à un email authentifié mais absent de la table users.
# "none" (3.4.1) = connecté, AUCUN rôle d'instance : la personne n'accède qu'aux projets où
# un grant ou une équipe SSO lui donne un rôle (plus un 403 — cf. auth.instance_user).
# Défaut par édition : enterprise → none (un IdP d'entreprise authentifie plus de monde que
# l'instance n'en veut ; un inconnu voyait les sessions du projet `default`), community → viewer.


def default_role() -> str:
    v = os.environ.get("SOKKAN_DEFAULT_ROLE", "").strip()
    if not v:
        import features
        v = "none" if features.edition() == "enterprise" else "viewer"
    if v not in (*ROLES, "none"):
        raise RuntimeError(f"SOKKAN_DEFAULT_ROLE invalid: {v!r} (viewer|dev|admin|owner|none)")
    return v


DEFAULT_ROLE = default_role()
# premier utilisateur = owner, défini par l'environnement (ou fallback local)
SEED = {
    os.environ.get("SOKKAN_OWNER_EMAIL", "owner@localhost"):
        ("owner", os.environ.get("SOKKAN_OWNER_NAME", "Owner")),
}


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


_init_lock = threading.Lock()
_initialized = False


def init(force: bool = False) -> None:
    """DDL + seed owner, exécutés une seule fois par process (force=True pour
    ré-initialiser après un changement de DB, p.ex. dans les tests)."""
    global _initialized
    with _init_lock:
        if _initialized and not force:
            return
        DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB)
        con.execute(
            "CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, role TEXT, name TEXT, created_at REAL)"
        )
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            con.executemany(
                "INSERT INTO users(email, role, name, created_at) VALUES(?,?,?,?)",
                [(e, r, n, time.time()) for e, (r, n) in SEED.items()],
            )
        con.commit()
        con.close()
        _initialized = True


def _con() -> sqlite3.Connection:
    init()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def get_user(email: str) -> dict:
    email = (email or "").lower().strip()
    con = _con()
    row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    con.close()
    if row:
        return {"email": row["email"], "role": row["role"], "name": row["name"], "known": True}
    # email authentifié mais pas encore enregistré → SOKKAN_DEFAULT_ROLE
    # ("none" est rejeté en 403 par auth.current_user)
    return {"email": email or "anonyme", "role": DEFAULT_ROLE,
            "name": email or "anonyme", "known": False}


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


def set_name(email: str, name: str) -> bool:
    """3.4.3 — remember a person's display name (from the OIDC login: `name`, else
    `preferred_username` when it is not an address). Only a KNOWN account, never the role;
    an empty or address-like name changes nothing. True when written."""
    email = (email or "").lower().strip()
    name = (name or "").strip()
    if not email or not name or "@" in name:
        return False
    con = _con()
    cur = con.execute("UPDATE users SET name=? WHERE email=?", (name, email))
    con.commit()
    con.close()
    return cur.rowcount > 0


def display_name(email: str) -> str:
    """The IAM name when it is one ('' when unset or just the address)."""
    n = (get_user(email).get("name") or "").strip()
    return "" if not n or n == (email or "").lower().strip() or "@" in n else n


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
