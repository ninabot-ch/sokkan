"""Opaque identity handles for an engine that must not see who and what it works for.

An engine that declares ``Capabilities.identity == "opaque"`` treats whatever identifies a
run as a secret: it logs it, redacts it in every output, or sends it off the host. Handed a
project name or an e-mail address, it would redact that word everywhere it appears — in a
file, a commit message, the answer. So the cockpit hands it HANDLES instead:

    run-3f9c1a…    the SOKKAN session (``RunSpec.run_id``)
    usr-81d0be…    the person (``RunSpec.user``)
    prj-c27e44…    the project (``RunSpec.project``, ``Confinement.project``)

A handle is ``HMAC-SHA256(instance key, kind:value)`` cut to 20 hex: stable for one
instance (the same project always has the same handle, a resumed run finds its own), and
meaningless elsewhere. The instance key (``engine-identity.key``, 0600) and the mapping
(``engine_handles.db``) live in the data directory; only the cockpit resolves a handle.

The SOKKAN MCP servers of a run need the real identity to attribute and scope their work.
For an opaque engine their environment carries ONE handle, ``SOKKAN_SESSION_HANDLE``, and
the server calls ``expand_env()`` when it starts: the variables it has always read
(``SOKKAN_SESSION_ID``, ``_USER``, ``_PROJECT``, ``_SCOPE``, ``SOKKAN_AGENT_*``) are put
back from the mapping. That needs the data directory, as today's servers do; a server
started where the data directory is not mounted resolves through the api instead (lot 1).

Standard library only: the memory server imports this module without the backend.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

KINDS = {"run": "run", "user": "usr", "project": "prj"}
HANDLE_ENV = "SOKKAN_SESSION_HANDLE"
# the identity variables of a SOKKAN MCP server (agentchat.mcp_servers_for)
IDENTITY_ENV = ("SOKKAN_SESSION_ID", "SOKKAN_SESSION_USER", "SOKKAN_SESSION_PROJECT",
                "SOKKAN_SESSION_SCOPE", "SOKKAN_AGENT_RUN", "SOKKAN_AGENT_NAME",
                "SOKKAN_AGENT_RUN_ID")


def _data_dir() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))


def _key() -> bytes:
    path = _data_dir() / "engine-identity.key"
    try:
        k = path.read_bytes()
        if len(k) >= 32:
            return k
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    k = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, k)
    except Exception:
        os.close(fd)
        raise
    os.close(fd)
    return k


def _con() -> sqlite3.Connection:
    path = _data_dir() / "engine_handles.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=2)
    con.execute("CREATE TABLE IF NOT EXISTS handles (handle TEXT PRIMARY KEY, kind TEXT NOT NULL,"
                " value TEXT NOT NULL, env TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL)")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return con


def handle(kind: str, value: str, *, env: dict | None = None) -> str:
    """The handle of one identifier (`kind` ∈ run | user | project). Empty stays empty.
    `env`: for a run, the identity environment of its MCP servers (see expand_env)."""
    if not value:
        return ""
    prefix = KINDS[kind]
    digest = hmac.new(_key(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:20]
    h = f"{prefix}-{digest}"
    con = _con()
    try:
        con.execute("INSERT INTO handles(handle, kind, value, env, ts) VALUES(?,?,?,?,?)"
                    " ON CONFLICT(handle) DO UPDATE SET env = CASE WHEN excluded.env != '{}'"
                    " THEN excluded.env ELSE handles.env END",
                    (h, kind, value, json.dumps(env or {}, sort_keys=True), time.time()))
        con.commit()
    finally:
        con.close()
    return h


def resolve(h: str) -> tuple[str, str] | None:
    """(kind, value) of a handle minted on this instance, else None."""
    if not h:
        return None
    con = _con()
    try:
        r = con.execute("SELECT kind, value FROM handles WHERE handle = ?", (h,)).fetchone()
    finally:
        con.close()
    return (r[0], r[1]) if r else None


def _env_of(h: str) -> dict:
    con = _con()
    try:
        r = con.execute("SELECT env FROM handles WHERE handle = ? AND kind = 'run'",
                        (h,)).fetchone()
    finally:
        con.close()
    return json.loads(r[0]) if r else {}


def opaque_spec(spec):
    """A copy of a `RunSpec` an opaque engine may be handed: run, person and project
    replaced by handles, the MCP servers' identity variables by one run handle."""
    run = handle("run", spec.run_id)
    servers = {}
    for name, cfg in (spec.mcp_servers or {}).items():
        env = dict(cfg.get("env") or {})
        who = {k: env.pop(k) for k in IDENTITY_ENV if k in env}
        if who:
            # the run's handle carries the identity of ITS servers (one session = one set)
            handle("run", spec.run_id, env=who)
            env[HANDLE_ENV] = run
        servers[name] = {**cfg, "env": env}
    conf = spec.confinement
    if conf is not None and conf.project:
        conf = dataclasses.replace(conf, project=handle("project", conf.project))
    return dataclasses.replace(spec, run_id=run, user=handle("user", spec.user),
                               project=handle("project", spec.project),
                               mcp_servers=servers, confinement=conf)


def expand_env(environ=None) -> bool:
    """Called by a SOKKAN MCP server at start: when its environment carries a run handle
    and no identity, the identity is put back from the mapping. True when it did."""
    environ = os.environ if environ is None else environ
    h = (environ.get(HANDLE_ENV) or "").strip()
    if not h or environ.get("SOKKAN_SESSION_ID"):
        return False
    who = _env_of(h)
    if not who:
        return False
    for k, v in who.items():
        environ.setdefault(k, v)
    return True
