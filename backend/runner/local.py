"""`local` runner (default): the CLI is a subprocess of the api, as in every release so far.

In the cockpit the SDK spawns it itself (`client_args` returns no transport), so the
behaviour is byte-identical to 3.1. The methods below implement the same contract for
callers that do not go through the SDK (and for the runner test-suite).
"""
from __future__ import annotations

import asyncio
import os

from .base import STREAM_LIMIT, Channel, Handle, SessionRunner, SessionSpec


class LocalRunner(SessionRunner):
    name = "local"
    remote = False

    def __init__(self) -> None:
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def start(self, spec: SessionSpec) -> Handle:
        p = self._procs.get(spec.sid)
        if p is not None and p.returncode is None:
            return Handle(spec.sid, str(p.pid), adopted=True)
        env = {**os.environ, **spec.env}
        p = await asyncio.create_subprocess_exec(
            *spec.argv, cwd=spec.cwd or None, env=env, limit=STREAM_LIMIT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        self._procs[spec.sid] = p
        return Handle(spec.sid, str(p.pid))

    async def open(self, handle: Handle, timeout: float = 0) -> Channel:
        p = self._procs[handle.sid]
        assert p.stdout is not None and p.stdin is not None
        return Channel(p.stdout, p.stdin, {"state": "attached"}, proc=p)  # type: ignore[arg-type]

    async def stop(self, handle: Handle) -> None:
        p = self._procs.pop(handle.sid, None)
        if p is None or p.returncode is not None:
            return
        p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 10)
        except asyncio.TimeoutError:
            p.kill()

    async def status(self, handle: Handle) -> str:
        p = self._procs.get(handle.sid)
        if p is None:
            return "gone"
        if p.returncode is None:
            return "running"
        return "succeeded" if p.returncode == 0 else "failed"

    async def list_live(self) -> list[Handle]:
        return [Handle(sid, str(p.pid)) for sid, p in self._procs.items()
                if p.returncode is None]
