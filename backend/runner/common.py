"""Shared pieces of the container runners (docker, kubernetes)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from .base import Resources, SessionSpec
from .mounts import SESSION_HOME

SUPERVISOR_PORT = 7070
LABEL_SESSION = "sokkan.ch/session"
LABEL_PROJECT = "sokkan.ch/project"
LABEL_INSTANCE = "sokkan.ch/instance"
LABEL_KIND = "sokkan.ch/kind"
LABEL_COMPONENT = "app.kubernetes.io/component"


def _key() -> bytes:
    """Per-instance runner key (tokens are derived from it, so that an api that restarts
    can reconnect to the session containers it started). SOKKAN_RUNNER_SECRET (a Secret in
    the Helm chart) or a 0600 file in the data dir."""
    env = os.environ.get("SOKKAN_RUNNER_SECRET")
    if env:
        return env.encode()
    d = os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
    path = os.path.join(d, "runner.key")
    try:
        with open(path, "rb") as f:
            k = f.read().strip()
            if k:
                return k
    except OSError:
        pass
    k = secrets.token_hex(32).encode()
    os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


def token_for(sid: str, purpose: str) -> str:
    return hmac.new(_key(), f"{purpose}:{sid}".encode(), hashlib.sha256).hexdigest()


def instance_label() -> str:
    return (os.environ.get("SOKKAN_RUNNER_INSTANCE") or "sokkan")[:63]


def container_env(spec: SessionSpec, relay_addr: str, proxy: str = "") -> dict[str, str]:
    """Environment of the session container: the session env + the supervisor's."""
    env = dict(spec.env)
    env.update({
        "HOME": SESSION_HOME,
        "CLAUDE_CONFIG_DIR": f"{SESSION_HOME}/.claude",
        "SOKKAN_SESSION_ARGV": json.dumps(spec.argv),
        "SOKKAN_SESSION_CWD": spec.cwd,
        "SOKKAN_SESSION_TOKEN": spec.token,
        "SOKKAN_RELAY_TOKEN": spec.relay_token,
        "SOKKAN_RELAY_ADDR": relay_addr,
        "SOKKAN_SESSION_IDLE_S": os.environ.get("SOKKAN_SESSION_IDLE_S") or "900",
        "SOKKAN_SUPERVISOR_PORT": str(SUPERVISOR_PORT),
        # a session container never tries to update its CLI (read-only image)
        "DISABLE_AUTOUPDATER": "1",
    })
    if proxy:
        relay_host = relay_addr.rpartition(":")[0]
        env.update({"HTTPS_PROXY": proxy, "https_proxy": proxy,
                    "NO_PROXY": ",".join(x for x in (relay_host, "localhost", "127.0.0.1")
                                         if x),
                    "no_proxy": relay_host})
    return env


def cpu_to_nano(v: str) -> int:
    v = v.strip()
    return int(float(v[:-1]) * 1e6) if v.endswith("m") else int(float(v) * 1e9)


_UNITS = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
          "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}


def mem_to_bytes(v: str) -> int:
    v = v.strip()
    for u in sorted(_UNITS, key=len, reverse=True):
        if v.endswith(u):
            return int(float(v[: -len(u)]) * _UNITS[u])
    return int(float(v))


def describe(r: Resources) -> dict:
    return {"cpu_request": r.cpu_request, "cpu_limit": r.cpu_limit,
            "memory_request": r.memory_request, "memory_limit": r.memory_limit}
