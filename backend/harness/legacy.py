"""legacy.py — the adapter of the engine SOKKAN has driven since 2.x: the Claude Code CLI,
piloted through the Claude Agent SDK (stream-json, over a local subprocess or a session
container).

Everything that touches that SDK lives HERE and nowhere else in the backend: its client,
its message and permission types, its hooks. What comes out is `harness.contract`.

How the three contracts are realised with this engine:

* **run** — `ClaudeSDKClient.query()` / `receive_response()`, one pump task per turn,
  translated message by message (`translate`).
* **gate** — the SDK's `can_use_tool` callback. The CLI is started at its strictest (never
  a bypass mode): every tool that is not in `RunSpec.auto_allow` asks, the callback emits
  `gate.requested` and waits for `decide()`. Questions with choices and the plan are
  gates like the others (`question`, `plan`).
* **context** — two SDK hooks: `UserPromptSubmit` (the recall of the turn, as additional
  context) and `PreToolUse` on the sub-agent tools (the recall of the sub-agent's subject,
  appended to its prompt). The content comes from `RunSpec.context`; this adapter only
  delivers it, and says so with `context.injected` (`source="hook"`).

The project confinement (`RunSpec.confinement`) is one more `PreToolUse` hook (it runs
before any allow rule of the CLI's own settings); the policy itself is `sandbox.decide`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import time
import uuid
from collections import deque
from typing import Any, AsyncIterator

# --- SDK (imports tolerant to packaging variations between versions) --------------------
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher  # type: ignore

try:  # permission classes
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny  # type: ignore
except ImportError:  # pragma: no cover
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny  # type: ignore

try:  # message blocks
    from claude_agent_sdk import (  # type: ignore
        ResultMessage, SystemMessage,
        TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock,
    )
except ImportError:  # pragma: no cover
    from claude_agent_sdk.types import (  # type: ignore
        ResultMessage, SystemMessage,
        TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock,
    )

from .base import Harness
from .refusals import ATELIER_NO_SHELL
from .contract import (
    ALLOW_ONCE, ANSWER, DENY, EDIT,
    AssistantText, AssistantThink, Capabilities, Confinement, ContextInjected,
    ContextProvider, ContextRequest, ContractError, Decision, Error, Event, Gate,
    GateRequested, GateResolved, RunEnded, RunInfo, RunSpec, RunStarted, ToolCalled,
    ToolResult, TurnEnded, TurnStarted, Usage, prompt_ref,
)

NAME = "legacy"
LABEL = "Claude Code CLI (Claude Agent SDK)"

HOOK_TIMEOUT_S = 5          # hard limit given to the CLI; the recall budget is ~1.5 s
SUBAGENT_TOOLS = ("Task", "Agent")
SUBAGENT_MATCHER = "|".join(SUBAGENT_TOOLS)

CAPABILITIES = Capabilities(
    gate_kinds=("tool", "shell", "file_write", "net", "mcp", "question", "plan"),
    decisions=(ALLOW_ONCE, DENY, ANSWER, EDIT),
    gate_authority="core",
    question=True, plan=True, interrupt=True, thinking=True, streaming=False,
    subagents=True, resume=True, context=("hook", "mcp"), mcp_stdio=True,
    sandbox="host", identity="plain", usage="transcripts", budget=("usd",), history=True,
)

try:  # the SDK's transport interface (the session runner carries it to a container)
    from claude_agent_sdk._internal.transport import Transport  # type: ignore
except ImportError:  # pragma: no cover — older SDK layout
    from claude_agent_sdk.transport import Transport  # type: ignore

try:  # option fields of the installed SDK (max_budget_usd, add_dirs do not exist everywhere)
    _OPTION_FIELDS = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
except Exception:  # noqa: BLE001
    _OPTION_FIELDS = set()


# ---- the engine's tool vocabulary → the contract's ---------------------------------------

# 3.6.8 — every tool of the CLI that runs a shell command (Monitor runs one per watch) or only
# serves one (output, stop). A run without a shell (`RunSpec.shell=False`, an Atelier work) has
# none of them: out of its catalogue (`disallowed_tools`, which the CLI applies to sub-agents
# too), and refused by a PreToolUse hook if one is called anyway — hooks run before any allow
# rule and before the CLI's own approval of a read-only command, which never reaches
# can_use_tool (parity bench, 16.11.2026: `ls …` and a sub-agent's `pwd && ls` ran unasked).
SHELL_TOOLS = ("Bash", "Monitor", "BashOutput", "KillShell", "KillBash", "PowerShell")
_SHELL = set(SHELL_TOOLS)
_FILE_WRITE = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
_FILE_READ = {"Read", "Glob", "Grep", "NotebookRead"}
_NET = {"WebFetch", "WebSearch"}
QUESTION_TOOL = "AskUserQuestion"
PLAN_TOOL = "ExitPlanMode"


def classify(tool_name: str) -> str:
    """`tool.called.classification` of one of the CLI's tools."""
    if tool_name in _SHELL:
        return "shell"
    if tool_name in _FILE_WRITE:
        return "file_write"
    if tool_name in _FILE_READ:
        return "file_read"
    if tool_name in _NET:
        return "net"
    if tool_name in SUBAGENT_TOOLS:
        return "subagent"
    if tool_name.startswith("mcp__"):
        return "mcp"
    return "other"


def gate_kind(tool_name: str) -> str:
    if tool_name == QUESTION_TOOL:
        return "question"
    if tool_name == PLAN_TOOL:
        return "plan"
    c = classify(tool_name)
    return c if c in ("shell", "file_write", "net", "mcp") else "tool"


def tool_gate(tool_name: str, input_data: dict | None, *, gate_id: str | None = None,
              turn_id: str | None = None) -> Gate:
    """The gate of one permission request of the CLI (`can_use_tool`)."""
    kind = gate_kind(tool_name)
    inp = input_data or {}
    subject: dict[str, Any] = {"tool": tool_name, "input": inp}
    if kind == "shell":
        subject["command"] = str(inp.get("command") or "")
    elif kind == "file_write":
        subject["path"] = str(inp.get("file_path") or inp.get("notebook_path") or "")
    return Gate(gate_id=gate_id or uuid.uuid4().hex, kind=kind, subject=subject,
                options=(ANSWER, DENY) if kind == "question" else (ALLOW_ONCE, EDIT, DENY),
                turn_id=turn_id)


def _text_of(content: Any) -> str:
    """Flattens a content (str | list of blocks / dicts) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text", "") if b.get("type") == "text" else "")
            else:
                parts.append(getattr(b, "text", "") or "")
        return "\n".join(p for p in parts if p)
    return ""


def _usage(usage: dict, *, turn_id: str | None, model: str | None, scope: str,
           message_id: str | None = None) -> Usage:
    """The API's usage dict → the contract. The long-lived part of the cache writes is
    kept apart (it is priced differently)."""
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    det = usage.get("cache_creation") or {}
    c1h = c5m = 0
    if isinstance(det, dict):
        c1h = int(det.get("ephemeral_1h_input_tokens") or 0)
        c5m = int(det.get("ephemeral_5m_input_tokens") or 0)
    if c1h + c5m:
        cw = c1h + c5m
    return Usage(turn_id=turn_id, model=model,
                 input=int(usage.get("input_tokens") or 0),
                 output=int(usage.get("output_tokens") or 0),
                 cache_read=int(usage.get("cache_read_input_tokens") or 0),
                 cache_write=cw, cache_write_long=c1h, message_id=message_id, scope=scope)


def _largest(prev: Usage | None, ev: Usage) -> Usage:
    """The same call reported again: the largest of each figure."""
    if prev is None:
        return ev
    return dataclasses.replace(ev, **{k: max(getattr(prev, k), getattr(ev, k)) for k in (
        "input", "output", "cache_read", "cache_write", "cache_write_long")})


# ---- hooks -------------------------------------------------------------------------------

def recall_hooks(provider: ContextProvider | None, emit=None, turn_id=None) -> dict:
    """`ClaudeAgentOptions.hooks` delivering the cockpit's recall ({} without a provider).
    In-process callbacks: no process start per message. ``emit(event)`` receives the
    `context.injected` events, ``turn_id()`` gives the running turn."""
    if provider is None:
        return {}

    def _said(block, channel: str) -> None:
        if emit is not None:
            emit(ContextInjected(turn_id=turn_id() if turn_id else None, source="hook",
                                 note_names=tuple(block.note_names), chars=len(block.text),
                                 channel=channel))

    async def _ask(req: ContextRequest):
        try:
            block = await asyncio.wait_for(provider(req), timeout=HOOK_TIMEOUT_S - 1)
        except Exception as e:  # noqa: BLE001 — a recall never breaks a turn
            print(f"[sokkan] recall hook failed: {e!r}", file=sys.stderr)
            return None
        return block if block is not None and block.text else None

    async def on_prompt(payload, _tool_use_id, _context):
        if payload.get("hook_event_name") != "UserPromptSubmit":
            return {}
        block = await _ask(ContextRequest(
            text=payload.get("prompt") or "", channel="prompt",
            turn_id=turn_id() if turn_id else None, agent_id=payload.get("agent_id")))
        if block is None:
            return {}
        _said(block, "prompt")
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": block.text},
                "suppressOutput": True}

    async def on_subagent(payload, _tool_use_id, _context):
        if payload.get("hook_event_name") != "PreToolUse" \
                or payload.get("tool_name") not in SUBAGENT_TOOLS:
            return {}
        tin = dict(payload.get("tool_input") or {})
        prompt = str(tin.get("prompt") or "")
        if not prompt:
            return {}
        block = await _ask(ContextRequest(
            text=prompt, title=str(tin.get("description") or ""), channel="subagent",
            turn_id=turn_id() if turn_id else None,
            agent_id=payload.get("tool_use_id") or None))
        if block is None:
            return {}
        _said(block, "subagent")
        tin["prompt"] = prompt + "\n" + block.text
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": tin},
                "suppressOutput": True}

    return {
        "UserPromptSubmit": [HookMatcher(hooks=[on_prompt], timeout=HOOK_TIMEOUT_S)],
        "PreToolUse": [HookMatcher(matcher=SUBAGENT_MATCHER, hooks=[on_subagent],
                                   timeout=HOOK_TIMEOUT_S)],
    }


def confinement_hooks(conf: Confinement | None, *, sid: str, user: str, cwd: str,
                      on_denied=None) -> dict:
    """`ClaudeAgentOptions.hooks` entries confining one run to its project ({} when the
    confinement does not apply). ``on_denied(tool, reason, shell_off)`` is called for every
    refusal (the person is told, not only the model)."""
    import sandbox
    if conf is None or not sandbox.applies(conf.project):
        return {}
    script = (sandbox.wrapper_script(sid, conf.project, cwd, list(conf.env_names),
                                     conf.git_socket)
              if sandbox.mode() == sandbox.BWRAP else None)

    async def pre_tool(payload, _tool_use_id, _context):
        try:
            tool = str(payload.get("tool_name") or "")
            out = sandbox.decide(tool, payload.get("tool_input") or {},
                                 sid=sid, user=user, project=conf.project, cwd=cwd,
                                 script=script, auto_rules=list(conf.rules),
                                 pod=sandbox.mode() == sandbox.POD)
            spec = out.get("hookSpecificOutput", {})
            if on_denied is not None and spec.get("permissionDecision") == "deny":
                try:
                    on_denied(tool, str(spec.get("permissionDecisionReason") or ""),
                              tool == "Bash" and sandbox.mode() == sandbox.HOOKS)
                except Exception:  # noqa: BLE001 — the refusal stands regardless
                    pass
            return out
        except Exception as e:  # noqa: BLE001 — fail closed
            return sandbox._deny(f"Sandbox check failed: {e!r}")

    return {"PreToolUse": [HookMatcher(matcher=sandbox.HOOK_MATCHER, hooks=[pre_tool])]}


def calc_hooks() -> list:
    """PostToolUse on the file tools (4.0 native formats): after a deliverable of an Atelier
    work is saved, the figures SOKKAN computed from its formulas are handed to the model as
    the tool's context — it quotes them instead of computing in its head (`outputs`)."""

    async def after_save(payload, _tool_use_id, _context):
        payload = payload or {}
        if payload.get("hook_event_name") != "PostToolUse":
            return {}
        inp = payload.get("tool_input") or {}
        path = str(inp.get("file_path") or "") if isinstance(inp, dict) else ""
        parts = os.path.abspath(path).split(os.sep) if path else []
        if len(parts) < 3 or parts[-3] != "deliverables":
            return {}
        try:
            import outputs
            text = outputs.write_calc(path)
        except Exception:  # noqa: BLE001 — a save never fails on its figures
            return {}
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}

    return [HookMatcher(matcher="Write|Edit|MultiEdit", hooks=[after_save])]


def no_shell_hooks(on_refused=None) -> list:
    """PreToolUse matchers refusing every shell tool of the CLI (3.6.8): for the session and
    each of its sub-agents. ``on_refused(tool, input)`` is told of each refusal."""

    async def no_shell(payload, _tool_use_id, _context):
        payload = payload or {}
        tool = str(payload.get("tool_name") or "Bash")
        inp = payload.get("tool_input") or {}
        if on_refused is not None:
            try:
                on_refused(tool, inp if isinstance(inp, dict) else {})
            except Exception:  # noqa: BLE001 — the refusal stands regardless
                pass
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": ATELIER_NO_SHELL}}

    return [HookMatcher(matcher="|".join(SHELL_TOOLS), hooks=[no_shell])]


# ---- the adapter -------------------------------------------------------------------------

class _Mailbox:
    """The run's events, in order, for ONE consumer. Not bound to an event loop (a session
    can be driven from successive loops: the API's, a test's); cancelling a `get` loses
    nothing."""

    def __init__(self) -> None:
        self._items: deque = deque()
        self._waiter: asyncio.Future | None = None

    def put(self, item) -> None:
        self._items.append(item)
        w = self._waiter
        if w is not None and not w.done():
            w.set_result(None)

    async def get(self):
        while not self._items:
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        return self._items.popleft()

    def drain(self) -> list:
        out = list(self._items)
        self._items.clear()
        return out


class _Stream:
    """The single event iterator of a run (see `Harness.start`)."""

    def __init__(self, harness: "LegacyHarness") -> None:
        self._h = harness

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> Event:
        h = self._h
        if h._finished:
            raise StopAsyncIteration
        if not h._connected and not h._connect_tried:
            # once: a CLI that could not be started is not started again behind the
            # cockpit's back — the turns that follow say why they fail
            h._connect_tried = True
            await h._connect()
        ev = await h._mail.get()
        if isinstance(ev, RunEnded):
            h._finished = True
        return ev


class LegacyHarness(Harness):
    name = NAME

    def __init__(self, client: Any = None) -> None:
        # `client`: an already connected SDK client (tests hand a scripted one)
        self._client = client
        self._connected = client is not None
        self._called: dict[str | None, str] = {}   # tool call id → tool name
        self._removed: set[str] = set()             # tools removed from the run (deny_tools)
        self._connect_tried = False
        self._spec: RunSpec | None = None
        self._mail = _Mailbox()
        self._gates: dict[str, asyncio.Future] = {}
        self._turn: str | None = None
        self._pump: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._engine_session: str | None = None
        # total_cost_usd is CUMULATED per CLI process: reset at each (re)start
        self._cost_seen = 0.0
        # the calls of the running turn as first reported ({message id: usage event}), and
        # the per-model totals of the CLI process at the last result (cumulated too)
        self._calls: dict[str, Usage] = {}
        self._model_totals: dict[str, dict[str, int]] = {}
        self._stopped = False
        self._finished = False
        if client is not None:
            self._mail.put(RunStarted(run_id="", harness=NAME, sid="", cwd=""))

    # ---- Harness ---------------------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return CAPABILITIES

    def history(self, ref: str, *, cwd: str = "") -> list:
        """The CLI keeps each session as a JSONL file (`cli_transcript`)."""
        from . import cli_transcript
        return cli_transcript.read(ref, cwd or None)

    def start(self, spec: RunSpec | None) -> AsyncIterator[Event]:
        self._spec = spec
        return _Stream(self)

    async def send(self, prompt: str) -> str:
        if self._client is None:
            raise ContractError("send() before the run is started")
        if self._pump is not None and not self._pump.done():
            raise ContractError("a turn is already running")
        turn = uuid.uuid4().hex[:12]
        self._turn = turn
        self._mail.put(TurnStarted(turn_id=turn, prompt_ref=prompt_ref(prompt)))
        self._pump = asyncio.create_task(self._run_turn(turn, prompt))
        return turn

    async def decide(self, gate_id: str, decision: Decision) -> bool:
        fut = self._gates.get(gate_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def set_mode(self, mode: str) -> None:
        if self._client is not None:
            await self._client.set_permission_mode("plan" if mode == "plan" else "default")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
        for fut in list(self._gates.values()):
            if not fut.done():
                fut.set_result(Decision.deny("Request cancelled", via="engine"))
        client, self._client = self._client, None
        if client is not None and hasattr(client, "__aexit__"):
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            await _reap(client)
        self._mail.put(RunEnded(status="ended", resumable=bool(self._engine_session),
                                engine_session=self._engine_session))

    # ---- tests / diagnostics ---------------------------------------------------------
    def drain(self) -> list[Event]:
        """Events emitted and not yet consumed (non-blocking)."""
        return self._mail.drain()

    # ---- start -----------------------------------------------------------------------
    def _options(self, spec: RunSpec) -> dict[str, Any]:
        """`RunSpec` → the keyword arguments of `ClaudeAgentOptions`."""
        mine = spec.options.get(NAME) or {}
        kw: dict[str, Any] = dict(
            cwd=spec.cwd,
            can_use_tool=self._can_use_tool,
            allowed_tools=list(spec.auto_allow),
            # the CLI never runs in a bypass mode: auto-approval is a rule of the cockpit,
            # applied gate by gate. Only `plan` is engaged in the CLI (it changes what the
            # model does).
            permission_mode="plan" if spec.plan else "default",
            setting_sources=list(mine.get("setting_sources") or []),
            mcp_servers=spec.mcp_servers,
        )
        denied = list(spec.deny_tools) if spec.deny_tools is not None else None
        if not spec.shell:
            denied = list(dict.fromkeys([*(denied or []), *SHELL_TOOLS]))
        if denied is not None:
            # defence in depth: an `allow` rule of the user or project settings bypasses
            # can_use_tool; disallowed_tools always wins
            kw["disallowed_tools"] = denied
            self._removed = set(denied)
        if spec.budget_usd and "max_budget_usd" in _OPTION_FIELDS:
            kw["max_budget_usd"] = spec.budget_usd
        hooks = recall_hooks(spec.context, self._mail.put, lambda: self._turn)
        if not spec.shell:
            hooks = dict(hooks or {})
            hooks["PreToolUse"] = [*no_shell_hooks(self._no_shell),
                                   *(hooks.get("PreToolUse") or [])]
            hooks["PostToolUse"] = [*calc_hooks(), *(hooks.get("PostToolUse") or [])]
        conf = confinement_hooks(spec.confinement, sid=spec.run_id, user=spec.user,
                                 cwd=spec.cwd, on_denied=self._confined)
        for ev, matchers in conf.items():
            hooks = dict(hooks or {})
            hooks[ev] = [*matchers, *(hooks.get(ev) or [])]
        if conf and "add_dirs" in _OPTION_FIELDS:
            kw["add_dirs"] = []
        if hooks:
            kw["hooks"] = hooks
        if spec.env:
            kw["env"] = {**os.environ, **spec.env}
        if spec.model:
            kw["model"] = spec.model
        if spec.resume:
            kw["resume"] = spec.resume
        if mine.get("cli_path") and "cli_path" in _OPTION_FIELDS:
            kw["cli_path"] = mine["cli_path"]   # a CLI pinned in the image, not the SDK's own
        return kw

    async def _connect(self) -> None:
        async with self._connect_lock:
            if self._connected:
                return
            spec = self._spec
            if spec is None:
                raise ContractError("start() needs a RunSpec")
            # runner (SOKKAN_RUNNER): local = the SDK spawns the CLI here; docker /
            # kubernetes = the CLI runs in a session container / pod (backend/runner)
            options, transport = client_args(
                spec.run_id, self._options(spec), user=spec.user, project=spec.project,
                kind=spec.kind)
            self._client = ClaudeSDKClient(options=options, transport=transport)
            self._cost_seen = 0.0   # new CLI process: its counters restart at 0
            self._model_totals = {}
            # __aenter__ rather than `async with`: the client stays open across turns
            await self._client.__aenter__()
            self._connected = True
            self._mail.put(RunStarted(
                run_id=spec.run_id, harness=NAME, sid=spec.run_id, cwd=spec.cwd,
                engine=spec.model_engine, model=spec.model, project=spec.project,
                resumed_from=spec.resume))

    def _no_shell(self, tool: str, inp: dict) -> None:
        """A shell call of a run without a shell, refused by the hook: the cockpit journals
        it as a rule of the space (nobody clicked, no card)."""
        self._mail.put(Error(code="policy_denied", message=ATELIER_NO_SHELL, recoverable=True,
                             data={"tool": tool, "input": inp,
                                   "rule": "simple_profile_no_shell"}))

    def _confined(self, tool: str, reason: str, shell_off: bool) -> None:
        self._mail.put(Error(code="confined", message=reason, recoverable=True,
                             data={"tool": tool, "shell_off": shell_off}))

    # ---- one turn --------------------------------------------------------------------
    async def _run_turn(self, turn: str, prompt: str) -> None:
        client = self._client
        ended = failed = False
        try:
            await client.query(prompt)
            async for msg in client.receive_response():
                for ev in self.translate(msg):
                    ended = ended or isinstance(ev, TurnEnded)
                    self._mail.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — said to the cockpit, the run stays usable
            failed = True
            self._mail.put(Error(code=type(e).__name__, message=str(e), recoverable=False))
        if not ended:
            self._mail.put(TurnEnded(turn_id=turn, stop_reason="error" if failed else "ended",
                                     is_error=failed, reported=False))

    def translate(self, msg: Any) -> list[Event]:
        """One SDK message → contract events."""
        turn = self._turn or ""
        if isinstance(msg, SystemMessage):
            data = getattr(msg, "data", {}) or {}
            sid, model = data.get("session_id"), data.get("model")  # init carries the model
            new_session = bool(sid) and sid != self._engine_session
            if new_session:
                self._engine_session = sid
            if new_session or model:
                return [RunInfo(engine_session=sid if new_session else None,
                                requested_model=model or None)]
            return []

        if isinstance(msg, ResultMessage):
            out: list[Event] = []
            usage = getattr(msg, "usage", None) or {}
            out.extend(self._settle(getattr(msg, "model_usage", None), usage))
            if isinstance(usage, dict):
                out.append(_usage(usage, turn_id=turn, model=None, scope="turn"))
            cost = getattr(msg, "total_cost_usd", None)
            if cost:
                total = float(cost)
                # delta since the last result of THIS process; a smaller total = a process
                # restarted behind our back → the total is the delta
                cost = total - self._cost_seen if total >= self._cost_seen else total
                self._cost_seen = total
            out.append(TurnEnded(
                turn_id=turn, stop_reason=getattr(msg, "subtype", "") or "",
                text=getattr(msg, "result", "") or "",
                is_error=bool(getattr(msg, "is_error", False)),
                num_turns=getattr(msg, "num_turns", None), cost_usd=cost, reported=True,
                duration_ms=getattr(msg, "duration_ms", None)))
            return out

        # AssistantMessage (and UserMessage carrying tool results)
        out = []
        model = getattr(msg, "model", None)
        if model:
            out.append(RunInfo(model=model))    # the model that really answers this turn
        usage = getattr(msg, "usage", None)
        if isinstance(usage, dict):
            ev = _usage(usage, turn_id=turn, model=model, scope="message",
                        message_id=getattr(msg, "message_id", None))
            out.append(ev)
            if ev.message_id:
                self._calls[ev.message_id] = _largest(self._calls.get(ev.message_id), ev)
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            return out
        # 3.6.7: a text block of a USER message is not the agent speaking — the CLI hands a
        # loaded skill's body (« Base directory for this skill: … ») back that way, and the
        # Atelier showed that page as the agent's answer. Only its tool results are read.
        from_user = type(msg).__name__ == "UserMessage"
        for b in content:
            if from_user and isinstance(b, TextBlock):
                continue
            if isinstance(b, TextBlock):
                out.append(AssistantText(turn_id=turn, text=getattr(b, "text", "") or ""))
            elif isinstance(b, ThinkingBlock):
                out.append(AssistantThink(turn_id=turn, text=getattr(b, "thinking", "") or ""))
            elif isinstance(b, ToolUseBlock):
                name = getattr(b, "name", "tool")
                if isinstance(getattr(self, "_called", None), dict):
                    self._called[getattr(b, "id", None)] = name
                out.append(ToolCalled(turn_id=turn, call_id=getattr(b, "id", None), name=name,
                                      input=getattr(b, "input", {}) or {},
                                      classification=classify(name)))
            elif isinstance(b, ToolResultBlock):
                call = getattr(b, "tool_use_id", None)
                said = _text_of(getattr(b, "content", ""))
                name = getattr(self, "_called", {}).get(call, "")
                refused = bool(name) and name in getattr(self, "_removed", set()) and "No such tool available" in said
                if refused:
                    # a tool the cockpit removed from the run (disallowed_tools): a rule of
                    # the space, said in the cockpit's words (harness/refusals.py)
                    from .refusals import removed
                    said = removed(name, classify(name), said)
                out.append(ToolResult(call_id=call, output=said, refused=refused,
                                      ok=not bool(getattr(b, "is_error", False)), turn_id=turn))
        return out

    def _settle(self, model_usage: Any, turn_usage: Any) -> list[Event]:
        """The calls of the turn, with their final figures.

        The CLI reports a call when its first block is complete: the input side is final,
        the OUTPUT is not (measured: 1 token announced for 5 in the transcript). The result
        of the turn carries exact totals — per model, cumulated over the CLI process
        (`model_usage`), and for the turn (`usage`). What the calls of a model lack against
        its exact total is added to its last call, as a revision of that call: the events
        of a turn then add up to what the transcript says, model by model."""
        calls, self._calls = self._calls, {}
        exact: dict[str, dict[str, int]] = {}
        if isinstance(model_usage, dict):
            for model, u in model_usage.items():
                if not isinstance(u, dict):
                    continue
                cur = {"input": int(u.get("inputTokens") or 0),
                       "output": int(u.get("outputTokens") or 0),
                       "cache_read": int(u.get("cacheReadInputTokens") or 0),
                       "cache_write": int(u.get("cacheCreationInputTokens") or 0)}
                prev = self._model_totals.get(model) or {}
                # a smaller total = a process restarted behind our back: it is the delta
                exact[model] = {k: v - prev.get(k, 0) if v >= prev.get(k, 0) else v
                                for k, v in cur.items()}
                self._model_totals[model] = cur
        models = {c.model for c in calls.values()}
        if not (models & set(exact)) and len(models) == 1 and isinstance(turn_usage, dict) \
                and turn_usage:
            # no per-model total under the name the calls carry: one model in the turn, so
            # the turn's own total is that model's
            t = _usage(turn_usage, turn_id=None, model=None, scope="turn")
            exact = {next(iter(models)): {"input": t.input, "output": t.output,
                                          "cache_read": t.cache_read,
                                          "cache_write": t.cache_write}}
        out: list[Event] = []
        for model, tot in exact.items():
            mine = [c for c in calls.values() if c.model == model]
            if not mine:
                continue
            more = {k: tot[k] - sum(getattr(c, k) for c in mine) for k in tot}
            if any(v > 0 for v in more.values()):
                last = mine[-1]
                out.append(dataclasses.replace(
                    last, revision=True,
                    **{k: getattr(last, k) + max(0, v) for k, v in more.items()}))
        return out

    # ---- the gate: the SDK's permission callback ---------------------------------------
    async def _can_use_tool(self, tool_name: str, input_data: dict, context: Any):
        gate = tool_gate(tool_name, input_data, turn_id=self._turn)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._gates[gate.gate_id] = fut
        self._mail.put(GateRequested(gate=gate))
        try:
            decision: Decision = await fut
        except asyncio.CancelledError:
            decision = Decision.deny("Request cancelled", via="engine")
        finally:
            self._gates.pop(gate.gate_id, None)
        self._mail.put(GateResolved(gate_id=gate.gate_id, decision=decision.kind,
                                    by=decision.by, via=decision.via,
                                    message=decision.message if decision.denied else ""))
        if decision.denied:
            return PermissionResultDeny(message=decision.message)
        if decision.kind == ANSWER:
            return PermissionResultAllow(updated_input={**input_data, "answers": decision.value})
        if decision.kind == EDIT and decision.input is not None:
            return PermissionResultAllow(updated_input=decision.input)
        return PermissionResultAllow(updated_input=input_data)


# ---- where the CLI runs: the session runner (local subprocess, container, pod) -----------

async def _reap(client: Any) -> None:
    """3.6.7 — make sure the CLI process of a closed client is gone. Seen on the end-to-end
    pass: a session whose client was closed from the sweep's task kept its CLI alive (parent
    1, ~0.3 GB) — the SDK's close raised before its terminate/kill escalation ran. SIGTERM,
    then SIGKILL after 3 s; a process already gone is fine."""
    import signal
    proc = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(proc, "pid", None)
    if not pid or getattr(proc, "returncode", None) is not None:
        return
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 1.0)):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                done = os.waitpid(pid, os.WNOHANG)[0] == pid
            except ChildProcessError:
                done = True
            if done:
                return
            await asyncio.sleep(0.1)


class RunnerTransport(Transport):
    """The SDK's Transport over a session runner (`backend/runner`).

    The SDK keeps doing everything it does for a local subprocess — initialize, control
    protocol (can_use_tool, hooks, interrupt, set_permission_mode), message parsing; this
    class only moves the stream-json lines to the session container's supervisor and back."""

    def __init__(self, runner, spec, on_close=None):
        self.runner = runner
        self.spec = spec
        self.handle = None
        self.chan = None
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


def build_argv(options: Any, cli: str = "claude") -> list[str]:
    """The CLI command line the SDK would run for these options (same flags, same
    `--permission-prompt-tool stdio` when can_use_tool is set)."""
    from claude_agent_sdk._internal.transport.subprocess_cli import (  # type: ignore
        SubprocessCLITransport)
    if getattr(options, "can_use_tool", None) and not options.permission_prompt_tool_name:
        options = dataclasses.replace(options, permission_prompt_tool_name="stdio")

    async def _empty():  # the SDK writes the prompt itself (streaming mode)
        return
        yield  # noqa: B901

    t = SubprocessCLITransport(prompt=_empty(), options=options)
    t._cli_path = cli
    return t._build_command()


def client_args(sid: str, opts_kwargs: dict, user: str = "", project: str = "default",
                kind: str = "session") -> tuple[Any, Any]:
    """(ClaudeAgentOptions, transport | None) for one run, on the instance's session
    runner: local = the options unchanged and no transport (the SDK spawns the CLI here);
    docker / kubernetes = the options rewritten for the session container (MCP servers
    through the relay) and a `RunnerTransport`."""
    import runner
    r = runner.get_runner()
    if not r.remote:
        return ClaudeAgentOptions(**opts_kwargs), None

    from runner import common, relay

    relay_token = common.token_for(sid, "relay")
    servers = opts_kwargs.get("mcp_servers") or {}
    relay.register(relay_token, sid, servers)
    remote_kwargs = dict(opts_kwargs)
    remote_kwargs["mcp_servers"] = {n: {"command": "sokkan-mcp-relay", "args": [n]}
                                    for n in servers}
    remote_kwargs.pop("env", None)  # goes to the container's Secret, filtered
    options = ClaudeAgentOptions(**remote_kwargs)
    spec = runner.SessionSpec(
        sid=sid, argv=build_argv(options, os.environ.get("SOKKAN_SESSION_CLI") or "claude"),
        cwd=str(opts_kwargs.get("cwd") or "/workspace"), project=project or "default",
        user=user, env=runner.session_env(opts_kwargs.get("env")),
        token=common.token_for(sid, "supervisor"), relay_token=relay_token, kind=kind)

    async def _before() -> None:
        await relay.ensure_started()
        await runner._reconcile_once(r)

    t = RunnerTransport(r, spec, on_close=lambda: relay.unregister(relay_token))
    t.before_connect = _before  # type: ignore[attr-defined]
    return options, t


def detect() -> tuple[bool, str]:
    """Is this engine usable here? (the SDK is importable — this module loaded — and a CLI
    is found: the one bundled with the SDK, or `claude` on the PATH)."""
    import shutil

    import claude_agent_sdk  # type: ignore
    bundled = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled", "claude")
    cli = os.environ.get("SOKKAN_SESSION_CLI") or "claude"
    if os.path.exists(bundled):
        return True, "CLI bundled with the SDK"
    if shutil.which(cli):
        return True, f"`{cli}` on the PATH"
    return False, "no Claude Code CLI found (neither bundled with the SDK nor on the PATH)"
