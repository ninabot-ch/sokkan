"""connectors.store — connectors.db: the instance's settings, each person's connections
(credentials ENCRYPTED with the vault's data key) and the actions waiting for a card.

Nothing a connector reads is stored here: no mail, no event, no document — only who is
connected to what, and the actions a person was asked to approve (their payload encrypted,
since a mail to send is content)."""
from __future__ import annotations

import json
import os
import secrets as _secrets
import sqlite3
import threading
import time

from . import ConnectorError, data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,                 -- JSON
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS accounts (
    email TEXT NOT NULL,                 -- the SOKKAN person
    provider TEXT NOT NULL,              -- graph | imap | caldav | webdav | demo
    login TEXT NOT NULL DEFAULT '',      -- the account at the provider (address, UPN)
    config TEXT NOT NULL DEFAULT '{}',   -- non-secret options (JSON)
    secret_enc TEXT NOT NULL DEFAULT '', -- password / refresh token (Fernet, vault context)
    access_enc TEXT NOT NULL DEFAULT '', -- cached access token (Fernet)
    access_exp REAL NOT NULL DEFAULT 0,
    scopes TEXT NOT NULL DEFAULT '',
    consented_at REAL NOT NULL,
    last_used_at REAL,
    PRIMARY KEY (email, provider)
);
CREATE TABLE IF NOT EXISTS oauth_pending (
    state TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    verifier TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                  -- mail.send | calendar.create | files.write
    provider TEXT NOT NULL,
    owner TEXT NOT NULL,                 -- the person whose credentials will act
    session_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,                 -- one line for the card
    preview TEXT NOT NULL DEFAULT '',    -- what the person reads before deciding (JSON)
    payload_enc TEXT NOT NULL,           -- what will run (Fernet)
    status TEXT NOT NULL DEFAULT 'pending', -- pending | done | rejected | failed | expired
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS actions_owner ON actions(owner, status);
"""
_lock = threading.Lock()
_ready: str | None = None
ACTION_TTL_S = 24 * 3600


def db_path() -> str:
    return os.environ.get("SOKKAN_CONNECTORS_DB") or os.path.join(data_dir(), "connectors.db")


def con() -> sqlite3.Connection:
    global _ready
    p = db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    if _ready != p:
        with _lock:
            c.executescript(_SCHEMA)
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
            _ready = p
    return c


# ---- encryption (the vault's data key: rotated with it, see reencrypt) ------------------
def _fernet():
    import secrets_provider
    return secrets_provider.active().fernet("vault")


def enc(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def dec(value: str) -> str:
    if not value:
        return ""
    return _fernet().decrypt(value.encode()).decode()


def reencrypt() -> int:
    """Data-key rotation of the `vault` context: every secret re-encrypted with the primary."""
    f = _fernet()
    n = 0
    c = con()
    with c:
        for r in c.execute("SELECT email, provider, secret_enc, access_enc FROM accounts").fetchall():
            vals = [f.rotate(v.encode()).decode() if v else "" for v in (r["secret_enc"], r["access_enc"])]
            c.execute("UPDATE accounts SET secret_enc=?, access_enc=? WHERE email=? AND provider=?",
                      (*vals, r["email"], r["provider"]))
            n += 1
        for r in c.execute("SELECT id, payload_enc FROM actions").fetchall():
            c.execute("UPDATE actions SET payload_enc=? WHERE id=?",
                      (f.rotate(r["payload_enc"].encode()).decode(), r["id"]))
            n += 1
        row = c.execute("SELECT value FROM settings WHERE key='secrets'").fetchone()
        if row:
            d = json.loads(row["value"])
            d = {k: f.rotate(v.encode()).decode() for k, v in d.items() if v}
            c.execute("UPDATE settings SET value=? WHERE key='secrets'", (json.dumps(d),))
    c.close()
    return n


# ---- settings ---------------------------------------------------------------------------
def get_setting(key: str, default=None):
    c = con()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return json.loads(r["value"]) if r else default


def put_setting(key: str, value, by: str = "") -> None:
    c = con()
    with c:
        c.execute("INSERT OR REPLACE INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?)",
                  (key, json.dumps(value), time.time(), by))
    c.close()


def put_instance_secret(name: str, value: str, by: str = "") -> None:
    """An instance credential (the Graph application's client secret): encrypted, never
    served back."""
    d = get_setting("secrets", {}) or {}
    if value:
        d[name] = enc(value)
    else:
        d.pop(name, None)
    put_setting("secrets", d, by)


def instance_secret(name: str) -> str:
    v = (get_setting("secrets", {}) or {}).get(name) or ""
    return dec(v) if v else ""


def has_instance_secret(name: str) -> bool:
    return bool((get_setting("secrets", {}) or {}).get(name))


# ---- accounts ---------------------------------------------------------------------------
def save_account(email: str, provider: str, *, login: str = "", config: dict | None = None,
                 secret: str = "", scopes: str = "") -> None:
    c = con()
    with c:
        c.execute("INSERT OR REPLACE INTO accounts(email, provider, login, config, secret_enc,"
                  " access_enc, access_exp, scopes, consented_at) VALUES(?,?,?,?,?,'',0,?,?)",
                  (email.lower(), provider, login, json.dumps(config or {}), enc(secret), scopes,
                   time.time()))
    c.close()


def account(email: str, provider: str) -> dict | None:
    c = con()
    r = c.execute("SELECT * FROM accounts WHERE email=? AND provider=?",
                  (email.lower(), provider)).fetchone()
    c.close()
    if not r:
        return None
    d = dict(r)
    d["config"] = json.loads(d["config"] or "{}")
    return d


def accounts_of(email: str) -> list[dict]:
    c = con()
    rows = c.execute("SELECT email, provider, login, config, scopes, consented_at, last_used_at"
                     " FROM accounts WHERE email=? ORDER BY provider", (email.lower(),)).fetchall()
    c.close()
    return [{**dict(r), "config": json.loads(r["config"] or "{}")} for r in rows]


def secret_of(acc: dict) -> str:
    return dec(acc.get("secret_enc") or "")


def update_tokens(email: str, provider: str, *, access: str, expires_at: float,
                  refresh: str | None = None) -> None:
    c = con()
    with c:
        if refresh:
            c.execute("UPDATE accounts SET access_enc=?, access_exp=?, secret_enc=? WHERE email=?"
                      " AND provider=?", (enc(access), expires_at, enc(refresh), email.lower(), provider))
        else:
            c.execute("UPDATE accounts SET access_enc=?, access_exp=? WHERE email=? AND provider=?",
                      (enc(access), expires_at, email.lower(), provider))
    c.close()


def touch(email: str, provider: str) -> None:
    c = con()
    with c:
        c.execute("UPDATE accounts SET last_used_at=? WHERE email=? AND provider=?",
                  (time.time(), email.lower(), provider))
    c.close()


def delete_account(email: str, provider: str) -> bool:
    c = con()
    with c:
        n = c.execute("DELETE FROM accounts WHERE email=? AND provider=?",
                      (email.lower(), provider)).rowcount
    c.close()
    return bool(n)


def delete_provider(provider: str) -> int:
    """An administrator turns a provider off: every person's connection to it is dropped."""
    c = con()
    with c:
        n = c.execute("DELETE FROM accounts WHERE provider=?", (provider,)).rowcount
    c.close()
    return n


# ---- OAuth (state + PKCE verifier, 10 minutes, single use) ---------------------------------
def oauth_begin(email: str, verifier: str) -> str:
    state = _secrets.token_urlsafe(24)
    c = con()
    with c:
        c.execute("DELETE FROM oauth_pending WHERE created_at < ?", (time.time() - 600,))
        c.execute("INSERT INTO oauth_pending(state, email, verifier, created_at) VALUES(?,?,?,?)",
                  (state, email.lower(), verifier, time.time()))
    c.close()
    return state


def oauth_take(state: str) -> dict | None:
    c = con()
    with c:
        r = c.execute("SELECT * FROM oauth_pending WHERE state=? AND created_at >= ?",
                      (state, time.time() - 600)).fetchone()
        c.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
    c.close()
    return dict(r) if r else None


# ---- actions (the approval cards) ---------------------------------------------------------
def add_action(*, kind: str, provider: str, owner: str, session_id: str, project: str,
               title: str, preview: dict, payload: dict) -> dict:
    aid = _secrets.token_hex(12)
    now = time.time()
    c = con()
    with c:
        c.execute("INSERT INTO actions(id, kind, provider, owner, session_id, project, title, preview,"
                  " payload_enc, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, kind, provider, owner.lower(), session_id, project, title[:300],
                   json.dumps(preview), enc(json.dumps(payload)), now, now + ACTION_TTL_S))
    c.close()
    return get_action(aid)


def _public(r) -> dict:
    d = dict(r)
    d.pop("payload_enc", None)
    d["preview"] = json.loads(d.get("preview") or "{}")
    if d["status"] == "pending" and d["expires_at"] < time.time():
        d["status"] = "expired"
    return d


def get_action(aid: str) -> dict | None:
    c = con()
    r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
    c.close()
    return _public(r) if r else None


def action_payload(aid: str) -> dict:
    c = con()
    r = c.execute("SELECT payload_enc FROM actions WHERE id=?", (aid,)).fetchone()
    c.close()
    if not r:
        raise ConnectorError("action not found", 404, "not_found")
    return json.loads(dec(r["payload_enc"]))


def actions_of(owner: str, status: str = "", limit: int = 100) -> list[dict]:
    c = con()
    q, args = "SELECT * FROM actions WHERE owner=?", [owner.lower()]
    if status:
        q += " AND status=?"
        args.append(status)
    rows = c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*args, int(limit))).fetchall()
    c.close()
    out = [_public(r) for r in rows]
    return [a for a in out if not status or a["status"] == status]


def claim_action(aid: str, owner: str, decision: str, by: str) -> dict | None:
    """Move ONE pending, unexpired action of ``owner`` to ``decision`` (atomically: a card is
    decided once — a double click, two tabs, a replayed request get None)."""
    c = con()
    with c:
        n = c.execute("UPDATE actions SET status=?, decided_at=?, decided_by=? WHERE id=? AND owner=?"
                      " AND status='pending' AND expires_at >= ?",
                      (decision, time.time(), by.lower(), aid, owner.lower(), time.time())).rowcount
    c.close()
    return get_action(aid) if n else None


def finish_action(aid: str, status: str, result: str) -> None:
    c = con()
    with c:
        c.execute("UPDATE actions SET status=?, result=? WHERE id=?", (status, result[:2000], aid))
    c.close()


def pending_count(owner: str) -> int:
    c = con()
    n = c.execute("SELECT COUNT(*) FROM actions WHERE owner=? AND status='pending' AND expires_at >= ?",
                  (owner.lower(), time.time())).fetchone()[0]
    c.close()
    return int(n)
