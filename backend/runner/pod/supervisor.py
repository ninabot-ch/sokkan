#!/usr/bin/env python3
"""sokkan-session-supervisor — PID 1 of a SOKKAN session container / pod (stdlib only).

The api does not run the Claude Code CLI itself when SOKKAN_RUNNER is `docker` or
`kubernetes`: it starts a session container whose entrypoint is this supervisor, then
connects to it over TCP. The supervisor:

* authenticates the api (first line = {"token": ...}, constant-time compare);
* spawns the CLI on the first connection (argv from SOKKAN_SESSION_ARGV, cwd from
  SOKKAN_SESSION_CWD) and pipes the stream-json protocol both ways, one JSON per line;
* survives the api: when the connection drops (api restart, rolling update), the CLI keeps
  running, its output is buffered (bounded) and replayed to the next connection. A new SDK
  client always starts with an `initialize` control request — the CLI was initialised
  already, so the supervisor answers it from the cached first answer instead of forwarding
  it. Hook callback ids are deterministic (hook_0, hook_1…), so the new client handles the
  callbacks of the running CLI. That is the "reattach after an api restart";
* exits when the CLI exits (container/pod completes), or after SOKKAN_SESSION_IDLE_S
  without any client (default 900 s): no session pod outlives its api forever.

Control lines of the supervisor itself have type "sokkan_supervisor" (never forwarded to
the CLI): {"type": "sokkan_supervisor", "op": "end_input"} closes the CLI's stdin; the
supervisor sends {"type": "sokkan_supervisor", "state": "attached"|"exited", ...}.
"""
from __future__ import annotations

import asyncio
import collections
import hmac
import json
import os
import signal
import sys
import time

LIMIT = 64 * 1024 * 1024  # one stream-json line can be large (file contents, images)


def _log(msg: str) -> None:
    print(f"[sokkan-supervisor] {msg}", file=sys.stderr, flush=True)


class Supervisor:
    def __init__(self, argv: list[str], cwd: str | None, token: str,
                 idle_s: float = 900.0, buffer_max: int = 5000,
                 env: dict | None = None):
        if not token:
            raise SystemExit("SOKKAN_SESSION_TOKEN is empty: refusing to serve")
        self.argv = argv
        self.cwd = cwd or None
        self.token = token
        self.idle_s = idle_s
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self.client: asyncio.StreamWriter | None = None
        self.buffer: collections.deque[bytes] = collections.deque(maxlen=buffer_max)
        self.dropped = 0
        self.init_response: dict | None = None
        self.init_request_id: str | None = None
        self.exit_code: int | None = None
        self.detached_at = time.monotonic()
        self.done = asyncio.Event()
        self._lock = asyncio.Lock()

    # ---- CLI process -------------------------------------------------------------------
    async def spawn(self) -> None:
        _log(f"spawning CLI in {self.cwd or os.getcwd()}")
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, limit=LIMIT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=None)  # stderr = the container's: `kubectl logs` / `docker logs`
        asyncio.get_running_loop().create_task(self._pump_stdout())

    async def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            self._capture_init(line)
            await self._deliver(line)
        self.exit_code = await self.proc.wait()
        _log(f"CLI exited with {self.exit_code}")
        await self._deliver(self._ctl(state="exited", code=self.exit_code))
        if self.client is not None:
            try:
                await self.client.drain()
                self.client.close()
            except Exception:  # noqa: BLE001
                pass
        self.done.set()

    def _capture_init(self, line: bytes) -> None:
        if self.init_response is not None or self.init_request_id is None:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        resp = msg.get("response") if isinstance(msg, dict) else None
        if (msg.get("type") == "control_response" and isinstance(resp, dict)
                and resp.get("request_id") == self.init_request_id
                and resp.get("subtype") == "success"):
            self.init_response = resp.get("response") or {}

    async def _deliver(self, line: bytes) -> None:
        async with self._lock:
            w = self.client
            if w is not None:
                try:
                    w.write(line)
                    await w.drain()
                    return
                except Exception:  # noqa: BLE001 — the api went away: buffer from here
                    self._detach(w)
            if len(self.buffer) == self.buffer.maxlen:
                self.dropped += 1
            self.buffer.append(line)

    @staticmethod
    def _ctl(**kw) -> bytes:
        return (json.dumps({"type": "sokkan_supervisor", **kw}) + "\n").encode()

    def _detach(self, w: asyncio.StreamWriter) -> None:
        if self.client is w:
            self.client = None
            self.detached_at = time.monotonic()
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- api connection ----------------------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.readline(), 15)
            hdr = json.loads(first or b"{}")
        except Exception:  # noqa: BLE001
            writer.close()
            return
        if not hmac.compare_digest(str(hdr.get("token") or ""), self.token):
            _log("connection refused: bad token")
            writer.close()
            return
        async with self._lock:
            if self.client is not None:  # a newer api replica takes over
                self._detach(self.client)
            resumed = self.proc is not None
            if self.proc is None:
                await self.spawn()
            writer.write(self._ctl(state="attached", resumed=resumed,
                                   dropped=self.dropped, exited=self.exit_code))
            while self.buffer:
                writer.write(self.buffer.popleft())
            self.dropped = 0
            await writer.drain()
            self.client = writer
        await self._pump_stdin(reader, writer)

    async def _pump_stdin(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                line = await reader.readline()
            except Exception:  # noqa: BLE001
                line = b""
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if isinstance(msg, dict) and msg.get("type") == "sokkan_supervisor":
                if msg.get("op") == "end_input" and self.proc and self.proc.stdin:
                    self.proc.stdin.close()
                continue
            if isinstance(msg, dict) and msg.get("type") == "control_request":
                req = msg.get("request") or {}
                if req.get("subtype") == "initialize":
                    if self.init_response is not None:
                        # the CLI is initialised already: answer for it (reattach)
                        await self._deliver_to(writer, {
                            "type": "control_response",
                            "response": {"subtype": "success",
                                         "request_id": msg.get("request_id"),
                                         "response": self.init_response}})
                        continue
                    self.init_request_id = msg.get("request_id")
            if self.proc is None or self.proc.stdin is None or self.proc.stdin.is_closing():
                continue
            try:
                self.proc.stdin.write(line)
                await self.proc.stdin.drain()
            except Exception:  # noqa: BLE001 — CLI gone: _pump_stdout reports the exit
                pass
        async with self._lock:
            if self.client is writer:
                self._detach(writer)

    async def _deliver_to(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        async with self._lock:
            writer.write((json.dumps(msg) + "\n").encode())
            await writer.drain()

    # ---- lifetime ----------------------------------------------------------------------
    async def watchdog(self, tick: float = 5.0) -> None:
        while not self.done.is_set():
            await asyncio.sleep(tick)
            if self.client is None and time.monotonic() - self.detached_at > self.idle_s:
                _log(f"no client for {self.idle_s:.0f} s: stopping")
                await self.terminate()
                return

    async def terminate(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self.proc is None:
            self.done.set()
        else:
            await asyncio.wait_for(self.done.wait(), 15)


async def amain() -> int:
    argv = json.loads(os.environ.get("SOKKAN_SESSION_ARGV") or "[]")
    if not argv:
        raise SystemExit("SOKKAN_SESSION_ARGV is empty")
    sup = Supervisor(argv, os.environ.get("SOKKAN_SESSION_CWD"),
                     os.environ.get("SOKKAN_SESSION_TOKEN", ""),
                     idle_s=float(os.environ.get("SOKKAN_SESSION_IDLE_S") or 900),
                     buffer_max=int(os.environ.get("SOKKAN_SESSION_BUFFER") or 5000),
                     env={k: v for k, v in os.environ.items()
                          if k not in ("SOKKAN_SESSION_TOKEN", "SOKKAN_SESSION_ARGV")})
    port = int(os.environ.get("SOKKAN_SUPERVISOR_PORT") or 7070)
    server = await asyncio.start_server(sup.handle, "0.0.0.0", port, limit=LIMIT)
    _log(f"listening on :{port}")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: loop.create_task(sup.terminate()))
    loop.create_task(sup.watchdog())
    await sup.done.wait()
    server.close()
    return int(sup.exit_code or 0)


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
