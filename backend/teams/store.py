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
    # 3.4.1: the channel's name and team as Teams sends them (resolves the mapping's name)
    conv = {r[1] for r in c.execute("PRAGMA table_info(conversations)")}
    if "channel_name" not in conv:
        c.execute("ALTER TABLE conversations ADD COLUMN channel_name TEXT NOT NULL DEFAULT ''")
    if "team_id" not in conv:
        c.execute("ALTER TABLE conversations ADD COLUMN team_id TEXT NOT NULL DEFAULT ''")
    # 3.4.3: the person's display name as Teams / the IdP give it (the @mention's text)
    ul = {r[1] for r in c.execute("PRAGMA table_info(user_links)")}
    if "display_name" not in ul:
        c.execute("ALTER TABLE user_links ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")


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


def _channel_out(r) -> dict:
    d = dict(r)
    d["approvals"] = bool(d.get("approvals", 1))     # 3.4.1: a boolean, as the API documents
    return d


def channel(channel_id: str) -> dict | None:
    c = con()
    r = c.execute("SELECT * FROM channel_map WHERE channel_id=?", (channel_id,)).fetchone()
    c.close()
    return _channel_out(r) if r else None


def channels() -> list[dict]:
    c = con()
    rows = [_channel_out(r) for r in c.execute("SELECT * FROM channel_map ORDER BY project, name")]
    c.close()
    return rows


def remember_names(channel_id: str, name: str = "", team_id: str = "") -> None:
    """3.4.1: what a verified activity said about its channel (``channelData.channel.name``,
    ``channelData.team.aadGroupId``) — Teams sends the name on some events only, so a known
    value is never overwritten by an empty one."""
    c = con()
    with c:
        c.execute("UPDATE conversations SET channel_name=CASE WHEN ?='' THEN channel_name ELSE ? END,"
                  " team_id=CASE WHEN ?='' THEN team_id ELSE ? END WHERE channel_id=?",
                  (name, name, team_id, team_id, channel_id))
    c.close()


def remembered(channel_id: str) -> dict:
    """{channel_name, team_id} seen for this channel ('' when never seen)."""
    c = con()
    r = c.execute("SELECT channel_name, team_id FROM conversations WHERE channel_id=?",
                  (channel_id,)).fetchone()
    c.close()
    return {"channel_name": r["channel_name"], "team_id": r["team_id"]} if r \
        else {"channel_name": "", "team_id": ""}


# ---- user links -------------------------------------------------------------------------
def link_user(aad_object_id: str, tenant: str, email: str, display_name: str = "") -> None:
    """Entra object id ↔ SOKKAN account. A display name given here is kept; an empty one
    never erases the one already remembered (3.4.3)."""
    c = con()
    with c:
        c.execute("INSERT INTO user_links(aad_object_id, tenant_id, email, linked_at, display_name)"
                  " VALUES(?,?,?,?,?) ON CONFLICT(aad_object_id, tenant_id) DO UPDATE SET"
                  " email=excluded.email, linked_at=excluded.linked_at,"
                  " display_name=CASE WHEN excluded.display_name='' THEN user_links.display_name"
                  " ELSE excluded.display_name END",
                  (aad_object_id, tenant, email.lower().strip(), time.time(),
                   (display_name or "").strip()))
    c.close()


def remember_user_name(aad_object_id: str, tenant: str, display_name: str) -> None:
    """3.4.3 — the name Teams sends with an activity (`from.name`), kept on the link."""
    display_name = (display_name or "").strip()
    if not display_name or not aad_object_id:
        return
    c = con()
    with c:
        c.execute("UPDATE user_links SET display_name=? WHERE aad_object_id=? AND tenant_id=?",
                  (display_name, aad_object_id, tenant))
    c.close()


def display_name_of(email: str, tenant: str) -> str:
    """The display name remembered for this account in the tenant ('' when none)."""
    c = con()
    r = c.execute("SELECT display_name FROM user_links WHERE email=? AND tenant_id=?"
                  " AND display_name<>'' ORDER BY linked_at DESC LIMIT 1",
                  (email.lower().strip(), tenant)).fetchone()
    c.close()
    return r["display_name"] if r else ""


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
