"""`Harness`: the interface every engine adapter implements.

One instance drives ONE run (a session a person talks to, or an unattended agent run).
The cockpit calls, in order::

    h = registry.create(...)             # nothing started yet
    stream = h.start(spec)               # the run's event stream — a single iterator
    async for ev in stream: ...          # first event: run.started (the engine is reached)
    await h.send(prompt)                 # → turn.started … turn.ended in the stream
    await h.decide(gate_id, decision)    # the answer to a gate.requested
    await h.interrupt()                  # stop the turn, keep the run
    await h.stop()                       # end the run → run.ended, the stream finishes

Rules an adapter keeps:

* Every engine-specific type stays inside the adapter. What comes out is `contract` events.
* Total order per run; identifiers (`turn_id`, `call_id`, `gate_id`) stable within the run.
* A gate blocks only what it guards: the stream keeps flowing while a gate waits, and
  `decide()` can be called from another task than the one that iterates.
* The stream survives its consumer: cancelling an iteration loses nothing and kills
  nothing (the next iteration continues), only `stop()` ends it.
* An adapter never decides a gate by itself in place of the cockpit, never recalls memory
  by itself, never prices tokens.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from .contract import Capabilities, Decision, Event, HistoryItem, RunSpec, Unsupported


class Harness(ABC):
    #: the adapter's id, as declared in the registry (`run.started.harness`)
    name: str = ""

    @abstractmethod
    def start(self, spec: RunSpec) -> AsyncIterator[Event]:
        """The event stream of the run. Reaching the engine happens on the first
        iteration: it yields ``run.started``, or raises when the engine cannot be
        reached (nothing was started). Called once per instance."""

    @abstractmethod
    async def send(self, prompt: str) -> str:
        """Start a turn with this prompt; returns its ``turn_id``. The turn's events come
        through the stream, ending with ``turn.ended``. One turn at a time."""

    @abstractmethod
    async def decide(self, gate_id: str, decision: Decision) -> bool:
        """Answer a ``gate.requested``. False when the gate is unknown or already resolved
        (decided elsewhere, expired): nothing was applied. What was applied is said by
        ``gate.resolved`` — for an engine whose gates are born in the engine
        (``Capabilities.gate_authority == "engine"``) the outcome can be stricter than the
        decision, never looser."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop the running turn; the run stays usable. No-op when nothing runs."""

    @abstractmethod
    async def stop(self) -> None:
        """End the run and free what the engine holds for it. Pending gates are dropped
        (the engine is told ``deny``), ``run.ended`` closes the stream. Idempotent."""

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """What this engine can do. Available before ``start``."""

    async def set_mode(self, mode: str) -> None:
        """Engage an engine-side mode (``"plan"``, or ``"default"`` to leave it). Only for
        what must change the ENGINE's behaviour: auto-approval modes are rules of the
        cockpit, applied when a gate is requested, never a setting of the engine."""
        if mode != "default":
            raise Unsupported(f"{self.name or type(self).__name__} has no {mode!r} mode")

    def history(self, ref: str, *, cwd: str = "") -> list[HistoryItem]:
        """The thread of a run as the engine keeps it, oldest first: what the cockpit shows
        when it reads a conversation back (a pane after a restart, an export). ``ref`` is
        the engine's own reference to the run (``run.info.engine_session``, what
        ``RunSpec.resume`` takes); ``cwd`` the run's working directory, for an engine that
        files its sessions by folder. Available without ``start``, like ``capabilities``.

        An unknown reference is an empty thread, never an error (a run the engine never
        kept, or one it has pruned). An engine that cannot read a thread back declares
        ``Capabilities.history = False`` and raises ``Unsupported`` here: the cockpit
        says the thread is not available, it never rebuilds one from somewhere else."""
        raise Unsupported(f"{self.name or type(self).__name__} cannot read a thread back")
