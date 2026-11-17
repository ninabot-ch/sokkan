"""Adapter 2: the kernel engine, a service next to the api, driven through its HTTP API.

The kernel (Nokker's agent kernel, consumed as a container image, never vendored here) runs
its own loop, its own tools, its own sandbox and its own gate. This adapter is a client of
its local API:

* ``POST /v1/local/sessions``           one kernel thread per run (``resume`` reopens one)
* ``POST /v1/chat/completions``         one prompt (``X-Session-Id: <thread>``)
* ``GET  /v1/events``                   SSE: ``turn.*``, ``tool.*``, ``approval.*``, ``question.*``
* ``POST /v1/local/approvals/{id}/decide``, ``POST /v1/local/questions/{id}/answer``
* ``POST /v1/threads/{id}/cancel``

What does not map one to one, and how it is bridged (docs: ``docs/sokkan/v4-lot1-kernel-design``
in the operator's notes; the short version is in ``harness/README.md``):

* **A gate ends the kernel's turn.** The kernel answers the gated call with « waiting for
  approval », lets its model finish the turn, and runs the tool AND a continuation turn once
  the card is decided. A cockpit turn therefore spans the kernel turn that raised the gate and
  every continuation turn that its decisions start: ``turn.ended`` is emitted when the chat
  call has returned, every kernel turn it caused is done, and no gate of the turn waits.
* **The kernel's first ``tool.result`` of a gated call is a placeholder** (« waiting »): it is
  not forwarded. The real result comes from the kernel's executor after an approval; after a
  refusal the kernel publishes none, so the adapter says the call failed.
* **Usage** comes per kernel turn and per model (``turn.cost.models``): one ``usage`` event per
  (kernel turn, model), ``message_id = <kernel turn>:<model>`` — an aggregate of the calls of
  that model in that kernel turn, never repeated.
* **Who decided** is echoed from the cockpit's decision: through its local API the kernel
  records every local decision as its machine owner.
* **Memory** is delivered as a preamble: the cockpit's recall (scope and clearance applied by
  the cockpit) is put in front of the prompt. The kernel's own long memory must stay off for a
  SOKKAN instance (C8).

Configuration (environment of the api): ``SOKKAN_KERNEL_URL`` (default
``http://kernel:8742``), the kernel's local token in ``SOKKAN_KERNEL_TOKEN`` or in the file
``SOKKAN_KERNEL_TOKEN_FILE`` (default ``/run/kernel/local-token``), ``SOKKAN_KERNEL_CWD_MAP``
(``api_path=kernel_path,...`` when the projects are mounted elsewhere in the kernel),
``SOKKAN_KERNEL_MCP_STDIO=1`` when the kernel launches the run's stdio MCP servers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("sokkan.harness.kernel")

from .base import Harness
from .contract import (
    ALLOW_ONCE, ALLOW_SESSION, ANSWER, DENY,
    AssistantText, AssistantThink, Capabilities, ContextInjected, ContextRequest, ContractError,
    Decision, Error, Event, Gate, GateRequested, GateResolved, RunEnded, RunInfo, RunSpec,
    RunStarted, ToolCalled, ToolResult, TurnEnded, TurnStarted, Unsupported, Usage, prompt_ref,
)

NAME = "kernel"
DEFAULT_URL = "http://kernel:8742"
DEFAULT_TOKEN_FILE = "/run/kernel/local-token"

# ---- the kernel's tool vocabulary → the contract's ---------------------------------------

_SHELL = {"terminal", "process_start", "process_stop", "ssh_run", "execute_code"}
_WRITE = {"write_file", "patch", "checkpoint_restore", "ssh_copy"}
_READ = {"read_file", "search_files", "checkpoint_list", "checkpoint_diff", "lsp_diagnostics"}
_SUBAGENT = {"delegate", "delegate_status", "delegate_cancel"}


def classify(name: str) -> str:
    n = name or ""
    if n in _SHELL:
        return "shell"
    if n in _WRITE:
        return "file_write"
    if n in _READ:
        return "file_read"
    if n in _SUBAGENT:
        return "subagent"
    if n.startswith("mcp_") or "__" in n:
        return "mcp"
    if n.startswith(("browser_", "web_")) or n in {"fetch_url", "web_search"}:
        return "net"
    return "other"


# the cockpit's approval mode → the kernel's own (its `qa/permissions.py`): the legacy engine
# decides a card INSIDE its call (`can_use_tool`), the kernel ends its turn on every card and
# starts a new one once it is decided (≈ 2 model requests per card, parity bench n°2). What
# the cockpit approves by rule anyway is therefore not made a card in the kernel.
#   default            every write and every command is a card (the Atelier, a person decides)
#   acceptEdits        the folder's file edits pass, commands stay cards
#   bypassPermissions  everything passes but the DESTRUCTIVE (still a card, approved by rule)
KERNEL_MODES = {"default": "default", "acceptEdits": "accept_edits", "bypassPermissions": "auto"}
_STRICTNESS = {"plan": 3, "default": 2, "accept_edits": 1, "auto": 0}

# the kernel publishes some refusals made BEFORE any execution (a `deny` rule, the plan mode,
# a « hardline » command…) with `is_error=False` and a stable `reason` (16.11.2026, its
# core/loop.py l. 2041; its hub sets `reason` for a refusal only): such a result is a refusal,
# never a tool that ran


def kernel_mode(approval_mode: str | None) -> str:
    return KERNEL_MODES.get(approval_mode or "default", "default")


def gate_kind(name: str) -> str:
    c = classify(name)
    return c if c in ("shell", "file_write", "net", "mcp") else "tool"


# messages of the kernel's file tools when a path leaves the session's folder (its refusal
# carries no stable code yet: see the design note, « what the kernel lacks »)
_OUTSIDE = ("outside", "hors du dossier", "hors de la racine", "hors du projet", "not under",
            "en dehors")


# an absolute path up to (and including) a segment that is a hex run the kernel would scrub
_HEX_DIR = re.compile(r"/(?:[^\s/«»\"'`<>]+/)*?(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])"
                      r"[0-9a-fA-F]{16,}(?![A-Za-z0-9])")


def _letters(run_id: str) -> str:
    """A short, stable folder name for a run, with no hex run in it."""
    h = hashlib.sha256((run_id or "").encode()).digest()
    return "run-" + "".join(chr(ord("g") + b % 20) for b in h[:8])


# ---- configuration -----------------------------------------------------------------------

def _url() -> str:
    return (os.environ.get("SOKKAN_KERNEL_URL") or DEFAULT_URL).rstrip("/")


def _token() -> str:
    tok = os.environ.get("SOKKAN_KERNEL_TOKEN") or ""
    if tok:
        return tok.strip()
    path = os.environ.get("SOKKAN_KERNEL_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _cwd_map(path: str) -> str:
    """The run's folder as the kernel sees it."""
    for pair in (os.environ.get("SOKKAN_KERNEL_CWD_MAP") or "").split(","):
        if "=" not in pair:
            continue
        src, dst = (p.strip().rstrip("/") for p in pair.split("=", 1))
        if src and (path == src or path.startswith(src + "/")):
            return dst + path[len(src):]
    return path


def _always_denied() -> list[str]:
    """Kernel tools a cockpit session never gets (``SOKKAN_KERNEL_DENY_TOOLS``, comma list).

    Default ``memory_note``: the kernel's own project-memory tool writes outside the cockpit's
    quarantine; a note of a conversation goes through SOKKAN's ``memory_write`` (MCP) instead.
    The kernel removes these from the catalogue of the thread and refuses them if called
    (``deny_tools``, kernel host-embed § 2). An empty value sends no list at all."""
    raw = os.environ.get("SOKKAN_KERNEL_DENY_TOOLS", "memory_note")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _mask(body: dict) -> dict:
    """The opening body as it may be kept (bench, logs): header and variable VALUES masked."""
    out = json.loads(json.dumps(body))
    for key in ("mcp_headers", "mcp_env"):
        v = out.get(key)
        if isinstance(v, dict):
            out[key] = {k: ({h: "***" for h in sub} if isinstance(sub, dict) else "***")
                        for k, sub in v.items()}
    return out


def _same_owner(path: str, like: str) -> None:
    """``path`` (a folder the adapter made inside the run's folder) owned and moded like
    ``like`` (the run's folder): the kernel's sandbox writes there under the run folder's
    owner — a folder of the api's uid would refuse a write AFTER its card was accepted
    (parity bench n°2, R1b: « mkdir …/.sokkan-kernel/run-…/archives: Permission denied »)."""
    try:
        st = os.stat(like)
    except OSError:
        return
    try:
        os.chmod(path, st.st_mode & 0o7777)
    except OSError:
        pass
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass       # not root: the api already runs as the folder's owner


def _mcp_headers(servers: dict[str, dict] | None) -> dict[str, dict[str, str]]:
    """Per-session headers for the cockpit's MCP servers reached over HTTP by the kernel
    (``mcp_headers``, kernel host-embed § 6; ``SOKKAN_KERNEL_MCP_HEADERS=1`` = the kernel's
    ``local.mcp_session_headers`` is on). Only servers declared with ``headers``."""
    if os.environ.get("SOKKAN_KERNEL_MCP_HEADERS") != "1":
        return {}
    return {name: {str(k): str(v) for k, v in (srv.get("headers") or {}).items()}
            for name, srv in (servers or {}).items() if srv.get("headers")}


SANDBOX_ROOT = "/workspace"


def _in_sandbox() -> bool:
    """``SOKKAN_KERNEL_SANDBOX=docker``: the kernel runs the run's tools in a sandbox container
    where the run's folder is ``/workspace`` (its file tools too): the model must be handed
    paths as the sandbox sees them, and the kernel's paths are mapped back for the cockpit."""
    return os.environ.get("SOKKAN_KERNEL_SANDBOX") == "docker"


def _headers(extra: dict | None = None) -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json",
            **(extra or {})}


_PROBE: dict[str, tuple[float, bool]] = {}


def _kernel_has_questions() -> bool:
    """Is the question-with-choices piece on in this kernel? (``GET /v1/local/questions``
    answers 200 when it is, 404 when the flag is off.) Cached one minute; False when the
    kernel is not reachable."""
    url = _url()
    hit = _PROBE.get(url)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    try:
        r = httpx.get(f"{url}/v1/local/questions", headers=_headers(), timeout=2.0)
        ok = r.status_code == 200
    except httpx.HTTPError:
        ok = False
    _PROBE[url] = (time.monotonic(), ok)
    return ok


def detect() -> tuple[bool, str]:
    """Is the kernel service reachable with a token? Never raises."""
    if not _token():
        return False, "no kernel token (SOKKAN_KERNEL_TOKEN or SOKKAN_KERNEL_TOKEN_FILE)"
    try:
        r = httpx.get(f"{_url()}/healthz", timeout=2.0)
        if r.status_code != 200:
            return False, f"kernel at {_url()} answered {r.status_code}"
        version = (r.json() or {}).get("version") or "?"
    except (httpx.HTTPError, ValueError) as e:
        return False, f"kernel at {_url()} not reachable ({type(e).__name__})"
    return True, f"kernel {version} at {_url()}"


# ---- the stream --------------------------------------------------------------------------

class _Stream:
    def __init__(self, h: "KernelHarness") -> None:
        self._h = h

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        h = self._h
        if not h._opened:
            await h._open()
        if h._finished:
            raise StopAsyncIteration
        ev = await h._mail.get()
        if isinstance(ev, RunEnded):
            h._finished = True
        return ev


class KernelHarness(Harness):
    name = NAME

    def __init__(self) -> None:
        self._spec: RunSpec | None = None
        self._mail: asyncio.Queue = asyncio.Queue()
        self._client: httpx.AsyncClient | None = None
        self._sse: asyncio.Task | None = None
        self._sse_ready: asyncio.Event | None = None
        self._last_event_id = 0
        self._thread = ""
        self._opened = self._finished = self._stopped = False
        # the cockpit turn in progress
        self._turn: str | None = None
        self._chat: asyncio.Task | None = None
        self._expect_done = 0           # kernel turns still to finish for this cockpit turn
        self._kturns: set[str] = set()  # kernel turns of this cockpit turn (started, not done)
        self._gates: dict[str, dict] = {}       # gate_id → state
        self._held_calls: dict[str, str] = {}   # call_id → gate_id (placeholder result held)
        self._last_text = ""
        self._turn_error: Error | None = None
        self._cancelled = False
        self._aliases: dict[str, str] = {}      # folder named by a hex id → its alias
        self._model: str | None = None
        self.opened_with: dict | None = None    # the opening body, values masked (benches)
        self.kernel_mode: str | None = None     # the mode the kernel applies to the thread
        # names of `deny_tools` the kernel did not match (`unknown_denied`, kernel host-3) that
        # were EXPECTED to be active tools: a refusal that would pass in silence
        self.deny_unmatched: list[str] = []
        self._removed: set[str] = set()         # tools removed from the run (deny_tools sent)

    # ---- capabilities ------------------------------------------------------------------
    def capabilities(self) -> Capabilities:
        question = _kernel_has_questions()
        kinds = ("tool", "shell", "file_write", "net", "mcp") + (("question",) if question else ())
        return Capabilities(
            gate_kinds=kinds,
            decisions=(ALLOW_ONCE, ALLOW_SESSION, DENY) + ((ANSWER,) if question else ()),
            gate_authority="engine", question=question, plan=False, interrupt=True,
            thinking=True, streaming=False, subagents=False, resume=True,
            context=("preamble",), mcp_stdio=os.environ.get("SOKKAN_KERNEL_MCP_STDIO") == "1",
            sandbox="engine", identity="opaque", usage="events", budget=("tokens", "usd"))

    # ---- Harness -----------------------------------------------------------------------
    def start(self, spec: RunSpec) -> AsyncIterator[Event]:
        if self._spec is not None:
            raise ContractError("start() called twice")
        if spec.plan:
            raise Unsupported("the kernel engine has no plan mode (a plan submitted, then approved)")
        self._spec = spec
        return _Stream(self)

    async def _open(self) -> None:
        """First iteration: reach the kernel, open (or reopen) its thread, then run.started."""
        self._opened = True
        spec = self._spec
        self._client = httpx.AsyncClient(base_url=_url(), timeout=httpx.Timeout(30.0, read=None))
        self._sse_ready = asyncio.Event()
        self._sse = asyncio.create_task(self._read_events())
        try:
            await asyncio.wait_for(self._sse_ready.wait(), 15)
        except asyncio.TimeoutError:
            await self._close_client()
            raise ConnectionError(f"kernel events not reachable at {_url()}") from None
        body: dict[str, Any] = {"cwd": _cwd_map(spec.cwd)}
        if spec.resume:
            # the kernel reopens an unknown thread as an empty one, without a word: a run
            # resumed from a session the kernel does not have is refused here (C4, honestly)
            t = await self._client.get(f"/v1/threads/{spec.resume}", headers=_headers())
            if t.status_code != 200:
                await self._close_client()
                raise ContractError(f"unknown engine session {spec.resume!r} "
                                    f"(the kernel answered {t.status_code})")
            body["resume"] = spec.resume
        budget = {}
        if spec.budget_tokens:
            budget["max_total_tokens"] = int(spec.budget_tokens)
        if spec.budget_usd:
            budget["max_cost_usd"] = float(spec.budget_usd)
        if budget:
            body["budget"] = budget
        # tools the run must not have: the spec's list (kernel names) plus the cockpit's own
        # « never » list; the kernel removes them from the thread and refuses them if called.
        # A kernel without the feature answers 400: the run does not start (never wider).
        # A run without a shell (the Atelier) loses every shell tool of the kernel: the rule of
        # the cockpit refuses a shell gate, but a gate never reached in the `default` mode (a
        # read-only command passes, parity bench n°2, R2) or a model that keeps trying costs
        # failure lines; removed, the tool is not even offered.
        no_shell = sorted(_SHELL) if not spec.shell else []
        denied = list(dict.fromkeys([*(spec.deny_tools or ()), *no_shell, *_always_denied()]))
        # the names that MUST match an active tool of the session (the others of the shell
        # family are removed in case this kernel has them)
        expected = set(spec.deny_tools or ()) | set(_always_denied()) | (
            {"terminal"} if not spec.shell else set())
        if denied:
            body["deny_tools"] = denied
            self._removed = set(denied)
        # the cockpit has no bubble surface: a kernel with `surfaces_gate` drops its bubble
        # tools from the thread (they would fail with « bubble not found »)
        body["surfaces"] = []
        headers_by_server = _mcp_headers(spec.mcp_servers)
        if headers_by_server:
            body["mcp_headers"] = headers_by_server
        mcp_env, mcp_off = {}, []
        if headers_by_server:
            mcp_off = sorted(set(spec.mcp_servers or {}) - set(headers_by_server))
        elif spec.mcp_servers and os.environ.get("SOKKAN_KERNEL_MCP_STDIO") == "1":
            # the kernel launches the servers declared in ITS configuration inside its
            # sandbox; the run's identity goes as their variables (opaque values only)
            for server in spec.mcp_servers.values():
                mcp_env.update({k: str(v) for k, v in (server.get("env") or {}).items()})
        else:
            # the kernel cannot reach the cockpit's servers with this run's identity (stdio
            # needs its docker sandbox; HTTP has no per-session headers yet): the run goes
            # without them, and says so — never with servers that would not know who asks
            mcp_off = sorted(spec.mcp_servers or {})
        if mcp_env:
            body["mcp_env"] = mcp_env
        self.opened_with = _mask(body)
        try:
            r = await self._client.post("/v1/local/sessions", headers=_headers(), json=body)
        except httpx.RemoteProtocolError:
            if "budget" not in body:
                raise
            # seen with the kernel on Postgres: setting a session cap drops the connection
            # (no tenant in the local route). The session opens without the ENGINE's cap —
            # said below, the cockpit's own budget checks still hold
            body.pop("budget")
            self.opened_with = _mask(body)
            r = await self._client.post("/v1/local/sessions", headers=_headers(), json=body)
        if r.status_code not in (200, 201):
            await self._close_client()
            raise ConnectionError(f"kernel refused the session ({r.status_code}): {r.text[:200]}")
        out = r.json()
        self._thread = out["thread_id"]
        # kernel host-3: `deny_tools` never answers 400, a name it did not match comes back in
        # `unknown_denied` (absent on older kernels: nothing to check). Naming: the name the
        # model sees, `<server>__<tool>` for an MCP tool.
        unknown = [str(n) for n in (out.get("unknown_denied") or [])]
        self.deny_unmatched = sorted(n for n in unknown if n in expected)
        # the kernel's mode follows the cockpit's approval mode (KERNEL_MODES): `default`, every
        # write and every command a card, unless the cockpit approves them by rule anyway
        try:
            await self._put_mode(kernel_mode(spec.approval_mode))
        except ConnectionError:
            await self._close_client()
            raise
        self._mail.put_nowait(RunStarted(run_id=spec.run_id, harness=NAME, sid=spec.run_id,
                                         cwd=spec.cwd, model=spec.model, project=spec.project,
                                         resumed_from=spec.resume))
        self._model = await self._default_model()
        self._mail.put_nowait(RunInfo(engine_session=self._thread, model=self._model))
        if self.deny_unmatched:
            log.warning("kernel thread %s: deny_tools not matched by the kernel: %s",
                        self._thread, ", ".join(self.deny_unmatched))
            self._mail.put_nowait(Error(code="deny_tools_unmatched", recoverable=True,
                                        message="tools to remove that the engine does not know: "
                                                + ", ".join(self.deny_unmatched),
                                        data={"tools": self.deny_unmatched}))
        if mcp_off:
            self._mail.put_nowait(Error(code="mcp_unavailable", recoverable=True,
                                        message="tools not available with this engine: "
                                                + ", ".join(mcp_off),
                                        data={"servers": mcp_off}))
        if budget and "budget" not in out:
            # the kernel ignores a cap when its flag is off: say it, never assume it holds
            self._mail.put_nowait(Error(code="budget_not_enforced", recoverable=True,
                                        message="the kernel does not enforce session caps"))

    async def _put_mode(self, mode: str) -> None:
        m = await self._client.put(f"/v1/local/sessions/{self._thread}/mode", headers=_headers(),
                                   json={"mode": mode})
        eff = (m.json() or {}).get("effective") if m.status_code == 200 else None
        # a stricter effective mode (the repository's settings restrict) is fine: the cockpit
        # still decides the cards; a WIDER one never is
        if eff not in _STRICTNESS or _STRICTNESS[eff] < _STRICTNESS[mode]:
            raise ConnectionError(f"kernel refused the mode {mode!r} ({m.status_code}): "
                                  f"{m.text[:200]}")
        self.kernel_mode = eff

    async def set_approval_mode(self, mode: str) -> None:
        """The cockpit's approval mode changed: the thread's kernel mode follows (from the next
        call of the model on). The cockpit only widens it when none of its « never » rules
        could fire (agentchat `_engine_approval_mode`)."""
        if self._spec is not None:
            self._spec.approval_mode = mode
        if self._client is not None and self._thread and not self._stopped:
            await self._put_mode(kernel_mode(mode))

    async def _default_model(self) -> str | None:
        """The model of the kernel's default tier, known before its first answer (the badge of
        a first save); `turn.cost` says later which model really answered."""
        try:
            r = await self._client.get("/v1/local/setup", headers=_headers())
            models = (r.json() or {}).get("models") or {} if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return None
        for entry in [models.get("router") or {}, *(models.get("byok") or []),
                      *(models.get("local") or [])]:
            m = entry.get("model") or next(iter(entry.get("models") or []), None)
            if m:
                return str(m)
        return None

    async def send(self, prompt: str) -> str:
        if not self._thread or self._stopped:
            raise ContractError("send() on a run that is not started")
        if self._turn is not None:
            raise ContractError("a turn is already running")
        turn = uuid.uuid4().hex[:12]
        self._turn, self._expect_done, self._kturns = turn, 1, set()
        self._last_text, self._turn_error, self._cancelled = "", None, False
        self._mail.put_nowait(TurnStarted(turn_id=turn, prompt_ref=prompt_ref(prompt)))
        text = prompt
        spec = self._spec
        if spec.context is not None:
            try:
                block = await spec.context(ContextRequest(text=prompt, channel="prompt",
                                                          turn_id=turn))
            except Exception:  # noqa: BLE001 — a recall never breaks a turn
                block = None
            if block is not None and block.text:
                text = block.text + "\n\n" + prompt
                self._mail.put_nowait(ContextInjected(
                    turn_id=turn, source="preamble", note_names=tuple(block.note_names),
                    chars=len(block.text)))
        text = self._alias_paths(text)
        if _in_sandbox():
            text = self._sandbox_paths(text)
        self._chat = asyncio.create_task(self._post_chat(turn, text))
        return turn

    def _sandbox_paths(self, text: str) -> str:
        """The run's folder named as the sandbox mounts it (``/workspace``)."""
        for root in sorted({os.path.realpath(self._spec.cwd), self._spec.cwd.rstrip("/"),
                            _cwd_map(self._spec.cwd).rstrip("/")}, key=len, reverse=True):
            text = text.replace(root + "/", SANDBOX_ROOT + "/")
            text = re.sub(re.escape(root) + r"(?![\w./-])", SANDBOX_ROOT, text)
        return text

    def _host_path(self, p: str) -> str:
        """A path the kernel reports, as the cockpit sees it."""
        if _in_sandbox() and (p == SANDBOX_ROOT or p.startswith(SANDBOX_ROOT + "/")):
            return os.path.join(self._spec.cwd, p[len(SANDBOX_ROOT):].lstrip("/"))
        return p if os.path.isabs(p) else os.path.join(self._spec.cwd, p)

    def _alias_paths(self, text: str) -> str:
        """The kernel scrubs every hexadecimal run of 16 characters or more from what its
        model reads (it may be a secret). The cockpit names folders by such ids (a document,
        a session's deliverables): the model would read a placeholder instead of the path and
        fail to open the file. Until the kernel lets a host declare its identifiers, each such
        folder INSIDE the run's folder is given an alias without hex (a symbolic link under
        `<cwd>/.sokkan-kernel/`), and the prompt names the alias. Nothing outside the run's
        folder is aliased: the kernel would refuse it anyway, and must."""
        cwd = os.path.realpath(self._spec.cwd)
        base = os.path.join(cwd, ".sokkan-kernel", _letters(self._spec.run_id))

        def sub(m: re.Match) -> str:
            real = m.group(0)
            if real in self._aliases:
                return self._aliases[real]
            target = os.path.realpath(real)
            if not os.path.isdir(target) or not (target + os.sep).startswith(cwd + os.sep):
                return real
            alias = os.path.join(base, f"f{len(self._aliases) + 1}")
            try:
                if not os.path.isdir(base):
                    os.makedirs(base, exist_ok=True)
                    for d in (os.path.dirname(base), base):
                        _same_owner(d, cwd)
                if not os.path.islink(alias):
                    # relative: the same link resolves on the host and in a sandbox where the
                    # run's folder is mounted elsewhere
                    os.symlink(os.path.relpath(target, os.path.dirname(alias)), alias)
            except OSError:
                return real
            self._aliases[real] = alias
            return alias

        return _HEX_DIR.sub(sub, text)

    async def _post_chat(self, turn: str, text: str) -> None:
        try:
            r = await self._client.post(
                "/v1/chat/completions", headers=_headers({"X-Session-Id": self._thread}),
                json={"messages": [{"role": "user", "content": text}]})
            out = r.json() if r.headers.get("content-type", "").startswith("application/json") \
                else {}
            err = (out.get("nokker") or {}).get("error") if isinstance(out, dict) else None
            if r.status_code >= 400:
                self._fail(f"http_{r.status_code}", str(out)[:300] or r.text[:300])
            elif err:
                self._fail(str(err.get("code") or "kernel_error"),
                           str(err.get("message") or ""))
                if str(err.get("code") or "").startswith("session_budget_"):
                    # refused before any model call: no kernel turn will report
                    self._expect_done = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(type(e).__name__, str(e))
            self._expect_done = 0
        finally:
            self._chat = None
            self._maybe_end()

    def _fail(self, code: str, message: str) -> None:
        if self._turn_error is None:
            self._turn_error = Error(code=code, message=message, recoverable=False)
            self._mail.put_nowait(self._turn_error)

    async def decide(self, gate_id: str, decision: Decision) -> bool:
        g = self._gates.get(gate_id)
        if g is None or g["resolved"]:
            return False
        g["resolved"] = True            # applied once: a second decision is refused
        effective = decision
        if decision.kind not in g["options"]:
            effective = Decision.deny(f"decision {decision.kind!r} is not offered for this "
                                      "request", by=decision.by, via="engine")
        try:
            if g["type"] == "question":
                cont = await self._answer(g, effective)
            else:
                cont = await self._decide_card(g, effective)
        except _Refused as e:
            g["resolved"] = False       # still pending in the kernel: another way may decide
            self._mail.put_nowait(Error(code=e.code, message=e.message, recoverable=True,
                                        data={"gate_id": gate_id}))
            return False
        if cont is None:                # the kernel no longer had it pending
            self._resolve(gate_id, Decision.deny("settled by the engine", via="engine"))
            return False
        if cont in ("running", "queued"):
            self._expect_done += 1
        self._resolve(gate_id, effective)
        return True

    async def _decide_card(self, g: dict, d: Decision) -> str | None:
        body: dict[str, Any] = {"decision": "reject" if d.denied else "approve", "scope": "once"}
        if d.kind == ALLOW_SESSION and g.get("rules"):
            body.update(scope="session", rule=g["rules"][0])
        # the real decider and rail, written by the kernel in its card's journal (kernel
        # host-embed § 3); its `decided_by` stays the local owner (the token that called)
        if d.by:
            body["on_behalf_of"] = str(d.by)[:200]
        if d.via:
            body["via"] = str(d.via)[:200]
        r = await self._client.post(f"/v1/local/approvals/{g['id']}/decide", headers=_headers(),
                                    json=body)
        if r.status_code == 403:
            info = (r.json() or {}).get("error") or {}
            raise _Refused(str(info.get("code") or "device_key_required"),
                           str(info.get("message") or "this card must be signed on a device"))
        if r.status_code in (404, 409):
            return None
        r.raise_for_status()
        return str((r.json() or {}).get("continuation") or "none")

    async def _answer(self, g: dict, d: Decision) -> str | None:
        if d.denied:
            body = {"text": d.message or "The person declined to answer."}
        else:
            value = d.value
            if isinstance(value, dict):
                value = next(iter(value.values()), "")
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            value = str(value or "")
            choice = next((o["id"] for o in g.get("choices") or []
                           if o.get("label", "").strip().lower() == value.strip().lower()), None)
            body = {"choice": choice} if choice else {"text": value}
        r = await self._client.post(f"/v1/local/questions/{g['id']}/answer", headers=_headers(),
                                    json=body)
        if r.status_code in (404, 409):
            return None
        r.raise_for_status()
        return str((r.json() or {}).get("continuation") or "running")

    def _resolve(self, gate_id: str, d: Decision) -> None:
        g = self._gates.get(gate_id)
        if g is None or g.get("announced") or g.get("held"):
            return
        g["resolved"] = True
        if g.get("kturn") and g["kturn"] in self._kturns:
            # the kernel turn that raised the gate is still writing: what it writes was
            # written BEFORE the decision (the kernel runs the tool after that turn), so the
            # decision is announced when that turn is done — never ahead of its text
            g["held"] = d
            return
        self._announce(gate_id, d)

    def _announce(self, gate_id: str, d: Decision) -> None:
        g = self._gates[gate_id]
        g["announced"], g["held"] = True, None
        self._mail.put_nowait(GateResolved(gate_id=gate_id, decision=d.kind, by=d.by, via=d.via,
                                           message=d.message if d.denied else ""))
        call = g.get("call_id")
        if d.denied and call and self._held_calls.pop(call, None):
            # the kernel publishes no result for a refused card: the call failed
            self._mail.put_nowait(ToolResult(call_id=call, ok=False, turn_id=g.get("turn_id"),
                                             output=d.message or "Refused."))
        self._maybe_end()

    async def interrupt(self) -> None:
        if self._turn is None or not self._thread:
            return
        try:
            r = await self._client.post(f"/v1/threads/{self._thread}/cancel", headers=_headers(),
                                        json={})
            running = bool((r.json() or {}).get("running"))
        except (httpx.HTTPError, ValueError):
            running = False
        self._cancelled = True
        for gid, g in list(self._gates.items()):
            if not g["resolved"]:
                self._resolve(gid, Decision.deny("Request cancelled", via="engine"))
        # queued continuations are dropped by the kernel; only a running turn still reports
        self._expect_done = 1 if running else 0
        self._maybe_end()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._opened and self._turn is not None:
            await self.interrupt()
            if self._turn is not None:      # nothing more will come: close the turn
                self._end_turn(force=True)
        self._mail.put_nowait(RunEnded(status="ended", resumable=bool(self._thread),
                                       engine_session=self._thread or None))
        await self._close_client()

    async def _close_client(self) -> None:
        if self._sse is not None:
            self._sse.cancel()
            try:
                await self._sse
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._sse = None
        if self._chat is not None:
            self._chat.cancel()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- end of a cockpit turn ---------------------------------------------------------
    def _maybe_end(self) -> None:
        if self._turn is None or self._chat is not None or self._expect_done > 0 or self._kturns:
            return
        # `announced`, not `resolved`: a decision being sent is not applied yet
        if any(not g.get("announced") for g in self._gates.values() if g.get("turn") == self._turn):
            return
        self._end_turn()

    def _end_turn(self, force: bool = False) -> None:
        turn, err = self._turn, self._turn_error
        self._turn = None
        stop = "interrupted" if self._cancelled else ("error" if err else "success")
        self._mail.put_nowait(TurnEnded(turn_id=turn, stop_reason=stop, text=self._last_text,
                                        is_error=err is not None,
                                        reported=not force and err is None))

    # ---- the kernel's events ------------------------------------------------------------
    async def _read_events(self) -> None:
        backoff = 0.5
        while True:
            headers = _headers({"Accept": "text/event-stream"})
            if self._last_event_id:
                headers["Last-Event-ID"] = str(self._last_event_id)
            try:
                async with self._client.stream("GET", "/v1/events", headers=headers) as r:
                    if r.status_code != 200:
                        raise httpx.HTTPStatusError("events", request=r.request, response=r)
                    self._sse_ready.set()
                    backoff = 0.5
                    name, data = "", []
                    async for line in r.aiter_lines():
                        if line.startswith("id:"):
                            try:
                                self._last_event_id = int(line[3:].strip())
                            except ValueError:
                                pass
                        elif line.startswith("event:"):
                            name = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line.strip() and name:
                            try:
                                self._on_event(name, json.loads("\n".join(data) or "{}"))
                            except Exception as e:  # noqa: BLE001 — one bad event never kills the run
                                self._mail.put_nowait(Error(code="kernel_event", recoverable=True,
                                                            message=f"{name}: {e}"))
                            name, data = "", []
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — reconnect, resuming from the last id
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    def _mine(self, d: dict) -> bool:
        th = str(d.get("thread_id") or "")
        return bool(self._thread) and th == self._thread

    def _on_event(self, name: str, d: dict) -> None:
        put = self._mail.put_nowait
        if name == "approval.requested":
            a = d.get("approval") or {}
            if a.get("thread_id") == self._thread and self._turn is not None:
                self._on_card(a)
            return
        if name == "approval.decided":
            gid = str(d.get("approval_id") or "")
            g = self._gates.get(gid)
            if g is not None and not g["resolved"]:
                # settled on the kernel's side (another client, its delay ran out)
                ok = str(d.get("decision") or "") in ("approve", "approved")
                if ok:
                    self._expect_done += 1
                self._resolve(gid, Decision.allow(by=str(d.get("by") or ""), via="engine")
                              if ok else Decision.deny("settled by the engine", via="engine"))
            return
        if name in ("question.asked", "question.answered"):
            if d.get("thread_id") != self._thread:
                return
            if name == "question.asked" and self._turn is not None:
                self._on_question(d)
            elif name == "question.answered":
                g = self._gates.get(str(d.get("question_id") or ""))
                if g is not None and not g["resolved"]:
                    self._expect_done += 1 if d.get("status") == "answered" else 0
                    self._resolve(g["id"], Decision.answer((d.get("answer") or {}).get("label"),
                                                           via="engine"))
            return
        if not self._mine(d) or self._turn is None:
            return
        kturn = str(d.get("turn_id") or "")
        if name == "turn.started":
            self._kturns.add(kturn)
        elif name == "tool.call":
            put(ToolCalled(turn_id=self._turn, call_id=d.get("call_id"), name=str(d.get("name")),
                           input=d.get("args") or {}, classification=classify(str(d.get("name")))))
        elif name == "tool.result":
            self._on_tool_result(d)
        elif name == "turn.reasoning":
            if d.get("text"):
                put(AssistantThink(turn_id=self._turn, text=str(d["text"])))
        elif name == "turn.delta":
            if d.get("text"):
                self._last_text = str(d["text"])
                put(AssistantText(turn_id=self._turn, text=self._last_text))
        elif name == "turn.cost":
            models = [str(m.get("model") or "") for m in d.get("models") or [] if m.get("model")]
            if models and models[-1] != self._model:
                # the model that really answered (the kernel may escalate): the cockpit's badge
                self._model = models[-1]
                put(RunInfo(model=self._model))
            for m in d.get("models") or []:
                # the kernel counts like an OpenAI upstream: its input INCLUDES the cached part;
                # the contract counts like the Messages API: `input` is the uncached part only
                cached = int(m.get("cached_tokens") or 0)
                put(Usage(turn_id=self._turn, model=str(m.get("model") or "") or None,
                          input=max(0, int(m.get("input_tokens") or 0) - cached),
                          output=int(m.get("output_tokens") or 0),
                          cache_read=cached,
                          message_id=f"{kturn}:{m.get('model')}"))
        elif name == "turn.done":
            err = d.get("error")
            if err and not d.get("cancelled"):
                self._fail(str(err.get("code") or "kernel_error"), str(err.get("message") or ""))
            self._kturns.discard(kturn)
            for gid, g in list(self._gates.items()):
                if g.get("held") and g.get("kturn") == kturn:
                    self._announce(gid, g["held"])
            self._expect_done = max(0, self._expect_done - 1)
            self._maybe_end()

    def _on_card(self, a: dict) -> None:
        gid = str(a["id"])
        if gid in self._gates:
            return
        tool, args = str(a.get("tool_name") or ""), a.get("arguments") or {}
        kind = gate_kind(tool)
        subject: dict[str, Any] = {"tool": tool, "input": args, "rule": a.get("rule"),
                                   "strength": a.get("strength"), "effect": a.get("effect")}
        if kind == "shell":
            subject["command"] = str(args.get("command") or "")
        elif kind == "file_write":
            p = str(args.get("path") or "")
            subject["path"] = self._host_path(p)
        rules = list(a.get("suggested_rules") or [])
        options = (ALLOW_ONCE, ALLOW_SESSION, DENY) if rules else (ALLOW_ONCE, DENY)
        call = a.get("call_id")
        self._gates[gid] = {"id": gid, "type": "card", "resolved": False, "options": options,
                            "rules": rules, "call_id": call, "turn": self._turn,
                            "turn_id": self._turn,
                            "kturn": f"{self._thread}:{a.get('turn_index')}"}
        if call:
            self._held_calls[call] = gid
        self._mail.put_nowait(GateRequested(gate=Gate(
            gate_id=gid, kind=kind, subject=subject, options=options,
            reason=str(a.get("reason") or ""), turn_id=self._turn)))

    def _on_question(self, q: dict) -> None:
        gid = str(q.get("question_id") or "")
        if not gid or gid in self._gates:
            return
        choices = list(q.get("options") or [])
        inp = {"questions": [{"question": q.get("question"), "header": str(q.get("question"))[:12],
                              "multiSelect": False,
                              "options": [{"label": o.get("label"), "description": o.get("label")}
                                          for o in choices]}]}
        self._gates[gid] = {"id": gid, "type": "question", "resolved": False,
                            "options": (ANSWER, DENY), "choices": choices, "turn": self._turn,
                            "call_id": None}
        self._mail.put_nowait(GateRequested(gate=Gate(
            gate_id=gid, kind="question", subject={"tool": "clarify", "input": inp},
            options=(ANSWER, DENY), turn_id=self._turn)))

    def _on_tool_result(self, d: dict) -> None:
        call = d.get("call_id")
        out = str(d.get("preview") or d.get("content") or "")
        if call in self._held_calls and not d.get("approval_id"):
            return                       # « waiting for approval »: not a result
        if d.get("approval_id"):
            self._held_calls.pop(call, None)
        # a refusal of the kernel's gate: `status: "refused"` (kernel host-2a, ed99c612; `ok`
        # and `is_error` unchanged there), or, on an older kernel, a `reason` on a result
        # published as a success — refused before running, never a tool that ran
        refused = str(d.get("status") or "") == "refused" or bool(d.get("reason"))
        ok = not d.get("is_error") and not refused
        name = str(d.get("name") or "")
        if refused and name in self._removed:
            # a tool the COCKPIT removed from the run: its refusal is a rule of the space,
            # said in the cockpit's words (harness/refusals.py), as the legacy engine's is
            from .refusals import removed
            out = removed(name, classify(name), out)
        if not ok and classify(str(d.get("name"))) == "file_write" and (
                d.get("reason") == "outside_root" or any(w in out.lower() for w in _OUTSIDE)):
            self._mail.put_nowait(Error(code="confined", recoverable=True, message=out[:300],
                                        data={"tool": d.get("name"), "shell_off": False}))
        self._mail.put_nowait(ToolResult(call_id=call, output=out, ok=ok, turn_id=self._turn,
                                         duration_ms=d.get("ms"), refused=refused))


class _Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message
