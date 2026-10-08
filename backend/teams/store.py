"""teams.store — teams.db: channel ↔ project mapping, Teams user ↔ SOKKAN account links,
single-use approvals, and the encrypted cache of outbound tokens."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_map (
    channel_id TEXT PRIMARY KEY,         -- Teams channel id, or the conversation id of a chat
    project TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 2,    -- audience of the channel (3.4 classification)
    name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS user_links (
    aad_object_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    email TEXT NOT NULL,
    linked_at REAL NOT NULL,
    PRIMARY KEY (aad_object_id, tenant_id)
);
CREATE TABLE IF NOT EXISTS approvals (
    nonce TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                  -- agent.run | agent.activate
    ref TEXT NOT NULL,
    project TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT '',
    approver_aad TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    used_by TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL                  -- JSON
);
CREATE TABLE IF NOT EXISTS conversations (
    channel_id TEXT PRIMARY KEY,         -- the key of channel_map
    service_url TEXT NOT NULL,           -- where Microsoft told us to answer (verified)
    conversation_id TEXT NOT NULL,       -- a channel: its id (a new post = a new thread)
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive (
    key TEXT PRIMARY KEY,                -- <approval key>@<channel id>
    item TEXT NOT NULL,                  -- agent.activate:<id> | run.tool:<run>:<permission>
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    project TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    service_url TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    activity_id TEXT NOT NULL DEFAULT '',
    nonce TEXT NOT NULL DEFAULT '',      -- the signed approval carried by the card ('' = notice)
    title TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 2,
    state TEXT NOT NULL DEFAULT 'open',  -- open | closed
    posted_at REAL NOT NULL,
    closed_at REAL,
    closed_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_proactive_state ON proactive(state);
CREATE TABLE IF NOT EXISTS token_cache (
    name TEXT PRIMARY KEY,
    value_enc TEXT NOT NULL,             -- Fernet
    expires_at REAL NOT NULL
);
"""
_lock = threading.Lock()
_ready: str | None = None


def _dir() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))


def db_path() -> Path:
    return Path(os.environ.get("SOKKAN_TEAMS_DB") or (_dir() / "teams.db"))


def con() -> sqlite3.Connection:
    global _ready
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    if _ready != str(p):
        with _lock:
            c.executescript(_SCHEMA)
            _migrate(c)
            c.commit()
            _ready = str(p)
    return c


def _migrate(c: sqlite3.Connection) -> None:
    """3.4.0: columns added to the tables of the first Teams release (idempotent)."""
    have = {t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
            for t in ("channel_map", "approvals")}
    if "approvals" not in have["channel_map"]:          # proactive approvals posted here
        c.execute("ALTER TABLE channel_map ADD COLUMN approvals INTEGER NOT NULL DEFAULT 1")
    if "card" not in have["approvals"]:                 # what the card showed (refresh)
        c.execute("ALTER TABLE approvals ADD COLUMN card TEXT NOT NULL DEFAULT ''")


def set_meta(name: str, value) -> None:
    import json
    c = con()
    with c:
        c.execute("INSERT OR REPLACE INTO meta(name, value) VALUES(?,?)", (name, json.dumps(value)))
    c.close()


def get_meta(name: str):
    import json
    c = con()
    r = c.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
    c.close()
    return json.loads(r["value"]) if r else None


# ---- key of the instance (token encryption, approval signatures) -------------------------
# 3.3: the `teams` data key comes from the secrets provider (teams.key in file mode, wrapped by
# OpenBao transit in openbao mode).
def _key_path() -> Path:
    import secrets_provider
    return Path(secrets_provider.key_path("teams"))


def key() -> bytes:
    """Primary data key of the Teams integration (file mode: generated once, 0600)."""
    import secrets_provider
    return secrets_provider.data_key("teams")


def keys() -> list[bytes]:
    """Every data key of a rotation in progress (newest first) — signatures still verify."""
    import secrets_provider
    return secrets_provider.all_data_keys("teams")


def _fernet():
    import secrets_provider
    return secrets_provider.active().fernet("teams")


def reencrypt() -> int:
    """Data-key rotation: cached app tokens re-encrypted with the primary key."""
    from cryptography.fernet import InvalidToken
    f = _fernet()
    n = 0
    c = con()
    with c:
        for r in c.execute("SELECT name, value_enc FROM token_cache").fetchall():
            try:
                c.execute("UPDATE token_cache SET value_enc=? WHERE name=?",
                          (f.rotate(r["value_enc"].encode()).decode(), r["name"]))
                n += 1
            except InvalidToken:
                c.execute("DELETE FROM token_cache WHERE name=?", (r["name"],))  # refetched
    c.close()
    return n


def put_token(name: str, value: str, expires_at: float) -> None:
    c = con()
    with c:
        c.execute("INSERT OR REPLACE INTO token_cache(name, value_enc, expires_at) VALUES(?,?,?)",
                  (name, _fernet().encrypt(value.encode()).decode(), expires_at))
    c.close()


def get_token(name: str, margin: float = 120.0) -> str | None:
    c = con()
    r = c.execute("SELECT value_enc, expires_at FROM token_cache WHERE name=?", (name,)).fetchone()
    c.close()
    if not r or r["expires_at"] - margin < time.time():
        return None
    try:
        return _fernet().decrypt(r["value_enc"].encode()).decode()
    except Exception:  # noqa: BLE001 — key rotated: fetch a new token
        return None


# ---- channels ---------------------------------------------------------------------------
def map_channel(channel_id: str, project: str, level: int = 2, name: str = "", by: str = "",
                approvals: bool = True) -> None:
    c = con()
    with c:
        c.execute("INSERT OR REPLACE INTO channel_map(channel_id, project, level, name, created_at,"
                  " created_by, approvals) VALUES(?,?,?,?,?,?,?)",
                  (channel_id, project, int(level), name, time.time(), by, int(bool(approvals))))
    c.close()


def unmap_channel(channel_id: str) -> None:
    c = con()
    with c:
        c.execute("DELETE FROM channel_map WHERE channel_id=?", (channel_id,))
    c.close()


def channel(channel_id: str) -> dict | None:
    c = con()
    r = c.execute("SELECT * FROM channel_map WHERE channel_id=?", (channel_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def channels() -> list[dict]:
    c = con()
    rows = [dict(r) for r in c.execute("SELECT * FROM channel_map ORDER BY project, name")]
    c.close()
    return rows


# ---- user links -------------------------------------------------------------------------
def link_user(aad_object_id: str, tenant: str, email: str) -> None:
    c = con()
    with c:
        c.execute("INSERT OR REPLACE INTO user_links(aad_object_id, tenant_id, email, linked_at)"
                  " VALUES(?,?,?,?)", (aad_object_id, tenant, email.lower().strip(), time.time()))
    c.close()


def linked_email(aad_object_id: str, tenant: str) -> str | None:
    c = con()
    r = c.execute("SELECT email FROM user_links WHERE aad_object_id=? AND tenant_id=?",
                  (aad_object_id, tenant)).fetchone()
    c.close()
    return r["email"] if r else None


def aad_of(email: str, tenant: str) -> str | None:
    c = con()
    r = c.execute("SELECT aad_object_id FROM user_links WHERE email=? AND tenant_id=?",
                  (email.lower().strip(), tenant)).fetchone()
    c.close()
    return r["aad_object_id"] if r else None
