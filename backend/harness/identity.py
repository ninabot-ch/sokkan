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

Two more places carried the identity to the engine (closed in 4.0, lot 0c):

* the run's **working folder** — a project's sessions work in
  ``<data>/projects/<slug>/work``: the slug is in every path the engine sees. An opaque
  engine is handed ``<data>/engine-roots/<prj-handle>/work`` instead, through a link
  ``<data>/engine-roots/<prj-handle>`` → ``../projects/<slug>`` (`opaque_cwd`): the same
  files, a path without the name. An engine that resolves links on the host (``realpath``)
  still finds the real folder: the alias keeps the name out of what it is handed, logs and
  shows, not out of what it can compute;
* **``RunSpec.env``** — the tools' environment held the person's address and the session
  id (inference headers, the forge ticket), and the project's slug (« Write with Claude »
  routing). `opaque_env` rewrites the values that carry them into handles, and `leaks`
  finds any value that still carries one, in clear or base64-encoded: `opaque_spec` DROPS
  such a variable (and says so on stderr) — a variable nobody rewrote never reaches the
  engine with a name in it.

Standard library only: the memory server imports this module without the backend.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
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


def instance_label(purpose: str = "kernel") -> str:
    """A stable, opaque name of THIS instance for ``purpose`` (4.0: the ``local.instance`` of
    the kernel engine, the label of its sandboxes on the docker daemon). Derived from the
    instance key, never from a path or a host name: the same across upgrades, restarts and a
    move of the install folder; different for two instances sharing one docker daemon (each
    has its own key). ``sokkan-`` + 16 hex."""
    digest = hmac.new(_key(), f"instance:{purpose}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"sokkan-{digest}"


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


ROOTS = "engine-roots"


def opaque_cwd(cwd: str) -> str:
    """The working folder an opaque engine is handed: a folder of a project's space
    (``<data>/projects/<slug>/…``) through the link ``<data>/engine-roots/<prj-handle>``;
    any other folder (the instance's workspace) unchanged."""
    if not cwd:
        return cwd
    projects = (_data_dir() / "projects").resolve()
    try:
        rel = Path(cwd).resolve().relative_to(projects)
    except (ValueError, OSError):
        return cwd
    if not rel.parts:
        return cwd
    slug, rest = rel.parts[0], rel.parts[1:]
    h = handle("project", slug)
    roots = _data_dir() / ROOTS
    roots.mkdir(parents=True, exist_ok=True)
    link = roots / h
    target = Path("..") / "projects" / slug
    if link.is_symlink():
        if os.readlink(link) != str(target):
            link.unlink()
    elif link.exists():
        raise RuntimeError(f"{link} exists and is not the alias of a project's space")
    if not link.is_symlink():
        os.symlink(target, link, target_is_directory=True)
    return str(link.joinpath(*rest))


def _encoded(word: str) -> set[str]:
    raw = word.encode()
    out = {word}
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        e = enc(raw).decode()
        out |= {e, e.rstrip("=")}
    return {x for x in out if x}


# the project every instance has: its name says nothing about anyone
PUBLIC_PROJECTS = ("default",)


def leaks(env: dict, words, slugs=()) -> list[str]:
    """The names of the variables of ``env`` whose value still carries an identifier:
    one of ``words`` (a session id, an address) anywhere, in clear or base64; one of
    ``slugs`` (a project's slug — a short slug is a common word) as a whole value, the value
    of a header line or a path component."""
    pats = []
    for w in words:
        if not w:
            continue
        for e in _encoded(w):
            pats.append(re.compile(r"(?<![A-Za-z0-9_.@-])" + re.escape(e)
                                   + r"(?![A-Za-z0-9_@-])"))
    for slug in slugs:
        if not slug or slug in PUBLIC_PROJECTS:
            continue
        q = re.escape(slug)
        pats.append(re.compile(rf"^\s*{q}\s*$|^\s*[A-Za-z0-9-]+\s*:\s*{q}\s*$|/{q}(?=/|$)",
                               re.M))
    return sorted(k for k, v in (env or {}).items() if any(p.search(str(v)) for p in pats))


_HEADER_LINE = re.compile(r"^(\s*[A-Za-z0-9-]+\s*:\s*)(.*?)(\s*)$")


def opaque_env(env: dict, names: dict[str, str]) -> dict:
    """``env`` with every identifier of ``names`` (value → handle) replaced by its handle:
    anywhere for an address or a session id (distinctive), and for a project's slug where
    it is a whole value or the value of a header line (``x-sokkan-project: radio``) — a
    short slug is a common word, it is not replaced inside other text."""
    out = {}
    for k, v in (env or {}).items():
        v = str(v)
        for word, h in names.items():
            if not word or not h:
                continue
            if "@" in word or len(word) >= 16:
                v = v.replace(word, h)
            elif v == word:
                v = h
            else:
                lines = []
                for line in v.split("\n"):
                    m = _HEADER_LINE.match(line)
                    if m and m.group(2) == word:
                        line = m.group(1) + h + m.group(3)
                    lines.append(line)
                v = "\n".join(lines)
        out[k] = v
    return out


def opaque_spec(spec, *, rewrite=None):
    """A copy of a `RunSpec` an opaque engine may be handed: run, person and project
    replaced by handles, the MCP servers' identity variables by one run handle, the working
    folder by its alias (`opaque_cwd`), the tools' environment rewritten (`opaque_env`) —
    ``rewrite(env, handles)`` lets the cockpit re-mint what it alone can (the forge
    ticket) — and any variable that still names someone dropped (`leaks`)."""
    run = handle("run", spec.run_id)
    user = handle("user", spec.user)
    project = handle("project", spec.project)
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
    names = {spec.run_id: run, spec.user: user}
    if spec.project not in PUBLIC_PROJECTS:
        names[spec.project] = project
    env = opaque_env(spec.env, names)
    if rewrite is not None:
        env = rewrite(env, {"run": run, "user": user, "project": project})
    for k in leaks(env, [w for w in (spec.run_id, spec.user) if w], [spec.project]):
        print(f"[sokkan] {k} not handed to the engine: it names the run's identity",
              file=sys.stderr)
        env.pop(k, None)
    return dataclasses.replace(spec, run_id=run, user=user, project=project,
                               cwd=opaque_cwd(spec.cwd), env=env,
                               secret_names=tuple(n for n in spec.secret_names if n in env),
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
