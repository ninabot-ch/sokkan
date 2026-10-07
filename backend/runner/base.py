"""Runner interface: where the Claude Code CLI of a session (or an agent run) executes.

`SessionRunner` is the one contract the cockpit sees, whatever the packaging:

=============  ===========================================================================
verb           meaning
=============  ===========================================================================
start          create the session's process / container / pod — or ADOPT the live one of
               this session id (api restart: reattach instead of a second CLI)
open           connect to it: a `Channel` (stream-json, one JSON per line, both ways)
events         read the CLI's messages (assistant text, tools, control requests…)
send           write one message to the CLI (user turn, control response, interrupt…)
approve        answer a `can_use_tool` control request (allow / deny) — what the
               cockpit's Allow / Deny buttons send through the SDK
stop           end it and clean up (container / pod / per-session Secret removed)
status         pending | running | succeeded | failed | gone
usage          resources measured by the platform (CPU millicores, memory bytes)
list_live      the live sessions of this instance (startup reconciliation, GC)
=============  ===========================================================================

In the cockpit, a session is still driven by the Claude Agent SDK (`agentchat.AgentSession`):
permissions, questions, hooks (memory recall), budgets and event translation do not change.
For `local` the SDK spawns the CLI itself (behaviour byte-identical to 3.1). For `docker` and
`kubernetes`, `RunnerTransport` (an SDK `Transport`) carries the same stream-json protocol to
the session supervisor running in the container (backend/runner/pod/supervisor.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

STREAM_LIMIT = 64 * 1024 * 1024
# variables of the api's environment that a session legitimately needs (model access);
# everything else of the api (DB URLs, OIDC secrets, vault key…) never reaches a container
ENV_PASS_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")
ENV_PASS_EXACT = ("DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING", "NO_PROXY")
# never forwarded, even when the session env sets them (the container sets its own)
ENV_DROP = ("HOME", "PATH", "PWD", "CLAUDE_CONFIG_DIR", "HOSTNAME", "SHLVL", "_",
            "SOKKAN_SESSION_TOKEN", "SOKKAN_RELAY_TOKEN", "SOKKAN_RELAY_ADDR")
STATUSES = ("pending", "running", "succeeded", "failed", "gone")


@dataclass
class Resources:
    cpu_request: str = "250m"
    cpu_limit: str = "1"
    memory_request: str = "512Mi"
    memory_limit: str = "2Gi"

    @classmethod
    def from_env(cls) -> "Resources":
        e = os.environ.get
        return cls(e("SOKKAN_SESSION_CPU_REQUEST") or cls.cpu_request,
                   e("SOKKAN_SESSION_CPU_LIMIT") or cls.cpu_limit,
                   e("SOKKAN_SESSION_MEMORY_REQUEST") or cls.memory_request,
                   e("SOKKAN_SESSION_MEMORY_LIMIT") or cls.memory_limit)


@dataclass
class SessionSpec:
    """Everything a runner needs to start one session. Built by `runner.client_args`."""
    sid: str
    argv: list[str]                     # the CLI command line (stream-json, MCP via relay)
    cwd: str                            # the session's working directory, api-side path
    project: str = "default"
    user: str = ""
    env: dict[str, str] = field(default_factory=dict)   # session env (model, vault values)
    token: str = ""                     # supervisor token (api → container)
    relay_token: str = ""               # MCP relay token (container → api)
    kind: str = "session"               # session | run
    resources: Resources = field(default_factory=Resources.from_env)


@dataclass
class Handle:
    sid: str
    name: str                 # container / pod name (local: pid)
    host: str = ""            # address of the supervisor
    port: int = 7070
    token: str = ""
    adopted: bool = False     # True = found alive (reattach), not created now
    meta: dict = field(default_factory=dict)


def safe_name(sid: str, prefix: str = "sokkan-s-") -> str:
    """DNS-1123 name of a session's container / pod (≤ 63 chars)."""
    s = re.sub(r"[^a-z0-9-]", "-", sid.lower()).strip("-")[:40] or "x"
    return prefix + s


def session_env(env: dict | None, api_env: dict | None = None) -> dict[str, str]:
    """The environment a session container receives: what the session ADDS to the api's
    environment (vault values, model routing — agentchat merges them over os.environ), plus
    the model-access variables of the api. Nothing else of the api leaks into a container."""
    api_env = dict(os.environ if api_env is None else api_env)
    out: dict[str, str] = {}
    for k, v in api_env.items():
        if k.startswith(ENV_PASS_PREFIXES) or k in ENV_PASS_EXACT:
            out[k] = v
    for k, v in (env or {}).items():
        if v is None:
            continue
        if k not in api_env or api_env.get(k) != v or k.startswith(ENV_PASS_PREFIXES):
            out[k] = str(v)
    for k in ENV_DROP:
        out.pop(k, None)
    out = {k: v for k, v in out.items() if v != ""}
    out["CLAUDE_CODE_ENTRYPOINT"] = out.get("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
    return out


def claude_project_slug(cwd: str) -> str:
    """Directory name Claude Code gives a working directory under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


class Channel:
    """A stream-json connection to a session supervisor (or a local CLI process)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter | None,
                 hello: dict | None = None, proc: Any = None):
        self.reader = reader
        self.writer = writer
        self.hello = hello or {}
        self.proc = proc
        self.exited: int | None = None

    async def send(self, msg: dict | str) -> None:
        data = msg if isinstance(msg, str) else json.dumps(msg) + "\n"
        if not data.endswith("\n"):
            data += "\n"
        assert self.writer is not None
        self.writer.write(data.encode())
        await self.writer.drain()

    async def events(self) -> AsyncIterator[dict]:
        while True:
            line = await self.reader.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "sokkan_supervisor":
                if msg.get("state") == "exited":
                    self.exited = msg.get("code")
                continue
            yield msg

    async def end_input(self) -> None:
        if self.proc is not None:
            if self.writer is not None:
                self.writer.close()
            return
        await self.send({"type": "sokkan_supervisor", "op": "end_input"})

    async def close(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass


async def connect_supervisor(host: str, port: int, token: str, timeout: float = 60.0,
                             interval: float = 0.5) -> Channel:
    """Connect + authenticate to a session supervisor, retrying while it boots."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: Exception | None = None
    while loop.time() < deadline:
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=STREAM_LIMIT), 5)
            w.write((json.dumps({"token": token}) + "\n").encode())
            await w.drain()
            first = await asyncio.wait_for(r.readline(), 15)
            hello = json.loads(first) if first else {}
            if hello.get("type") != "sokkan_supervisor":
                w.close()
                raise ConnectionError("session supervisor refused the connection")
            return Channel(r, w, hello)
        except (OSError, asyncio.TimeoutError, ConnectionError, ValueError) as e:
            last = e
            await asyncio.sleep(interval)
    raise ConnectionError(f"session supervisor {host}:{port} unreachable: {last!r}")


class SessionRunner(ABC):
    name = "abstract"
    remote = True

    @abstractmethod
    async def start(self, spec: SessionSpec) -> Handle: ...

    @abstractmethod
    async def stop(self, handle: Handle) -> None: ...

    @abstractmethod
    async def status(self, handle: Handle) -> str: ...

    async def usage(self, handle: Handle) -> dict:
        return {}

    async def list_live(self) -> list[Handle]:
        return []

    async def open(self, handle: Handle, timeout: float = 120.0) -> Channel:
        return await connect_supervisor(handle.host, handle.port, handle.token, timeout)

    async def reconcile(self) -> dict:
        """Startup / periodic: remove finished session containers, keep the live ones
        (they are adopted by `start` when their session is opened again)."""
        return {"kept": [h.name for h in await self.list_live()], "removed": []}

    # ---- the session verbs on top of a channel (used by tests and by non-SDK callers) --
    @staticmethod
    async def send(chan: Channel, msg: dict | str) -> None:
        await chan.send(msg)

    @staticmethod
    def events(chan: Channel) -> AsyncIterator[dict]:
        return chan.events()

    @staticmethod
    async def approve(chan: Channel, request_id: str, allow: bool, message: str = "",
                      updated_input: dict | None = None) -> None:
        """Answer a `can_use_tool` control request of the CLI."""
        resp: dict[str, Any] = ({"behavior": "allow", "updatedInput": updated_input or {}}
                                if allow else {"behavior": "deny", "message": message})
        await chan.send({"type": "control_response",
                         "response": {"subtype": "success", "request_id": request_id,
                                      "response": resp}})
