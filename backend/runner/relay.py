"""MCP relay (api side): session containers reach the SOKKAN MCP servers through it.

A session container runs `sokkan-mcp-relay <server>` (runner/pod/mcp_relay.py) where the
CLI would run a local MCP server. The relay connects here, presents the session's relay
token, and the api starts the real stdio server with the configuration and IDENTITY the
api registered for that session (agentchat.mcp_servers_for: user, project, agent run) —
the container can only reach its own session's servers, as itself. Bytes are piped both ways
until either side closes.

The same channel answers the git credential helper of a session container
(``{"token", "op": "git-credential", "request": {ticket, action, protocol, host, path}}``,
one JSON line back): the relay token says WHICH session is asking (fixed by the api), the
HMAC ticket must name that same session and its live person (forge.gitcred.answer) — a
ticket replayed from another session, or without a relay token, gets nothing.

Listens on SOKKAN_RUNNER_RELAY_BIND (default 0.0.0.0:8098) — only for remote runners; the
NetworkPolicy (kubernetes) / internal network (docker) limit who can connect.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys

_registry: dict[str, dict] = {}       # relay token → {"sid", "servers": {name: cfg}}
_server: asyncio.base_events.Server | None = None


def register(token: str, sid: str, servers: dict) -> None:
    _registry[token] = {"sid": sid, "servers": dict(servers or {})}


def unregister(token: str) -> None:
    _registry.pop(token, None)


def lookup(token: str) -> dict | None:
    for t, entry in _registry.items():
        if hmac.compare_digest(t, token or ""):
            return entry
    return None


async def _pipe(r: asyncio.StreamReader, w, close) -> None:
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        hdr = json.loads(await asyncio.wait_for(reader.readline(), 15) or b"{}")
    except Exception:  # noqa: BLE001
        writer.close()
        return
    entry = lookup(str(hdr.get("token") or ""))
    if hdr.get("op") == "git-credential":
        await _git_credential(entry, hdr.get("request"), writer)
        return
    name = str(hdr.get("server") or "")
    cfg = (entry or {}).get("servers", {}).get(name)
    if not entry or not cfg or "command" not in cfg:
        print(f"[sokkan] mcp relay: refused server {name!r}", file=sys.stderr)
        writer.close()
        return
    env = {**os.environ, **(cfg.get("env") or {})}
    proc = await asyncio.create_subprocess_exec(
        cfg["command"], *(cfg.get("args") or []), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=None)
    assert proc.stdin and proc.stdout

    def close_stdin() -> None:
        proc.stdin.close()  # type: ignore[union-attr]

    await asyncio.gather(_pipe(reader, proc.stdin, close_stdin),
                         _pipe(proc.stdout, writer, writer.close))
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()


async def _git_credential(entry: dict | None, req, writer: asyncio.StreamWriter) -> None:
    if entry is None or not isinstance(req, dict):
        ans = {"reason": "unknown relay token"}
    else:
        try:
            import features
            from forge import gitcred
            if not features.enabled("gitlab"):
                ans = {"reason": "feature gitlab is off"}
            else:
                ans = await asyncio.to_thread(gitcred.answer, req, bound_sid=entry["sid"])
        except Exception as e:  # noqa: BLE001 — never a token in an error
            ans = {"reason": f"credential lookup failed ({type(e).__name__})"}
    try:
        writer.write((json.dumps(ans) + "\n").encode())
        await writer.drain()
    finally:
        writer.close()


async def ensure_started() -> tuple[str, int] | None:
    """Start the relay listener once, in the api's event loop."""
    global _server
    if _server is not None:
        return None
    bind = os.environ.get("SOKKAN_RUNNER_RELAY_BIND") or "0.0.0.0:8098"
    host, _, port = bind.rpartition(":")
    _server = await asyncio.start_server(handle, host or "0.0.0.0", int(port))
    sock = _server.sockets[0].getsockname()
    print(f"[sokkan] runner MCP relay listening on {sock[0]}:{sock[1]}", file=sys.stderr)
    return sock[0], sock[1]


async def stop() -> None:
    global _server
    if _server is not None:
        _server.close()
        _server = None
