"""A small in-memory engine, for the conformance scenarios and the cockpit's own tests.

It is NOT an engine of an instance (the registry does not declare it) and it calls no
model: a `ReferenceModel` plays scripted steps. It exists because one implementation does
not prove an interface. It is deliberately unlike the CLI adapter where the contract
allows engines to differ:

* its **gates are born in the engine** (`gate_authority="engine"`): it has its own policy,
  asks only for what that policy does not settle, refuses some commands without asking
  (`never`), and can still refuse what the cockpit allowed (`revoke`) — the outcome is in
  `gate.resolved`, never looser than the decision;
* it takes no edited input (`edit` is not among its decisions);
* it has no question tool and no plan mode unless built with them (C7);
* its context path is a **preamble** (the recall is put in front of the prompt);
* it confines its tools itself (`sandbox="engine"`) and reports usage as events.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from collections import deque
from typing import Any, AsyncIterator, ClassVar

from ..base import Harness
from ..contract import (
    ALLOW_ONCE, ANSWER, DENY,
    AssistantText, Capabilities, ContextInjected, ContextRequest, ContractError, Decision,
    Error, Event, Gate, GateRequested, GateResolved, RunEnded, RunInfo, RunSpec, RunStarted,
    ToolCalled, ToolResult, TurnEnded, TurnStarted, Unsupported, Usage, prompt_ref,
)

NAME = "reference"
MODEL = "reference-1"


class ReferenceModel:
    """The scripted model behind the engine: `script` holds one list of steps per coming
    turn; `requests` keeps everything it was shown (one text per turn)."""

    def __init__(self) -> None:
        self.script: deque = deque()
        self.requests: list[str] = []

    def next(self, shown: str) -> list:
        self.requests.append(shown)
        return list(self.script.popleft()) if self.script else []


class _Mailbox:
    def __init__(self) -> None:
        self._items: deque = deque()
        self._waiter: asyncio.Future | None = None

    def put(self, item) -> None:
        self._items.append(item)
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    async def get(self):
        while not self._items:
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        return self._items.popleft()


class _Stream:
    def __init__(self, h: "ReferenceHarness") -> None:
        self._h = h

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        h = self._h
        if h._finished:
            raise StopAsyncIteration
        ev = await h._mail.get()
        if isinstance(ev, RunEnded):
            h._finished = True
        return ev


class ReferenceHarness(Harness):
    name = NAME
    # engine session → what was said so far (what a resumed run finds again)
    _sessions: ClassVar[dict[str, list[str]]] = {}

    def __init__(self, model: ReferenceModel | None = None, *, question: bool = False,
                 plan: bool = False, never: tuple[str, ...] = ("sudo ",),
                 revoke: tuple[str, ...] = ()) -> None:
        self.model = model or ReferenceModel()
        self._question, self._plan = question, plan
        self._never, self._revoke = never, revoke
        self._spec: RunSpec | None = None
        self._mail = _Mailbox()
        self._gates: dict[str, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._session = ""
        self._history: list[str] = []
        self._stopped = self._finished = False
        self.mode = "default"

    def capabilities(self) -> Capabilities:
        kinds = ("shell", "file_write") + (("question",) if self._question else ())
        return Capabilities(
            gate_kinds=kinds,
            decisions=(ALLOW_ONCE, DENY) + ((ANSWER,) if self._question else ()),
            gate_authority="engine", question=self._question, plan=self._plan,
            interrupt=True, thinking=False, streaming=False, subagents=False, resume=True,
            context=("preamble",), mcp_stdio=False, sandbox="engine", identity="opaque",
            usage="events", budget=())

    # ---- Harness ---------------------------------------------------------------------
    def start(self, spec: RunSpec) -> AsyncIterator[Event]:
        self._spec = spec
        if spec.resume:
            if spec.resume not in self._sessions:
                raise ContractError(f"unknown engine session {spec.resume!r}")
            self._session = spec.resume
        else:
            self._session = "ref-" + uuid.uuid4().hex[:12]
            self._sessions[self._session] = []
        self._history = self._sessions[self._session]
        self._mail.put(RunStarted(run_id=spec.run_id, harness=NAME, sid=spec.run_id,
                                  cwd=spec.cwd, model=MODEL, project=spec.project,
                                  resumed_from=spec.resume))
        self._mail.put(RunInfo(engine_session=self._session, model=MODEL))
        return _Stream(self)

    async def send(self, prompt: str) -> str:
        if self._spec is None or self._stopped:
            raise ContractError("send() on a run that is not started")
        if self._task is not None and not self._task.done():
            raise ContractError("a turn is already running")
        turn = uuid.uuid4().hex[:12]
        self._mail.put(TurnStarted(turn_id=turn, prompt_ref=prompt_ref(prompt)))
        self._task = asyncio.create_task(self._turn(turn, prompt))
        return turn

    async def decide(self, gate_id: str, decision: Decision) -> bool:
        fut = self._gates.get(gate_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    async def interrupt(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def set_mode(self, mode: str) -> None:
        if mode == "plan" and not self._plan:
            raise Unsupported("the reference engine was built without a plan mode")
        self.mode = mode

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        for fut in list(self._gates.values()):
            if not fut.done():
                fut.set_result(Decision.deny("Request cancelled", via="engine"))
        self._mail.put(RunEnded(status="ended", resumable=True, engine_session=self._session))

    # ---- the engine's side of a gate -----------------------------------------------
    def expire(self, gate_id: str) -> bool:
        """The engine settles a waiting gate by itself (its own delay ran out)."""
        fut = self._gates.get(gate_id)
        if fut is None or fut.done():
            return False
        fut.set_result(Decision.deny("The request expired", via="timeout"))
        return True

    async def _gate(self, turn: str, kind: str, subject: dict, options: tuple[str, ...]
                    ) -> Decision:
        gate = Gate(gate_id="ap-" + uuid.uuid4().hex[:10], kind=kind, subject=subject,
                    options=options, turn_id=turn)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._gates[gate.gate_id] = fut
        self._mail.put(GateRequested(gate=gate))
        try:
            d: Decision = await fut
        finally:
            self._gates.pop(gate.gate_id, None)
        if d.kind not in options:
            d = Decision.deny(f"decision {d.kind!r} is not offered for this request",
                              via="engine")
        elif d.allowed and kind == "shell" and any(
                r in subject.get("command", "") for r in self._revoke):
            # the cockpit allowed; the engine's own policy still says no
            d = Decision.deny("Refused by the engine's policy", by=d.by, via="engine")
        self._mail.put(GateResolved(gate_id=gate.gate_id, decision=d.kind, by=d.by, via=d.via,
                                    message=d.message if d.denied else ""))
        return d

    # ---- one turn --------------------------------------------------------------------
    def _inside(self, path: str) -> bool:
        root = os.path.realpath(self._spec.cwd)
        p = os.path.realpath(path)
        return p == root or p.startswith(root + os.sep)

    async def _turn(self, turn: str, prompt: str) -> None:
        spec, put = self._spec, self._mail.put
        said = ""
        try:
            shown = prompt
            if spec.context is not None:
                try:
                    block = await spec.context(ContextRequest(text=prompt, channel="prompt",
                                                              turn_id=turn))
                except Exception:  # noqa: BLE001 — a recall never breaks a turn
                    block = None
                if block is not None and block.text:
                    shown = block.text + "\n\n" + prompt
                    put(ContextInjected(turn_id=turn, source="preamble",
                                        note_names=tuple(block.note_names),
                                        chars=len(block.text)))
            request = "\n".join([*self._history, shown])
            steps = self.model.next(request)
            self._history.append(prompt)
            results: list[str] = []
            for n, step in enumerate(steps):
                call = f"call-{turn}-{n}"
                if step.kind == "say":
                    said = step.text
                    put(AssistantText(turn_id=turn, text=step.text))
                elif step.kind == "shell":
                    results.append(await self._shell(turn, call, step.text))
                elif step.kind == "write":
                    results.append(await self._write(turn, call, step.path, step.content))
                elif step.kind == "ask":
                    results.append(await self._ask(turn, call, step.text, step.options))
            if results:
                # what the tools returned goes back to the model (its next request)
                self._history.append("\n".join(results))
                self.model.requests.append("\n".join([*self._history]))
            if not said:
                said = "Done."
                put(AssistantText(turn_id=turn, text=said))
            self._history.append(said)
            put(Usage(turn_id=turn, model=MODEL, input=max(1, len(request) // 4),
                      output=max(1, len(said) // 4), basis="local",
                      message_id=f"msg-{turn}"))
            put(TurnEnded(turn_id=turn, stop_reason="success", text=said, num_turns=1))
        except asyncio.CancelledError:
            put(TurnEnded(turn_id=turn, stop_reason="interrupted", reported=False))
            raise
        except Exception as e:  # noqa: BLE001
            put(Error(code=type(e).__name__, message=str(e), recoverable=False))
            put(TurnEnded(turn_id=turn, stop_reason="error", is_error=True, reported=False))

    async def _shell(self, turn: str, call: str, command: str) -> str:
        put = self._mail.put
        put(ToolCalled(turn_id=turn, call_id=call, name="shell", input={"command": command},
                       classification="shell"))
        if any(n in command for n in self._never):
            # refused by the engine's own policy, without asking: no gate reaches the cockpit
            out = "Refused by the engine's policy"
            put(ToolResult(call_id=call, output=out, ok=False, turn_id=turn))
            return out
        d = await self._gate(turn, "shell", {"tool": "shell", "input": {"command": command},
                                             "command": command}, (ALLOW_ONCE, DENY))
        if d.denied:
            put(ToolResult(call_id=call, output=d.message, ok=False, turn_id=turn))
            return d.message
        r = await asyncio.to_thread(subprocess.run, command, shell=True, cwd=self._spec.cwd,
                                    capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        put(ToolResult(call_id=call, output=out, ok=r.returncode == 0, turn_id=turn))
        return out

    async def _write(self, turn: str, call: str, path: str, content: str) -> str:
        put = self._mail.put
        full = path if os.path.isabs(path) else os.path.join(self._spec.cwd, path)
        put(ToolCalled(turn_id=turn, call_id=call, name="write_file",
                       input={"path": path, "content": content}, classification="file_write"))
        if self._spec.confinement is not None and not self._inside(full):
            why = f"{path} is outside the run's space"
            put(Error(code="confined", message=why, recoverable=True,
                      data={"tool": "write_file", "shell_off": False}))
            put(ToolResult(call_id=call, output=why, ok=False, turn_id=turn))
            return why
        d = await self._gate(turn, "file_write",
                             {"tool": "write_file", "input": {"path": path, "content": content},
                              "path": full}, (ALLOW_ONCE, DENY))
        if d.denied:
            put(ToolResult(call_id=call, output=d.message, ok=False, turn_id=turn))
            return d.message
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        put(ToolResult(call_id=call, output="written", ok=True, turn_id=turn))
        return "written"

    async def _ask(self, turn: str, call: str, question: str, options: tuple[str, ...]) -> str:
        put = self._mail.put
        if not self._question:
            # no question tool in this engine: the model has nothing to call. Nothing is
            # emulated (no gate, no card); it is told, and goes on.
            why = "this engine has no question tool"
            put(Error(code="unsupported", message=why, recoverable=True,
                      data={"capability": "question"}))
            return why
        inp: dict[str, Any] = {"questions": [{
            "question": question, "header": question[:12], "multiSelect": False,
            "options": [{"label": o, "description": o} for o in options]}]}
        put(ToolCalled(turn_id=turn, call_id=call, name="ask", input=inp,
                       classification="other"))
        d = await self._gate(turn, "question", {"tool": "ask", "input": inp}, (ANSWER, DENY))
        out = d.message if d.denied else json.dumps(d.value, ensure_ascii=False)
        put(ToolResult(call_id=call, output=out, ok=d.allowed, turn_id=turn))
        return out
