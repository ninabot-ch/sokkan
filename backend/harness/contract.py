"""The session contract: what an engine says to the cockpit, and what the cockpit answers.

Three parts, one file:

* **run** — the normalised event stream an adapter emits (``run.started`` … ``run.ended``).
  The cockpit consumes these events and nothing else: no type of an engine's SDK, CLI or
  HTTP API crosses ``backend/harness/``.
* **gate** — one verb, *ask before acting*: ``gate.requested`` → a :class:`Decision` →
  ``gate.resolved``. Who decides (rules, a person in the rail, a person in a chat card) is
  the cockpit's business; the engine only sees the decision.
* **context** — the memory recall of a turn: the cockpit produces the content
  (:class:`ContextProvider`), the adapter delivers it by the best path its engine has and
  says so with ``context.injected``.

Everything here is a plain dataclass that round-trips through ``to_dict`` /
``event_from_dict`` (JSON-safe as long as tool inputs and outputs are). No import of any
engine, and none of the cockpit: this module is the only thing both sides share.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields
from typing import Any, Awaitable, Callable, ClassVar, Mapping

CONTRACT_VERSION = 1

# ---- vocabulary ------------------------------------------------------------------------

# what a tool call does, as the adapter classifies it (``tool.called.classification``)
CLASSIFICATIONS = ("shell", "file_write", "file_read", "net", "mcp", "subagent", "other")

# what a gate is about (``gate.requested.kind``)
GATE_KINDS = ("tool", "shell", "file_write", "net", "mcp", "question", "plan", "budget")

# what can be answered to a gate
ALLOW_ONCE, ALLOW_SESSION, DENY, ANSWER, EDIT = (
    "allow_once", "allow_session", "deny", "answer", "edit")
DECISIONS = (ALLOW_ONCE, ALLOW_SESSION, DENY, ANSWER, EDIT)

# through what a decision came (``gate.resolved.via``)
VIAS = ("rail", "teams", "share", "auto_rule", "timeout", "engine")

# how the tokens of a ``usage`` event are billed, when the ENGINE knows (else None: the
# cockpit derives it from how the instance reaches the model)
BASES = ("api", "subscription", "gateway", "local")


class ContractError(ValueError):
    """An event, a gate or a decision that does not respect the contract."""


class Unsupported(RuntimeError):
    """The engine does not have this capability (see :class:`Capabilities`): the cockpit
    greys the command out, it never emulates it."""


# ---- gate ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate:
    """One request to act. ``subject`` is what is asked, shaped by ``kind``:

    * ``tool`` / ``shell`` / ``file_write`` / ``net`` / ``mcp`` / ``plan`` —
      ``{"tool": <engine tool name>, "input": {...}}``, plus the one field the cockpit's
      rules read without knowing the engine's tools: ``"command"`` for ``shell``,
      ``"path"`` for ``file_write``;
    * ``question`` — the same, with ``input["questions"]`` (text, options, multi-select);
    * ``budget`` — ``{"limit": …, "used": …, "unit": "usd" | "tokens"}``.

    ``options`` lists the decisions the ENGINE accepts for this gate (a gate born in the
    engine may not take ``edit``, a question only takes ``answer`` / ``deny``).
    """
    gate_id: str
    kind: str
    subject: dict = field(default_factory=dict)
    options: tuple[str, ...] = (ALLOW_ONCE, DENY)
    reason: str = ""
    turn_id: str | None = None
    expires: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in GATE_KINDS:
            raise ContractError(f"unknown gate kind {self.kind!r}")
        bad = [o for o in self.options if o not in DECISIONS]
        if bad:
            raise ContractError(f"unknown decision(s) in gate options: {bad}")

    @property
    def tool(self) -> str:
        return str(self.subject.get("tool") or "")

    @property
    def input(self) -> dict:
        v = self.subject.get("input")
        return v if isinstance(v, dict) else {}

    @property
    def path(self) -> str:
        """The file a ``file_write`` gate is about ('' otherwise)."""
        return str(self.subject.get("path") or "")

    def to_dict(self) -> dict:
        return {"gate_id": self.gate_id, "turn_id": self.turn_id, "kind": self.kind,
                "subject": self.subject, "options": list(self.options),
                "reason": self.reason, "expires": self.expires}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Gate":
        return cls(gate_id=d["gate_id"], kind=d["kind"], subject=dict(d.get("subject") or {}),
                   options=tuple(d.get("options") or (ALLOW_ONCE, DENY)),
                   reason=d.get("reason") or "", turn_id=d.get("turn_id"),
                   expires=d.get("expires"))


@dataclass(frozen=True)
class Decision:
    """The answer to a gate.

    * ``allow_once`` — run what was asked (``input``, when set, is the gate's own input);
    * ``allow_session`` — the same, and do not ask again for an identical request;
    * ``edit`` — run ``input`` INSTEAD of what was asked (a person corrected it, or the
      cockpit rewrote it: a command wrapped in a sandbox). Only for a gate that offers it;
    * ``answer`` — ``value`` is the answer to a question;
    * ``deny`` — ``message`` is what the engine tells its model.

    ``by`` / ``via`` say who decided and through what — the adapter echoes them in
    ``gate.resolved``."""
    kind: str
    input: dict | None = None
    value: Any = None
    message: str = ""
    by: str = ""
    via: str = ""

    def __post_init__(self) -> None:
        if self.kind not in DECISIONS:
            raise ContractError(f"unknown decision {self.kind!r}")

    @classmethod
    def allow(cls, input: dict | None = None, *, session: bool = False,  # noqa: A002
              by: str = "", via: str = "") -> "Decision":
        return cls(ALLOW_SESSION if session else ALLOW_ONCE, input=input, by=by, via=via)

    @classmethod
    def edit(cls, input: dict, *, by: str = "", via: str = "") -> "Decision":  # noqa: A002
        return cls(EDIT, input=input, by=by, via=via)

    @classmethod
    def answer(cls, value: Any, *, by: str = "", via: str = "") -> "Decision":
        return cls(ANSWER, value=value, by=by, via=via)

    @classmethod
    def deny(cls, message: str = "", *, by: str = "", via: str = "") -> "Decision":
        return cls(DENY, message=message, by=by, via=via)

    @property
    def allowed(self) -> bool:
        return self.kind != DENY

    @property
    def denied(self) -> bool:
        return self.kind == DENY

    def to_dict(self) -> dict:
        return {"kind": self.kind, "input": self.input, "value": self.value,
                "message": self.message, "by": self.by, "via": self.via}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Decision":
        return cls(kind=d["kind"], input=d.get("input"), value=d.get("value"),
                   message=d.get("message") or "", by=d.get("by") or "", via=d.get("via") or "")


# ---- capabilities ----------------------------------------------------------------------

@dataclass(frozen=True)
class Capabilities:
    """What an engine can do, declared by its adapter and shown by the cockpit. Honest
    parity: what is absent is greyed out, never emulated (conformance C7).

    ``gate_authority`` says where a gate is born:

    * ``"core"`` — the engine is started at its strictest and asks for everything it was
      not told to run without asking (``RunSpec.auto_allow``); the cockpit is the policy.
    * ``"engine"`` — the engine has its own policy and raises a gate only when that policy
      says so. The cockpit publishes and decides those gates and may only RESTRICT: a
      ``deny`` always holds, an ``allow`` is a request the engine can still refuse (the
      effective outcome is the ``decision`` of ``gate.resolved``), and what the engine
      refuses without asking never reaches the cockpit as a gate.
    """
    gate_kinds: tuple[str, ...] = ("tool",)
    decisions: tuple[str, ...] = (ALLOW_ONCE, DENY)
    gate_authority: str = "core"            # core | engine
    question: bool = False                  # questions with choices
    plan: bool = False                      # a plan submitted, then approved
    interrupt: bool = True                  # a turn can be stopped without ending the run
    thinking: bool = False                  # a reasoning stream, apart from the answer
    streaming: bool = False                 # assistant.text / assistant.think arrive as deltas
    subagents: bool = False                 # sub-agent tool calls are visible in the stream
    resume: bool = False                    # a run can be resumed after the process ended
    context: tuple[str, ...] = ()           # delivery paths: hook | preamble | mcp | native
    mcp_stdio: bool = False                 # launches RunSpec.mcp_servers (stdio) itself
    sandbox: str = "host"                   # who confines the tools: host | engine
    identity: str = "plain"                 # plain | opaque (see RunSpec)
    usage: str = "none"                     # events | transcripts | none
    budget: tuple[str, ...] = ()            # native spend caps: usd | tokens
    history: bool = False                   # the thread of a run can be read back (history())

    def to_dict(self) -> dict:
        return {f.name: (list(v) if isinstance(v := getattr(self, f.name), tuple) else v)
                for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Capabilities":
        known = {f.name for f in fields(cls)}
        return cls(**{k: (tuple(v) if isinstance(v, list) else v)
                      for k, v in d.items() if k in known})


# ---- context ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextRequest:
    """What an adapter asks the cockpit before a turn (``channel="prompt"``) or before a
    sub-agent starts (``channel="subagent"``, ``agent_id`` = the call that starts it)."""
    text: str
    channel: str = "prompt"
    turn_id: str | None = None
    agent_id: str | None = None
    title: str = ""             # a sub-agent's short description, when the engine has one


@dataclass(frozen=True)
class ContextBlock:
    """What the cockpit answers: the text to put in front of the model and the notes it
    comes from. The scope (project, clearance of the person) was applied by the cockpit:
    an adapter never widens it and never recalls on its own (conformance C8)."""
    text: str
    note_names: tuple[str, ...] = ()
    source: str = "memory"


# async (ContextRequest) -> ContextBlock | None ; never raises (a recall never breaks a turn)
ContextProvider = Callable[[ContextRequest], Awaitable["ContextBlock | None"]]


# ---- history ---------------------------------------------------------------------------

HISTORY_KINDS = ("prompt", "text", "think", "tool")


@dataclass(frozen=True)
class HistoryItem:
    """One entry of a run's thread as the ENGINE keeps it (``Harness.history``): what the
    cockpit shows when it reads a thread back — after a restart, in an export — whatever the
    engine and however it stores its sessions.

    * ``prompt`` — what was sent to the engine for a turn (``text``), as it was sent;
    * ``text`` / ``think`` — an answer / reasoning block of the model (``model``: the model
      that answered, when the engine knows it);
    * ``tool`` — one tool call: ``call_id``, ``name`` (the engine's tool name), ``input``,
      ``classification`` (the vocabulary of ``tool.called``), and its outcome when known:
      ``output`` (None = no result kept) and ``ok``.

    Nothing the engine adds for itself (its reminders, its command echoes, its own system
    lines) is an item: the adapter drops it."""
    kind: str
    text: str = ""
    model: str | None = None
    call_id: str | None = None
    name: str = ""
    input: dict = field(default_factory=dict)
    classification: str = "other"
    output: str | None = None
    ok: bool = True

    def __post_init__(self) -> None:
        if self.kind not in HISTORY_KINDS:
            raise ContractError(f"unknown history item kind {self.kind!r}")

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "HistoryItem":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---- run spec --------------------------------------------------------------------------

@dataclass(frozen=True)
class Confinement:
    """The project boundary the engine must enforce on its tools (file tools stay inside
    the project's space, the shell runs inside a sandbox or not at all). ``rules`` are the
    shell rules that may run without a gate once confined."""
    project: str
    rules: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()
    git_socket: str | None = None


@dataclass
class RunSpec:
    """Everything an engine needs to start one run. Built by the cockpit, read by the
    adapter.

    Identity: ``run_id``, ``user`` and ``project`` are the cockpit's own identifiers. An
    engine that declares ``Capabilities.identity == "opaque"`` (a remote engine that treats
    whatever it is handed as a secret, or logs it) must be given opaque handles instead —
    never a project name or an e-mail address; the cockpit keeps the mapping.
    """
    run_id: str
    cwd: str
    project: str = ""
    user: str = ""
    kind: str = "session"                   # session (a person is there) | run (unattended)
    model: str | None = None
    model_engine: str | None = None         # which configured model engine serves the run
    resume: str | None = None               # the engine session to resume
    plan: bool = False                      # start in plan mode (Capabilities.plan)
    env: dict[str, str] = field(default_factory=dict)       # for the run's tools; never shown
    secret_names: tuple[str, ...] = ()      # names of the env entries that hold secrets
    mcp_servers: dict[str, dict] = field(default_factory=dict)  # stdio: command, args, env
    auto_allow: tuple[str, ...] = ()        # tool rules that run without a gate
    deny_tools: tuple[str, ...] | None = None   # tools removed from the run
    # the cockpit's approval mode (default | acceptEdits | bypassPermissions): a cockpit rule,
    # applied when a gate is requested. An engine whose gate ENDS its turn (the kernel) is also
    # told, so that what the cockpit would approve anyway is not a card (one turn per card).
    # The cockpit only sends a wider mode when none of its « never » rules could fire.
    approval_mode: str = "default"
    # False = the run has no shell (the Atelier): an engine that can remove its shell tools
    # from the run does (the kernel: deny_tools); the cockpit's rule still refuses a shell gate
    shell: bool = True
    budget_usd: float | None = None         # native cap, when the engine prices the model
    budget_tokens: int | None = None
    context: ContextProvider | None = None
    confinement: Confinement | None = None
    options: dict[str, Any] = field(default_factory=dict)   # engine-specific, by engine id

    def describe(self) -> dict:
        """A JSON-safe view for logs and tests: no callable, no secret value."""
        return {
            "run_id": self.run_id, "cwd": self.cwd, "project": self.project,
            "user": self.user, "kind": self.kind, "model": self.model,
            "model_engine": self.model_engine, "resume": self.resume, "plan": self.plan,
            "env": sorted(self.env), "mcp_servers": sorted(self.mcp_servers),
            "auto_allow": list(self.auto_allow),
            "deny_tools": None if self.deny_tools is None else list(self.deny_tools),
            "approval_mode": self.approval_mode, "shell": self.shell,
            "budget_usd": self.budget_usd, "budget_tokens": self.budget_tokens,
            "context": self.context is not None,
            "confinement": self.confinement.project if self.confinement else None,
        }


# ---- events ----------------------------------------------------------------------------

_EVENTS: dict[str, type["Event"]] = {}


def _event(cls):
    cls = dataclass(frozen=True)(cls)
    _EVENTS[cls.type] = cls
    return cls


class Event:
    """Base of every event. ``type`` is the wire name (``assistant.text`` …)."""
    type: ClassVar[str] = ""
    # field name → wire name, when they differ (`in` is not a Python identifier)
    _wire: ClassVar[dict[str, str]] = {}

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"type": self.type}
        for f in fields(self):  # type: ignore[arg-type]
            v = getattr(self, f.name)
            if hasattr(v, "to_dict"):
                v = v.to_dict()
            elif isinstance(v, tuple):
                v = list(v)
            out[self._wire.get(f.name, f.name)] = v
        return out

    @classmethod
    def _from(cls, d: Mapping[str, Any]) -> "Event":
        back = {w: n for n, w in cls._wire.items()}
        kw = {}
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        for k, v in d.items():
            k = back.get(k, k)
            if k in names:
                kw[k] = tuple(v) if isinstance(v, list) and k in cls._tuples else v
        return cls(**kw)

    _tuples: ClassVar[tuple[str, ...]] = ()


@_event
class RunStarted(Event):
    """The engine is reached and the run exists. ``harness`` is the adapter's id,
    ``engine`` the model engine that serves it, ``sid`` the cockpit's session id."""
    type: ClassVar[str] = "run.started"
    run_id: str
    harness: str
    sid: str
    cwd: str
    engine: str | None = None
    model: str | None = None
    project: str = ""
    resumed_from: str | None = None


@_event
class RunInfo(Event):
    """What the engine learnt about the run after it started: its own session reference
    (what ``RunSpec.resume`` takes), the model that really answers (it can change within a
    run: an escalation). ``requested_model`` is what the engine says it was asked to use
    (an alias, before any answer); ``model`` is only ever the model an answer came from.
    Only the fields that are set have changed."""
    type: ClassVar[str] = "run.info"
    engine_session: str | None = None
    model: str | None = None
    requested_model: str | None = None


@_event
class TurnStarted(Event):
    type: ClassVar[str] = "turn.started"
    turn_id: str
    prompt_ref: str = ""        # a reference to the prompt (never its text)


@_event
class AssistantText(Event):
    type: ClassVar[str] = "assistant.text"
    turn_id: str
    text: str
    delta: bool = False         # True: a fragment to append; False: a whole block


@_event
class AssistantThink(Event):
    type: ClassVar[str] = "assistant.think"
    turn_id: str
    text: str
    delta: bool = False


@_event
class ToolCalled(Event):
    type: ClassVar[str] = "tool.called"
    turn_id: str
    call_id: str | None
    name: str
    input: dict = field(default_factory=dict)
    classification: str = "other"


@_event
class ToolResult(Event):
    type: ClassVar[str] = "tool.result"
    call_id: str | None
    output: str = ""
    ok: bool = True
    duration_ms: int | None = None
    turn_id: str | None = None


@_event
class GateRequested(Event):
    type: ClassVar[str] = "gate.requested"
    gate: Gate

    def to_dict(self) -> dict:
        return {"type": self.type, **self.gate.to_dict()}

    @classmethod
    def _from(cls, d: Mapping[str, Any]) -> "GateRequested":
        return cls(gate=Gate.from_dict(d))


@_event
class GateResolved(Event):
    """What was really applied. ``decision`` is the EFFECTIVE outcome: an engine that
    refuses what the cockpit allowed says ``deny`` here, with ``via="engine"``."""
    type: ClassVar[str] = "gate.resolved"
    gate_id: str
    decision: str
    by: str = ""
    via: str = ""
    message: str = ""


@_event
class ContextInjected(Event):
    type: ClassVar[str] = "context.injected"
    turn_id: str | None
    source: str                 # the delivery path: hook | preamble | mcp | native
    note_names: tuple[str, ...] = ()
    chars: int = 0
    channel: str = "prompt"     # prompt | subagent
    _tuples: ClassVar[tuple[str, ...]] = ("note_names",)


@_event
class Usage(Event):
    """Tokens, as the engine counts them; the cockpit prices them.

    ``scope="message"`` — one model call. A repeated ``message_id`` SUPERSEDES the earlier
    event of that id (engines repeat a message's usage while it grows): keep the largest of
    each field, never add. ``revision=True`` marks an event that only revises a call
    already reported, once its figures are final (an engine that first reports a call when
    it starts — the output is not known yet — settles it when the turn ends).
    ``scope="turn"`` — the engine's own total for the turn, for display; it is never added
    to the message events.
    ``cache_write_long`` is the part of ``cache_write`` written with the long lifetime
    (priced differently)."""
    type: ClassVar[str] = "usage"
    _wire: ClassVar[dict[str, str]] = {"input": "in", "output": "out"}
    turn_id: str | None
    model: str | None
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    basis: str | None = None
    message_id: str | None = None
    scope: str = "message"
    cache_write_long: int = 0
    revision: bool = False


@_event
class TurnEnded(Event):
    """The turn is over. ``reported`` is False when the engine ended it without a result
    of its own (the stream stopped, the call failed: an ``error`` event came before).
    ``cost_usd`` is what the ENGINE says this turn cost, when it prices its model;
    ``duration_ms`` how long it took by the engine's clock (the cockpit times the turn
    itself when it is absent)."""
    type: ClassVar[str] = "turn.ended"
    turn_id: str
    stop_reason: str = ""
    text: str = ""
    is_error: bool = False
    num_turns: int | None = None
    cost_usd: float | None = None
    reported: bool = True
    duration_ms: int | None = None   # how long the turn took, as the engine measured it


@_event
class RunEnded(Event):
    type: ClassVar[str] = "run.ended"
    status: str = "ended"       # ended | failed
    summary: str = ""
    resumable: bool = False
    engine_session: str | None = None


@_event
class Error(Event):
    """``recoverable``: the run goes on (a tool refused by the confinement, a failed
    call the model can work around); otherwise the turn is lost."""
    type: ClassVar[str] = "error"
    code: str
    message: str
    recoverable: bool = False
    data: dict = field(default_factory=dict)


EVENT_TYPES = tuple(_EVENTS)


def event_from_dict(d: Mapping[str, Any]) -> Event:
    try:
        cls = _EVENTS[d["type"]]
    except KeyError as e:
        raise ContractError(f"unknown event type {d.get('type')!r}") from e
    try:
        return cls._from({k: v for k, v in d.items() if k != "type"})
    except TypeError as e:
        raise ContractError(f"malformed {d['type']} event: {e}") from e


def prompt_ref(text: str) -> str:
    """A stable reference to a prompt for ``turn.started`` (the text itself stays out of
    the event stream: it is the person's, and it is already in the session's own log)."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]
