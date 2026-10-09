"""store.py — alerting.db (sqlite under SOKKAN_DATA_DIR, same convention as incidents.db).

Tables: sources, rules, alerts, transitions, silences, channels, terms (new_term memory),
keyvals (change memory), meta (leader lease). Secrets of sources and channels are encrypted
with the vault data key (secrets provider, Fernet) and never leave this module in clear
except to the adapter / channel that uses them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

_lock = threading.RLock()


def db_path() -> str:
    d = os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
    return os.environ.get("SOKKAN_ALERTING_DB") or os.path.join(d, "alerting.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL DEFAULT '*', name TEXT NOT NULL,
  kind TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', auth_kind TEXT NOT NULL DEFAULT 'none',
  auth_user TEXT NOT NULL DEFAULT '', secret_ct TEXT NOT NULL DEFAULT '',
  options TEXT NOT NULL DEFAULT '{}', builtin TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '{}', created_by TEXT NOT NULL DEFAULT '',
  created_at REAL, updated_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS sources_builtin ON sources(builtin) WHERE builtin != '';
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL DEFAULT 'default',
  body TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL DEFAULT '',
  created_at REAL, updated_at REAL, state TEXT NOT NULL DEFAULT '{}',
  next_eval REAL NOT NULL DEFAULT 0, external TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER NOT NULL, project TEXT NOT NULL,
  group_key TEXT NOT NULL DEFAULT '', grp TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'warning', started_at REAL, fired_at REAL, resolved_at REAL,
  last_notified_at REAL, value REAL, threshold TEXT NOT NULL DEFAULT 'null',
  summary TEXT NOT NULL DEFAULT '', acked_by TEXT, acked_at REAL, silenced_until REAL,
  incident_id INTEGER, sample_events TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS alerts_open ON alerts(rule_id, group_key, state);
CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER NOT NULL, project TEXT NOT NULL,
  ts REAL NOT NULL, group_key TEXT NOT NULL DEFAULT '', grp TEXT NOT NULL DEFAULT '{}',
  from_state TEXT NOT NULL, to_state TEXT NOT NULL, value REAL, threshold TEXT DEFAULT 'null',
  note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS transitions_rule ON transitions(rule_id, ts);
CREATE TABLE IF NOT EXISTS silences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, rule_id INTEGER,
  matchers TEXT NOT NULL DEFAULT '{}', starts_at REAL NOT NULL, ends_at REAL NOT NULL,
  reason TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL DEFAULT 'default', name TEXT NOT NULL,
  kind TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', secrets_ct TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, last_test TEXT NOT NULL DEFAULT 'null',
  created_by TEXT NOT NULL DEFAULT '', created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS terms (
  rule_id INTEGER NOT NULL, term TEXT NOT NULL, first_seen REAL NOT NULL,
  PRIMARY KEY (rule_id, term));
CREATE TABLE IF NOT EXISTS keyvals (
  rule_id INTEGER NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, ts REAL NOT NULL,
  PRIMARY KEY (rule_id, k));
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def con() -> sqlite3.Connection:
    p = db_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_SCHEMA)
    return c


def j(v, default):
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def now() -> float:
    return time.time()


# ---- secrets ---------------------------------------------------------------------------------
def _fernet():
    import secrets_provider
    return secrets_provider.active().fernet("vault")


def seal(d: dict) -> str:
    """{key: secret} → ciphertext ('' when empty)."""
    d = {k: v for k, v in (d or {}).items() if v}
    if not d:
        return ""
    return _fernet().encrypt(json.dumps(d).encode()).decode()


def unseal(ct: str) -> dict:
    if not ct:
        return {}
    try:
        return json.loads(_fernet().decrypt(ct.encode()).decode())
    except Exception:  # noqa: BLE001 — a key rotated away = secrets lost, never a crash
        return {}


def reencrypt() -> int:
    """Data-key rotation: every sealed secret re-encrypted with the primary key."""
    from cryptography.fernet import InvalidToken
    f = _fernet()
    n = 0
    with _lock:
        c = con()
        for table, col in (("sources", "secret_ct"), ("channels", "secrets_ct")):
            for r in c.execute(f"SELECT id, {col} AS ct FROM {table} WHERE {col} != ''").fetchall():
                try:
                    c.execute(f"UPDATE {table} SET {col}=? WHERE id=?",
                              (f.rotate(r["ct"].encode()).decode(), r["id"]))
                    n += 1
                except InvalidToken:
                    continue
        c.commit()
        c.close()
    return n


# ---- meta / leader lease ----------------------------------------------------------------------
def get_meta(name: str, default=None):
    c = con()
    r = c.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
    c.close()
    return j(r["value"], default) if r else default


def set_meta(name: str, value) -> None:
    with _lock:
        c = con()
        c.execute("INSERT INTO meta(name, value) VALUES(?,?) ON CONFLICT(name) DO UPDATE "
                  "SET value=excluded.value", (name, json.dumps(value)))
        c.commit()
        c.close()


def take_lease(holder: str, ttl: float) -> bool:
    """One evaluator per data dir: the lease is renewed by its holder, taken over when expired."""
    t = now()
    with _lock:
        c = con()
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT value FROM meta WHERE name='leader'").fetchone()
        cur = j(r["value"], {}) if r else {}
        ok = (not cur) or cur.get("holder") == holder or float(cur.get("until") or 0) < t
        if ok:
            c.execute("INSERT INTO meta(name, value) VALUES('leader', ?) ON CONFLICT(name) DO "
                      "UPDATE SET value=excluded.value",
                      (json.dumps({"holder": holder, "until": t + ttl}),))
        c.commit()
        c.close()
        return ok
