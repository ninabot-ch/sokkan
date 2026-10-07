"""`RunnerTransport`: the Claude Agent SDK's Transport over a session runner.

The SDK (Query) keeps doing everything it does for a local subprocess — initialize, control
protocol (can_use_tool, hooks, interrupt, set_permission_mode), message parsing; this class
only moves the stream-json lines to the session container's supervisor and back.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, AsyncIterator

try:
    from claude_agent_sdk._internal.transport import Transport  # type: ignore
except ImportError:  # pragma: no cover — older SDK layout
    from claude_agent_sdk.transport import Transport  # type: ignore

from .base import Channel, Handle, SessionRunner, SessionSpec


class RunnerTransport(Transport):
    def __init__(self, runner: SessionRunner, spec: SessionSpec, on_close=None):
        self.runner = runner
        self.spec = spec
        self.handle: Handle | None = None
        self.chan: Channel | None = None
        self._ready = False
        self._on_close = on_close
        self.before_connect = None  # async hook: relay listener, startup reconcile

    async def connect(self) -> None:
        if self.before_connect is not None:
            await self.before_connect()
        self.handle = await self.runner.start(self.spec)
        self.chan = await self.runner.open(self.handle)
        self._ready = True
        if self.handle.adopted:
            print(f"[sokkan] runner {self.runner.name}: reattached session {self.spec.sid} "
                  f"to {self.handle.name}", file=sys.stderr)

    async def write(self, data: str) -> None:
        if not self.chan:
            raise ConnectionError("runner transport not connected")
        await self.chan.send(data)

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read()

    async def _read(self) -> AsyncIterator[dict[str, Any]]:
        assert self.chan is not None
        async for msg in self.chan.events():
            yield msg
        self._ready = False
        if self.chan.exited not in (None, 0):
            # surfaced by the SDK like a CLI process that died
            raise RuntimeError(f"session CLI exited with {self.chan.exited}")

    async def end_input(self) -> None:
        if self.chan:
            try:
                await self.chan.end_input()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        """Session dropped (or run finished): end the container. Bounded (SDK contract)."""
        self._ready = False
        if self.chan:
            await self.chan.close()
        if self.handle:
            try:
                await asyncio.wait_for(self.runner.stop(self.handle), 30)
            except Exception as e:  # noqa: BLE001 — reconcile() removes leftovers
                print(f"[sokkan] runner {self.runner.name}: stop {self.handle.name}: {e!r}",
                      file=sys.stderr)
        if self._on_close:
            self._on_close()

    def is_ready(self) -> bool:
        return self._ready


def dumps(msg: dict) -> str:
    return json.dumps(msg) + "\n"
