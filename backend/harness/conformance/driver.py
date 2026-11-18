"""What a conformance scenario drives an engine with: a `Rig` (the engine-specific part,
provided by the test of each adapter) and a `Run` (start, turns, stop — over the `Harness`
interface only)."""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..base import Harness
from ..contract import (
    Capabilities, Decision, Event, GateRequested, GateResolved, RunEnded, RunSpec,
    RunStarted, ToolCalled, ToolResult, TurnEnded, TurnStarted, event_from_dict,
)

# ---- what a scenario tells the model to do (the rig translates to the engine's tools) ----


@dataclass(frozen=True)
class Step:
    kind: str                   # say | shell | write | ask
    text: str = ""              # say: the text; shell: the command; ask: the question
    path: str = ""              # write: where
    content: str = ""           # write: what
    options: tuple[str, ...] = ()   # ask: the choices


def say(text: str) -> Step:
    return Step("say", text=text)


def shell(command: str) -> Step:
    return Step("shell", text=command)


def write(path: str, content: str) -> Step:
    return Step("write", path=str(path), content=content)


def ask(question: str, options: tuple[str, ...]) -> Step:
    return Step("ask", text=question, options=tuple(options))


class Rig:
    """The engine-specific part of the scenarios. One rig = one fresh workspace."""
    name: str = ""
    workspace: Path             # the run's working directory
    outside: Path               # a directory the run must not write to

    def capabilities(self) -> Capabilities:
        raise NotImplementedError

    def harness(self) -> Harness:
        """A new, not started, harness."""
        raise NotImplementedError

    def spec(self, *, context=None, resume: str | None = None, confined: bool = False
             ) -> RunSpec:
        """A run in `workspace` (confined to it when `confined`)."""
        raise NotImplementedError

    def script(self, *turns: list[Step]) -> None:
        """What the model does in the next turns: one list of steps per turn (after its
        steps a turn ends with a short text)."""
        raise NotImplementedError

    def model_saw(self, text: str) -> bool:
        """Was this text shown to the model, in any request so far?"""
        raise NotImplementedError

    def last_request_saw(self, text: str) -> bool:
        """Was it in the LAST request (history included)?"""
        raise NotImplementedError


# ---- one run -------------------------------------------------------------------------

Decider = Callable[[Any], "Decision | None | Any"]


def allow(gate) -> Decision:
    return Decision.allow(by="conformance", via="rail")


def deny(gate) -> Decision:
    return Decision.deny("Denied by the conformance scenario", by="conformance", via="rail")


@dataclass
class Turn:
    turn_id: str
    events: list[Event] = field(default_factory=list)

    def of(self, cls) -> list:
        return [e for e in self.events if isinstance(e, cls)]

    @property
    def ended(self) -> TurnEnded:
        return self.of(TurnEnded)[-1]


class Run:
    """`async with Run(harness, spec) as run: t = await run.turn(prompt, decide)`."""

    def __init__(self, harness: Harness, spec: RunSpec, timeout: float = 90.0) -> None:
        self.harness, self.spec, self.timeout = harness, spec, timeout
        self.events: list[Event] = []
        self.started: RunStarted | None = None
        self.ended: RunEnded | None = None
        self._stream = None

    async def __aenter__(self) -> "Run":
        self._stream = self.harness.start(self.spec)
        first = await asyncio.wait_for(self._stream.__anext__(), self.timeout)
        assert isinstance(first, RunStarted), f"the first event must be run.started: {first}"
        self.started = first
        self.events.append(first)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def stop(self) -> RunEnded | None:
        if self.ended is not None:
            return self.ended
        await self.harness.stop()

        async def _rest():
            async for ev in self._stream:
                self.events.append(ev)
                if isinstance(ev, RunEnded):
                    self.ended = ev
        await asyncio.wait_for(_rest(), self.timeout)
        return self.ended

    async def next_event(self) -> Event:
        ev = await asyncio.wait_for(self._stream.__anext__(), self.timeout)
        self.events.append(ev)
        return ev

    async def turn(self, prompt: str, decide: Decider | None = allow) -> Turn:
        """One turn to its end. `decide(gate)` answers each gate (None = leave it
        pending: the scenario decides later with `harness.decide`)."""
        turn = Turn(await self.harness.send(prompt))
        while True:
            ev = await self.next_event()
            turn.events.append(ev)
            if isinstance(ev, GateRequested) and decide is not None:
                d = decide(ev.gate)
                if inspect.isawaitable(d):
                    d = await d
                if d is not None:
                    await self.harness.decide(ev.gate.gate_id, d)
            if isinstance(ev, TurnEnded) and ev.turn_id == turn.turn_id:
                check_turn(turn, self.harness.capabilities())
                return turn


def check_turn(turn: Turn, caps: Capabilities) -> None:
    """What holds for every turn of every engine."""
    evs = turn.events
    started = [i for i, e in enumerate(evs) if isinstance(e, TurnStarted)
               and e.turn_id == turn.turn_id]
    assert started, "no turn.started for the turn"
    assert isinstance(evs[-1], TurnEnded), "the turn does not end with turn.ended"
    for e in evs:
        again = event_from_dict(e.to_dict())
        assert again == e, f"{e.type} does not round-trip: {again} != {e}"
    gates = {e.gate.gate_id: e.gate for e in evs if isinstance(e, GateRequested)}
    assert len(gates) == len([e for e in evs if isinstance(e, GateRequested)]), \
        "a gate id was used twice"
    for g in gates.values():
        assert g.kind in caps.gate_kinds, f"gate kind {g.kind} is not declared"
        assert set(g.options) <= set(caps.decisions), \
            f"gate options {g.options} outside the declared decisions"
    resolved = [e.gate_id for e in evs if isinstance(e, GateResolved)]
    assert len(resolved) == len(set(resolved)), "a gate was resolved twice"
    assert set(resolved) <= set(gates), "a gate was resolved that was never requested"
    called = {e.call_id for e in evs if isinstance(e, ToolCalled)}
    for r in (e for e in evs if isinstance(e, ToolResult)):
        assert r.call_id in called, f"tool.result {r.call_id} without its tool.called"
