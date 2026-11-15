#!/usr/bin/env python3
"""agentchat.py — SOKKAN: the interactive chat session of the cockpit.

An `AgentSession` per sid is the CONSUMER of the session contract (`backend/harness`): it
starts the run on the instance's engine (`harness.registry`), reads its event stream, and
turns it into what the cockpit knows — the WebSocket flow of the session pane, the journal
of validations, the costs, the budgets. The interactive elements are structured events
rendered as web widgets:

  - tool permissions      → `gate.requested` → Allow / Deny buttons → `Harness.decide`
  - questions with choices → a gate of kind `question` → buttons; the answer goes back as
                              a `Decision.answer`
  - text / tools / thinking → `assistant.text` / `tool.called` / `assistant.think`

This module knows no engine: no SDK, no CLI, no HTTP API of an engine is imported here.
The engine SOKKAN has driven so far is one adapter of the contract (`harness.legacy`).

The flow sent to the front is unchanged by the contract: `_consume` translates each
contract event into the events the pane already reads (`text`, `tool_use`, `permission`,
`result`…). Outgoing events are broadcast to the subscribed WebSockets + buffered (replay
on refresh). See CHANTIER-B.md for the WS protocol.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import board  # persistance sid ↔ claude_session_id (resume après restart)
import budgets  # budgets par projet, jour / mois (3.2 lot 4)
import connectai  # 3.2 Connect your AI: engine of a Crew card / a project
import hitl_journal  # every approval asked and every decision, durable
from harness import contract, registry  # the session contract and the declared engines
import instance  # budgets de coût (hard stop HITL par session)
import memrecall  # memory recall of each turn and sub-agent: the run's context provider
import llm  # config LLM par instance (BYOK / inférence incluse)
import notify  # HITL push : ping si une permission traîne sans réponse
import study  # feature study_mode: consent gate, turn metadata (pseudonymised)
import vault  # coffre de secrets par instance → env des sessions (jamais au LLM)

CWD = os.environ.get("SOKKAN_AGENT_CWD") or (
    "/workspace" if os.path.isdir("/workspace") else os.getcwd())
# serveurs MCP SOKKAN injectés dans chaque session (mémoire RAG + board) —
# indépendants du .mcp.json du workspace, donc le moat marche out-of-the-box
_HERE = os.path.dirname(os.path.abspath(__file__))
_MEM_SRV = os.path.join(_HERE, "..", "memory", "memory_search_server.py")
_BOARD_SRV = os.path.join(_HERE, "board_mcp.py")
_OBS_SRV = os.path.join(_HERE, "observability_mcp.py")
_AGENTS_SRV = os.path.join(_HERE, "agents_mcp.py")
_PY = os.environ.get("SOKKAN_PYTHON", sys.executable)


def session_project(sid: str) -> str:
    """Project of a session for its memory scope (3.2). The value handed to the MCP server
    is a single project; an unknown session maps to the default project only while the
    instance has one project — otherwise to "" (an invalid slug = empty scope = nothing)."""
    import projects
    try:
        p = board.get_session_project(sid)
        if p is None:
            return "" if projects.multi_project() else projects.DEFAULT_PROJECT
        return p if projects.recall_scope(p) else ""
    except Exception as e:  # noqa: BLE001 — fail-closed: no project, no recall
        print(f"[sokkan] session project of {sid} unknown ({e!r}): no memory scope",
              file=sys.stderr)
        return ""


def project_cwd(sid: str) -> str:
    """Working directory of a session (3.2 lot 3): the instance's workspace for the default
    project (unchanged); for another project its own workspace
    $SOKKAN_DATA_DIR/projects/<slug>/work — so Claude Code does not load the default
    project's CLAUDE.md / MEMORY.md / .mcp.json into it. (The boundary itself is the
    sandbox of lot 8 (sandbox.py: tool hook + bubblewrap), when the feature is on.)"""
    p = session_project(sid)
    if p == "default":
        return CWD
    # no project (unknown session on a multi-project instance): an empty neutral workspace,
    # never the default project's
    d = Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
             ) / "projects" / (p or "_no-project") / "work"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return CWD
    return str(d)


def setting_sources_for(project: str) -> list[str]:
    """Which Claude Code settings a session loads (3.4.5). The default project keeps the
    host's user settings (hooks, allow rules, CLAUDE.md of ~/.claude: the operator's own
    workspace). Another project never does: on sokkan.ninabot.ch the host's user hook
    injected the default project's notes into every session of another project, and the
    host's allow rules would auto-approve tools there. The CLI login itself is not a setting
    (credentials stay in CLAUDE_CONFIG_DIR), so sessions still start on it."""
    if project == "default":
        return ["user", "project", "local"]
    return ["project", "local"]


def session_scope_env(project: str, user: str = "") -> str:
    """Comma list handed to the MCP servers: the project + shared (lot 3), or "" = none.
    3.4: each entry carries the clearance of the person the session acts for."""
    import classification
    return ",".join(classification.scope_for_email(user, project)) if project else ""
MCP_SERVERS = {
    "sokkan-memory": {"command": _PY, "args": [os.path.abspath(_MEM_SRV)]},
    "sokkan-board": {"command": _PY, "args": [os.path.abspath(_BOARD_SRV)]},
    # opérer la prod : lire métriques/logs, composer des dashboards
    "sokkan-observability": {"command": _PY, "args": [os.path.abspath(_OBS_SRV)]},
    # 3.1 « Crew up » : créer / piloter des agents depuis une session (HITL)
    "sokkan-agents": {"command": _PY, "args": [os.path.abspath(_AGENTS_SRV)]},
}


def mcp_servers_for(sid: str, user: str = "", only: list[str] | None = None,
                    agent_run: bool | dict = False, project: str | None = None) -> dict:
    """MCP servers of ONE session: same commands, plus who is calling (the API
    sets it, the model cannot) so a server can attribute and gate its writes.
    `only` restricts the set (agent runs get the servers their agent lists).
    `project` (3.2) scopes the memory server to the session's project; None = the
    session's stored project (board), resolved here."""
    if project is None:
        project = session_project(sid)
    who = {"SOKKAN_SESSION_ID": sid, "SOKKAN_SESSION_USER": user or "",
           "SOKKAN_SESSION_PROJECT": project,
           # what the session may READ: its project + shared (writes: its project only)
           "SOKKAN_SESSION_SCOPE": session_scope_env(project, user)}
    if agent_run:
        who["SOKKAN_AGENT_RUN"] = "1"  # agents MCP read-only, memory writes quarantined
        if isinstance(agent_run, dict):
            who["SOKKAN_AGENT_NAME"] = str(agent_run.get("agent") or "")
            who["SOKKAN_AGENT_RUN_ID"] = str(agent_run.get("run") or "")
    out = {}
    for name, cfg in MCP_SERVERS.items():
        if only is not None and name not in only:
            continue
        out[name] = {**cfg, "env": {**cfg.get("env", {}), **who}}
    return out
MODEL = os.environ.get("SOKKAN_AGENT_MODEL") or None  # None → défaut du CLI
# lectures auto-approuvées (UX fluide) ; tout le reste passe par les boutons.
# Les outils MCP SOKKAN en lecture (mémoire RAG, board) sont sûrs → le seed
# « check ta mémoire » ne bloque pas sur une permission avant qu'on ouvre le pane.
SAFE_TOOLS = [
    "Read", "Glob", "Grep", "TodoWrite", "NotebookRead",
    "mcp__sokkan-memory__memory_search", "mcp__sokkan-memory__memory_get",
    "mcp__sokkan-memory__memory_links",
    "mcp__sokkan-board__list_tags", "mcp__sokkan-board__list_board",
    "mcp__sokkan-board__get_card", "mcp__sokkan-board__search_cards",
    "mcp__sokkan-board__get_card_tree", "mcp__sokkan-board__morning_brief",  # 3.3 Helm
    # observabilité en LECTURE : diagnostiquer sans gate ; create_dashboard
    # (écriture) reste soumis à permission.
    "mcp__sokkan-observability__query_metrics", "mcp__sokkan-observability__query_logs",
    "mcp__sokkan-observability__list_dashboards",
    # 3.5 Operate › Alerts in READ (proposing a rule goes through the gate, then a person)
    "mcp__sokkan-observability__alerting_list_rules",
    "mcp__sokkan-observability__alerting_list_alerts",
    "mcp__sokkan-observability__alerting_preview",
    # agents (3.1) en LECTURE ; créer/modifier/lancer passe par le gate puis,
    # pour une création, par l'approbation humaine dans l'onglet Crew
    "mcp__sokkan-agents__list_agents", "mcp__sokkan-agents__get_agent",
    "mcp__sokkan-agents__list_runs", "mcp__sokkan-agents__get_run",
]
# modes de permission pilotables depuis le cockpit (équivalent web du Shift+Tab du TUI)
VALID_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}
# outils intégrés de Claude Code qu'un run d'agent n'a QUE s'ils sont dans sa liste
_BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
                  "WebFetch", "WebSearch", "Task", "Agent", "Skill"]
# outils traités comme « édition de fichier » par le mode acceptEdits
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
RING_MAX = 500  # events bufferisés par session pour le replay au reconnect

# titre court d'une carte d'outil (aligné sur transcript.py)
_TOOL_TITLE_FIELD = {
    "Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
    "NotebookEdit": "file_path", "Glob": "pattern", "Grep": "pattern",
    "Task": "description", "Agent": "description", "WebFetch": "url",
    "WebSearch": "query", "Skill": "skill",
    "mcp__sokkan-memory__memory_write": "name",
    "mcp__sokkan-memory__memory_get": "note_name",
    "mcp__sokkan-memory__memory_search": "query",
    # the kernel engine's tools (harness/kernel.py)
    "terminal": "command", "read_file": "path", "write_file": "path", "patch": "path",
    "search_files": "pattern", "delegate": "goal",
}


def _tool_title(name: str, inp: dict) -> str:
    field = _TOOL_TITLE_FIELD.get(name)
    val = inp.get(field) if field else None
    if isinstance(val, str) and val.strip():
        if name == "Bash" and "/sandbox/sessions/" in val:
            import sandbox  # lot 8: show what the model asked, not the wrapper
            val = sandbox.display_command(val) or val
        return val.strip().splitlines()[0][:200]
    return name


class AgentSession:
    """One chat session: a run on an engine (`harness.Harness`), consumed and broadcast."""

    def __init__(self, sid: str, cwd: str = CWD, resume: str | None = None,
                 model: str | None = MODEL, user: str = "", policy: dict | None = None,
                 secrets: list[str] | None = None):
        self.sid = sid
        self.cwd = cwd
        self.resume = resume
        self.model = model
        self.user = user  # email du spawneur → attribution per-user du metering (mode géré)
        # the run on the engine: None until the first turn (or `attach`)
        self.harness: Any = None
        self._stream: Any = None
        self._gate_tasks: set[asyncio.Task] = set()
        # gates requested by the engine and not resolved yet: an engine whose gate ends its
        # turn (the kernel) writes to the person BEFORE the decision — that text is a note
        self._open_gates: set[str] = set()
        self._turn_usage: contract.Usage | None = None      # the engine's own turn total
        # the model calls of the running turn, final figures ({call key: usage event}), and
        # when it started: the turn's record in the ledger (usage.turn_record) is built
        # from them at `turn.ended`; the last record is what the study journal reads
        self._turn_calls: dict[str, contract.Usage] = {}
        self._turn_started: float | None = None
        self.last_turn: dict | None = None
        self.context_log: list[dict] = []     # what was injected in this session (names, size)
        # the engine's own reference to the run (persisted: resume after a restart)
        self.claude_session_id: str | None = None
        self.events: list[dict] = []          # ring buffer (replay au reconnect)
        self.subscribers: set[asyncio.Queue] = set()
        self._perms: dict[str, asyncio.Future] = {}
        self._questions: dict[str, asyncio.Future] = {}
        # the gates waiting for a person here, as the contract gave them (id → gate, the
        # input the rules let through): what a reviewer, Teams or the rail decide on
        self._pending: dict[str, tuple[contract.Gate, dict]] = {}
        self._notify_tasks: set[asyncio.Task] = set()  # HITL push différé
        self._busy = False
        self._start_lock = asyncio.Lock()
        self.cost_usd = 0.0          # estimated cumulated cost (what each turn.ended reports)
        self._budget_warned = False  # avertissement 80 % émis une seule fois
        self._project_budget_warned = False  # idem pour le budget du projet (lot 4)
        self._model_seen: str | None = None
        # 3.6.5: the model THE API ANSWERED WITH on the last assistant message (the badge of
        # each answer, of each save, of each validation) — not the alias the CLI was given
        self._turn_model: str | None = None
        self.mode = "default"  # default | acceptEdits | bypassPermissions | plan
        # agent run (3.1) : politique d'outils/secrets/budget de l'agent, None pour
        # une session humaine (comportement inchangé). Clés : tools (noms de base
        # autorisés), auto_approve (règles Claude Code), secrets (noms du coffre),
        # budget_usd, mcp (serveurs), on_wait(bool) (une approbation attend ou non)
        self.policy = policy
        # secrets nommés à l'ouverture (SOKKAN_SESSION_SECRETS=named) ; None = relire le store
        self.secrets = secrets
        self.tokens_in = 0
        self.tokens_out = 0
        self.num_turns = 0
        self.last_result: dict | None = None
        # 3.1.2 : valeurs des secrets à masquer dans TOUT ce que la session émet (events
        # live, replay) — ceux de l'agent pour un run ; posé par get_or_create pour une
        # session de run rouverte depuis History.
        self.redact_values: dict[str, str] = (
            vault.session_env(list(policy.get("secrets") or []),
                              project=policy.get("project") or "default") if policy else {})
        # 3.1.2 : comptage SOKKAN d'un run sur un modèle non-Claude (agentcost.Meter)
        self.meter = (policy or {}).get("meter")
        self.budget_stop: str | None = None
        # 3.2 lot 8: project whose sandbox confines this session (None = not confined)
        self.sandboxed: str | None = None
        self._bash_off_noted = False
        self._refusal_said: str | None = None
        # 3.6.7: an idle session's CLI process is closed after SOKKAN_SESSION_IDLE_CLOSE_S
        # (≈ 0.3-0.4 GB each, nothing closed them) and started again, resumed, at the next turn
        self.last_active = time.monotonic()
        self.parked = 0               # how many times its CLI was closed for idleness
        self._sandbox_said = False

    # ---- diffusion ----------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _emit(self, event: dict) -> None:
        if self.redact_values:
            import agents  # pure module (no SDK), imported late to keep this one light
            event = agents.redact_obj(event, self.redact_values)
        self.last_active = time.monotonic()
        self.events.append(event)
        if len(self.events) > RING_MAX:
            del self.events[: len(self.events) - RING_MAX]
        for q in list(self.subscribers):
            q.put_nowait(event)

    def _sandbox_bash_off(self) -> None:
        """Bash refused by a hooks-only sandbox: one visible note per session (the model
        gets the same text as the hook's reason, on every attempt)."""
        if self._bash_off_noted:
            return
        self._bash_off_noted = True
        import sandbox
        self._emit({"type": "sandbox", "mode": sandbox.HOOKS, "message": sandbox.BASH_OFF,
                    "denied": "Bash"})

    # ---- the run on the engine ------------------------------------------------
    def attach(self, harness, spec: contract.RunSpec | None = None) -> None:
        """Plug a harness that is already built (a scripted engine, a test): the session
        consumes its stream as it would the instance's engine."""
        self.harness = harness
        self._stream = harness.start(spec)
        self._announce_capabilities()

    def _announce_capabilities(self) -> None:
        """C7: what the engine cannot do is greyed out in the pane, never emulated. Said
        only when something the pane offers is absent (today's engine has everything: the
        flow it sends is unchanged)."""
        try:
            caps = self.harness.capabilities()
        except Exception:  # noqa: BLE001
            return
        if not (caps.plan and caps.question):
            self._emit({"type": "capabilities", "plan": bool(caps.plan),
                        "question": bool(caps.question)})

    async def _run_spec(self) -> contract.RunSpec:
        """What the engine needs to start this session's run: where, for whom, with which
        tools, secrets, memory and boundary. Everything here is the cockpit's decision; how
        it is enforced is the adapter's."""
        pol = self.policy
        proj = session_project(self.sid)
        mcp = mcp_servers_for(self.sid, self.user,
                              only=pol.get("mcp") if pol else None,
                              agent_run=({"agent": pol.get("agent"), "run": pol.get("run")}
                                         if pol else False))
        # memory recall at every turn + for every sub-agent (3.0, P0-3)
        recall = memrecall.provider(self.sid, projects=self._recall_scope())
        # per-instance model configuration (BYOK / managed inference) + the vault (the
        # person operates their production: $STRIPE_KEY & co in the shells, the value never
        # read by the UI nor by the model), injected per session
        secret_env = vault.session_env(self._secret_names(), project=proj)
        if secret_env:  # 3.4.4: what the env holds is masked in everything emitted
            self.redact_values = {**self.redact_values, **secret_env}
        env_extra = {**secret_env, **llm.session_env(self.user)}
        # 3.6.5: a work started with « Write with Claude » — tier, project and model on
        # every call of its session (the router decides again, claudetier)
        import claudetier
        claude_env = claudetier.session_env(self.sid, proj, env_extra)
        env_extra.update(claude_env)
        # 3.2 connect_ai: a Crew card's engine (model `engine:<id>`) or the project's
        eng_env, eng_model = connectai.session_overrides(self.sid, self.model)
        env_extra.update(eng_env)
        # 3.2 lot 5: a forge project's session pushes with the PERSON's token, through
        # a credential helper — the env holds a ticket, never the token (forge.gitcred)
        # — reached through the runner relay (session container / pod) or, for the shell
        # inside bubblewrap, a per-session Unix socket bound into the sandbox
        import runner
        import sandbox
        forge_socket = None
        if runner.get_runner().remote:
            transport = "relay"
        elif sandbox.applies(proj) and sandbox.mode() == sandbox.BWRAP:
            transport = "socket"
        else:
            transport = "loopback"
        forge_env = self._forge_env(transport)
        if forge_env and transport == "socket":
            from forge import gitcred
            forge_socket = await gitcred.open_socket(self.sid)
        env_extra.update(forge_env)
        # 3.2 lot 8: a session of another project reaches only its project's space — the
        # engine enforces the boundary (file tools checked before any allow rule, the shell
        # inside bubblewrap or refused); no extra directory is handed to it
        confinement = None
        if sandbox.applies(proj):
            confinement = contract.Confinement(
                project=proj, rules=tuple((pol or {}).get("auto_approve") or []),
                env_names=tuple(sorted({*secret_env, *forge_env})), git_socket=forge_socket)
            self.sandboxed = proj
            if sandbox.mode() == sandbox.HOOKS and not self._sandbox_said:
                # say it up front in the pane (banner), not only when the shell is refused —
                # once: an engine started again after idleness (3.6.7) does not repeat it
                self._sandbox_said = True
                self._emit({"type": "sandbox", "mode": sandbox.HOOKS,
                            "message": sandbox.BASH_OFF})
        model = eng_model if eng_model is not None else (self.model or llm.session_model())
        legacy_opts: dict[str, Any] = {"setting_sources": setting_sources_for(proj)}
        if claude_env.get("ANTHROPIC_MODEL"):
            # the Claude model the person chose: the engine shapes its requests for it (the
            # router checks it is in the instance's list before anything leaves)
            model = claude_env["ANTHROPIC_MODEL"]
            # the API refuses the current Claude models to an older CLI (« Claude Code
            # 2.1.259 does not support this model; 2.1.280 or newer is required », seen on
            # 12.11.2026): the SDK runs its OWN bundled CLI unless told otherwise — a Claude
            # work runs the CLI pinned in the image (docker/api.Dockerfile)
            cli = claudetier.pinned_cli()
            if cli:
                legacy_opts["cli_path"] = cli
        return contract.RunSpec(
            run_id=self.sid, cwd=self.cwd, project=proj, user=self.user,
            kind="run" if pol else "session", model=model or None,
            resume=self.resume or None,
            # bypass / acceptEdits are rules of the cockpit (`gate`); only `plan` is
            # engaged in the engine (it changes what the model does)
            plan=self.mode == "plan",
            env=env_extra, secret_names=tuple(sorted(secret_env)), mcp_servers=mcp,
            auto_allow=tuple(self._allowed_tools()),
            # defence in depth for an agent run: tools outside its list are removed
            deny_tools=tuple(self._disallowed_tools()) if pol else None,
            budget_usd=self._sdk_max_budget(), context=recall, confinement=confinement,
            options={"legacy": legacy_opts})

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._stream is not None:
                return
            spec = await self._run_spec()
            self.harness = registry.create()
            if self.harness.capabilities().identity == "opaque":
                # who and what the run is for never reaches such an engine: handles only —
                # in its spec, its working folder and its tools' environment
                from harness import identity
                spec = identity.opaque_spec(spec, rewrite=_opaque_env_rewrite)
            self._stream = self.harness.start(spec)
            self._announce_capabilities()
            # the first event reaches the engine: `run.started`, or the engine's own error
            self._consume(await self._stream.__anext__())

    def parkable(self, idle_s: float, now: float | None = None) -> bool:
        """Its CLI can be closed without anyone noticing: started, no turn running, no
        approval nor question waiting for a person, nothing emitted for `idle_s` seconds."""
        now = time.monotonic() if now is None else now
        return (self._stream is not None and not self._busy and not self._perms
                and not self._questions and now - self.last_active >= idle_s)

    async def park(self, idle_s: float = 0.0) -> bool:
        """3.6.7 — close the CLI process of an idle session and keep everything else: the
        events (the thread is replayed as before), the mode, the person, the policy. The next
        turn starts the CLI again on the same conversation (`resume` = its Claude session),
        so the person sees no difference but a few seconds at the next answer. Under the
        start lock: a turn that begins meanwhile waits, then finds the CLI closed and starts
        it again. Returns True when the CLI was closed."""
        async with self._start_lock:
            if not self.parkable(idle_s):
                return False
            self.resume = self.claude_session_id or self.resume
            # 4.0: the engine closes its own process (the legacy adapter reaps a CLI its
            # client's close left running, 3.6.7); its stream is dropped, the next turn
            # starts the engine again with `resume`
            harness, self.harness, self._stream = self.harness, None, None
            if harness is not None:
                try:
                    await harness.stop()
                except Exception:  # noqa: BLE001 — an engine already gone is closed too
                    pass
            try:
                from forge import gitcred
                await gitcred.close_socket(self.sid)
            except Exception:  # noqa: BLE001
                pass
            self.parked += 1
            return True

    async def close(self) -> None:
        if getattr(self, "_run_journal", None) is not None:
            self._run_journal.flush()
        for fut in list(self._perms.values()) + list(self._questions.values()):
            if not fut.done():
                fut.cancel()
        harness, self.harness, self._stream = self.harness, None, None
        if harness is not None:
            try:
                await harness.stop()
            except Exception:  # noqa: BLE001
                pass
        for t in list(self._gate_tasks):
            t.cancel()
        try:
            from forge import gitcred
            await gitcred.close_socket(self.sid)
        except Exception:  # noqa: BLE001
            pass

    def _forge_env(self, transport: str = "loopback") -> dict[str, str]:
        try:
            import sandbox
            from forge import gitcred
            return gitcred.session_env(self.sid, self.user, session_project(self.sid),
                                       transport=transport,
                                       network=sandbox.network_allowed())
        except Exception as e:  # noqa: BLE001 — no credentials = the push fails, says so
            print(f"[sokkan] forge credentials of {self.sid} unavailable ({type(e).__name__})",
                  file=sys.stderr)
            return {}

    def _recall_scope(self) -> tuple[str, ...]:
        """Memory scope of this session (3.2): its project + shared (see session_project)."""
        import classification
        p = session_project(self.sid)
        if not p:
            return ()
        import claudetier
        return claudetier.cap_scope(self.sid, classification.scope_for_email(self.user, p))

    def _secret_names(self) -> list[str] | None:
        """Quels secrets du coffre vont dans l'env : ceux de l'agent pour un run ;
        pour une session humaine, tout (mode `all`) ou ceux choisis à son ouverture
        (mode `named`, rien si rien n'a été choisi)."""
        # 3.2 lot 4: the names are looked up in the session's project vault only
        # (vault.session_env(…, project=)); a project without a vault gets nothing
        if self.policy:
            return list(self.policy.get("secrets") or [])
        if vault.session_mode() == "all":
            return None
        if self.secrets is not None:
            return list(self.secrets)
        try:
            return board.get_session_secrets(self.sid) or []
        except Exception:  # noqa: BLE001
            return []

    # ---- politique d'agent (3.1) ----------------------------------------------
    def _allowed_tools(self) -> list[str]:
        """Outils approuvés sans demander. Session humaine : SAFE_TOOLS. Run
        d'agent : les lectures sûres QU'IL A LE DROIT d'utiliser + ses règles
        auto_approve (syntaxe Claude Code, ex. `Bash(npm audit:*)`)."""
        pol = self.policy
        if not pol:
            return SAFE_TOOLS
        safe = [t for t in SAFE_TOOLS if self._tool_permitted(t)]
        return safe + [r for r in pol.get("auto_approve") or [] if r not in safe]

    def _sdk_max_budget(self) -> float | None:
        """The run budget handed to the engine — only when the engine prices the model
        right. A model the engine would price at another model's tariff is capped by the
        SOKKAN meter (agentcost) instead."""
        pol = self.policy or {}
        if not pol.get("budget_usd") or (self.meter is not None and self.meter.active):
            return None
        return float(pol["budget_usd"])

    def _disallowed_tools(self) -> list[str]:
        """Outils intégrés retirés d'un run d'agent : tous ceux hors de sa liste."""
        allowed = {t.split("(", 1)[0] for t in (self.policy or {}).get("tools") or []}
        if "Task" in allowed or "Agent" in allowed:
            allowed |= {"Task", "Agent"}
        return [t for t in _BUILTIN_TOOLS if t not in allowed]

    def _tool_permitted(self, tool_name: str) -> bool:
        pol = self.policy
        if not pol:
            return True
        if tool_name.startswith("mcp__"):
            server = tool_name.split("__")[1] if tool_name.count("__") >= 2 else ""
            return server in (pol.get("mcp") or []) or tool_name in (pol.get("tools") or [])
        allowed = {t.split("(", 1)[0] for t in pol.get("tools") or []}
        return tool_name in allowed

    def _set_waiting(self, waiting: bool) -> None:
        cb = (self.policy or {}).get("on_wait")
        if cb:
            try:
                cb(waiting)
            except Exception:  # noqa: BLE001 — l'état du run ne bloque jamais l'outil
                pass

    # ---- journal des validations (hitl_journal) ------------------------------
    def _journal_auto(self, gate: contract.Gate, input_data: dict, allowed: bool,
                      reason: str) -> None:
        """An approval nobody clicked (mode, agent policy, sandbox): told apart from a
        person's decision in the journal."""
        hitl_journal.automatic(
            "question" if gate.kind == "question" else "permission",
            session_id=self.sid, tool=gate.tool, title=_tool_title(gate.tool, input_data),
            input_data=input_data, allowed=allowed, reason=reason, mode=self.mode,
            secrets=self.redact_values, owner=self.user,
            agent_run=str((self.policy or {}).get("agent") or ""), model=self._turn_model or "")

    # ---- the gate (heart of the interactivity) -------------------------------
    async def gate(self, gate: contract.Gate) -> contract.Decision:
        """The cockpit's answer to one `gate.requested`: the rules first (confinement,
        agent policy, profile, approval mode), then a person. The engine only sees the
        `Decision`; whatever it is, the rules here can only be stricter than the engine's
        own."""
        loop = asyncio.get_event_loop()
        tool_name, input_data = gate.tool, gate.input
        auto = {"by": "policy", "via": "auto_rule"}
        if self.sandboxed is not None:
            # lot 8, defence in depth: the engine's hook already decided; never let a call
            # reach the permission flow unconfined
            import sandbox
            checked = sandbox.recheck(tool_name, input_data, sid=self.sid, user=self.user,
                                      project=self.sandboxed, cwd=self.cwd)
            if isinstance(checked, str):
                if tool_name == "Bash" and checked == sandbox.BASH_OFF:
                    self._sandbox_bash_off()
                self._journal_auto(gate, input_data, False, "sandbox")
                return contract.Decision.deny(checked, **auto)
            input_data = checked

        if self.policy:
            # agent run: nobody is watching → a question is refused with an instruction, a
            # tool outside the agent's list is refused without waking anybody. The rest
            # follows the normal human gate (HITL ping).
            if gate.kind == "question":
                self._journal_auto(gate, input_data, False, "policy:unattended_question")
                return contract.Decision.deny(
                    "This is an unattended agent run: nobody can answer. Decide with your "
                    "best judgement, and list the open question in your final deliverable.",
                    **auto)
            if not self._tool_permitted(tool_name):
                self._journal_auto(gate, input_data, False, "policy:tool_not_allowed")
                return contract.Decision.deny(
                    f"{tool_name} is not in this agent's allowed tools. Do without it, or say "
                    "in your deliverable that the agent needs it.", **auto)
            if gate.kind == "file_write" and _closed_to_agent_run(
                    gate.path, session_project(self.sid)):
                self._journal_auto(gate, input_data, False, "policy:memory_write")
                return contract.Decision.deny(
                    "An agent run does not write memory files directly: use "
                    "mcp__sokkan-memory__memory_write (the note is quarantined until a human "
                    "approves it).", **auto)

        # 3.6 pilot: a session that was handed documents (feature `documents`) does not write
        # memory files directly — what it learnt from a dropped document reaches the memory
        # only through mcp__sokkan-memory__memory_write, which quarantines it for that session
        # (memory/memory_search_server.py). Same rule as an agent run, for the same reason: a
        # document is outside content.
        if not self.policy and gate.kind == "file_write" and _is_memory_path(gate.path) \
                and self._has_documents():
            self._journal_auto(gate, input_data, False, "policy:documents_memory_write")
            return contract.Decision.deny(
                "This conversation was handed documents: it does not write memory files "
                "directly. Use mcp__sokkan-memory__memory_write — the note waits in quarantine "
                "until a person approves it.", **auto)

        # 3.6 simple profile: a work opened from it belongs to someone who is not a developer —
        # they are never asked to approve a shell command (seen on a timed journey: « the agent
        # wants to carry out a technical operation » for a `mkdir`). Refused here, with what to
        # do instead; file tools keep their approval card.
        if gate.kind == "shell" and not self.policy and self._is_simple_work():
            # nobody clicked: told apart from a person's refusal in the journal of validations
            self._journal_auto(gate, input_data, False, "policy:simple_profile_no_shell")
            return contract.Decision.deny(
                "No shell in this conversation: the person is not a developer. The folder for "
                "the document already exists — save the document with the Write tool (and "
                "change it with Edit), read files with Read. Do not mention this to the person.",
                **auto)

        # 3.6.7 Atelier: ONE approval per document asked for. Once the person approved the save
        # of a document in this turn, the agent does not save it again on its own (a « v2 »
        # nobody asked for meant a second card for one deliverable). A change they ask for
        # comes with their next message, which opens a new turn.
        if gate.kind == "file_write" and not self.policy and self._is_simple_work():
            path = gate.path or str(input_data.get("file_path")
                                    or input_data.get("notebook_path") or "")
            if path and os.path.abspath(path) in getattr(self, "_approved_saves", set()):
                self._journal_auto(gate, input_data, False, "policy:simple_one_approval_per_turn")
                return contract.Decision.deny(
                    "The person already approved this document in this turn: it is saved. Do "
                    "not save it again and do not offer another version. Tell them in two or "
                    "three sentences what it contains and what remains to confirm (if a part is "
                    "still missing, say which one and ask whether to add it); they will ask for "
                    "a change if they want one. Do not mention this message.", **auto)

        # a question with choices: rendered as buttons, the answer goes back to the engine
        if gate.kind == "question":
            qid = gate.gate_id
            fut: asyncio.Future = loop.create_future()
            self._questions[qid] = fut
            self._pending[qid] = (gate, input_data)
            hitl_journal.requested(qid, "question", session_id=self.sid, tool=tool_name,
                                   title=_tool_title(tool_name, input_data),
                                   input_data=input_data, mode=self.mode,
                                   secrets=self.redact_values, owner=self.user,
                                   model=self._turn_model or "")
            self._emit({"type": "question", "id": qid, "ts": time.time(),
                        "questions": input_data.get("questions", [])})
            try:
                answered: contract.Decision = await fut
            except asyncio.CancelledError:
                hitl_journal.decided(qid, "cancelled")
                return contract.Decision.deny("Question cancelled", via="engine")
            finally:
                self._questions.pop(qid, None)
                self._pending.pop(qid, None)
            return answered

        # auto-approval by the current mode (automode / accept-edits) — the web equivalent
        # of the TUI's Shift+Tab. A question (above) always stays interactive: it is a real
        # question to the person, not a mere permission gate.
        if (self.mode == "bypassPermissions" or (
                self.mode == "acceptEdits" and gate.kind == "file_write")) \
                and not self._four_eyes_binding():
            # (four-eyes: an approval mode is the owner approving in advance — it does not
            # stand in for the second person)
            self._journal_auto(gate, input_data, True, f"mode:{self.mode}")
            return _allow(gate, input_data, by=f"mode:{self.mode}", via="auto_rule")

        # a mutating tool (shell / edit / write / …): ask a person
        pid = gate.gate_id
        fut = loop.create_future()
        self._perms[pid] = fut
        self._pending[pid] = (gate, input_data)
        title = _tool_title(tool_name, input_data)
        hitl_journal.requested(pid, "permission", session_id=self.sid, tool=tool_name,
                               title=title, input_data=input_data, mode=self.mode,
                               secrets=self.redact_values, owner=self.user,
                               agent_run=str((self.policy or {}).get("agent") or ""),
                               model=self._turn_model or "")
        self._emit({"type": "permission", "id": pid, "tool": tool_name, "ts": time.time(),
                    "title": title, "input": input_data, "kind": gate.kind})
        self._arm_hitl_notify(pid, title)  # ping if nobody answers in time
        self._set_waiting(True)
        try:
            decision: contract.Decision = await fut
        except asyncio.CancelledError:
            hitl_journal.decided(pid, "cancelled")
            return contract.Decision.deny("Request cancelled", via="engine")
        finally:
            self._perms.pop(pid, None)
            self._pending.pop(pid, None)
            if not self._perms:
                self._set_waiting(False)
        if decision.allowed:
            final = decision.input or input_data
            if self.sandboxed is not None:
                # the browser may send an edited input: what runs is re-checked/re-wrapped
                import sandbox
                final = sandbox.recheck(tool_name, final, sid=self.sid, user=self.user,
                                        project=self.sandboxed, cwd=self.cwd)
                if isinstance(final, str):
                    return contract.Decision.deny(final, **auto)
            saved = str(final.get("file_path") or "") or gate.path
            if gate.kind == "file_write" and self._is_simple_work() and not self.policy \
                    and _is_deliverable_path(saved):
                if not hasattr(self, "_approved_saves"):
                    self._approved_saves = set()
                self._approved_saves.add(os.path.abspath(saved))
            return _allow(gate, final, by=decision.by, via=decision.via)
        return contract.Decision.deny(decision.message or "Denied by the user",
                                      by=decision.by, via=decision.via)

    def _on_gate(self, gate: contract.Gate) -> None:
        """A `gate.requested` of the stream: decided apart, so the stream keeps flowing
        while a person thinks; the decision goes back through `Harness.decide`."""
        harness = self.harness

        async def _run() -> None:
            decision = await self.gate(gate)
            if decision.kind not in gate.options and not decision.denied:
                # the cockpit never asks an engine for more than the gate offers
                decision = contract.Decision.deny(
                    f"This engine does not take the decision {decision.kind!r} for this "
                    "request.", by="policy", via="auto_rule")
            if harness is not None:
                if "@" in (decision.by or "") and self._opaque(harness):
                    # who decided reaches an opaque engine as a handle; the cockpit's journal
                    # gets the person back from `gate.resolved` (`_journal`)
                    from harness import identity
                    decision = dataclasses.replace(
                        decision, by=identity.handle("user", decision.by.lower().strip()))
                try:
                    await harness.decide(gate.gate_id, decision)
                except Exception as e:  # noqa: BLE001 — the engine is gone: nothing to tell
                    print(f"[sokkan] gate {gate.gate_id} of {self.sid}: decision not "
                          f"delivered ({e!r})", file=sys.stderr)

        t = asyncio.create_task(_run())
        self._gate_tasks.add(t)
        t.add_done_callback(self._gate_tasks.discard)

    def _gate_resolved(self, ev: contract.GateResolved) -> None:
        """`gate.resolved`: what the engine applied. A gate still waiting for a person here
        was settled on the engine's side (expired, decided elsewhere): the card closes."""
        fut = self._perms.get(ev.gate_id) or self._questions.get(ev.gate_id)
        if fut is not None and not fut.done():
            kind = "question" if ev.gate_id in self._questions else "permission"
            # nobody here decided: the journal says what the engine applied, and through what
            hitl_journal.decided(
                ev.gate_id, {"allow_once": "allow", "allow_session": "allow", "edit": "edit",
                             "answer": "answer"}.get(ev.decision, "deny"),
                actor="", origin="system",
                message=f"settled by the engine ({ev.via or 'engine'})"
                + (f": {ev.message}" if ev.message else ""), fallback_owner=self.user)
            fut.cancel()
            self._emit({"type": f"{kind}_resolved", "id": ev.gate_id, "ts": time.time()})

    def _is_simple_work(self) -> bool:
        """Opened from the simple profile (simple.works)? Looked up once per session."""
        v = getattr(self, "_simple_work", None)
        if v is None:
            try:
                import simple
                v = simple.enabled() and simple.get_work(self.sid) is not None
            except Exception:  # noqa: BLE001 — unknown = an ordinary session
                v = False
            self._simple_work = v
        return v

    def _has_documents(self) -> bool:
        """Was a document attached to this session (feature `documents`)? Asked at each
        memory write: a document can be attached after the session started."""
        try:
            import documents
            return documents.session_has_documents(self.sid)
        except Exception:  # noqa: BLE001 — no store = no document
            return False

    def _arm_hitl_notify(self, pid: str, title: str) -> None:
        """Programme un ping HITL_DELAY_S plus tard : si la permission est
        toujours en attente (tu es parti), on te notifie ; sinon rien."""
        if not notify.hitl_enabled():
            return

        async def _run() -> None:
            try:
                await asyncio.sleep(notify.HITL_DELAY_S)
            except asyncio.CancelledError:
                return
            fut = self._perms.get(pid)
            if fut is None or fut.done():
                return  # déjà répondu → pas de ping
            try:
                await asyncio.to_thread(
                    notify.send, "SOKKAN — action required",
                    f"A session is waiting for your approval: {title}",
                    notify.session_link(self.sid), "hitl")
            except Exception:  # noqa: BLE001
                pass

        t = asyncio.create_task(_run())
        self._notify_tasks.add(t)
        t.add_done_callback(self._notify_tasks.discard)

    # ---- a person's decision: one entry, the contract's Decision ------------------------
    def pending_gates(self) -> list[dict]:
        """The gates waiting for a person in this session, from the contract (not from the
        flow sent to the pane): what a reviewer, Teams or the rail can decide."""
        out = []
        for gid, (gate, inp) in list(self._pending.items()):
            out.append({"id": gid, "kind": gate.kind, "tool": gate.tool,
                        "title": _tool_title(gate.tool, inp), "options": list(gate.options),
                        "question": gid in self._questions})
        return out

    def _owner(self) -> str:
        return (self._study_owner() or self.user or "").lower().strip()

    def _four_eyes_binding(self) -> bool:
        """Four-eyes on this session's gates: the feature `four_eyes` is on AND the owner has
        delegated the approval to a reviewer (`shared_review`, a write share with the
        approval). Then an allow needs ANOTHER person than the owner."""
        try:
            import features
            if not features.enabled("four_eyes"):
                return False
            import sharing
            return bool(sharing.approvers(self.sid))
        except Exception:  # noqa: BLE001 — no share store: nothing delegated
            return False

    def four_eyes_refusal(self, gate_id: str, decision: contract.Decision) -> str:
        """Why this decision may not be applied under four-eyes ('' = it may). A refusal,
        an answer to a question: always. An allow: only by another person than the owner —
        an unknown decider is not another person."""
        if not decision.allowed or decision.kind == contract.ANSWER or gate_id in self._questions:
            return ""
        if not self._four_eyes_binding():
            return ""
        who = (decision.by or "").lower().strip()
        if who and who != self._owner():
            return ""
        return ("Four-eyes approval: another person approves this request (the reviewer "
                "the session was shared with).")

    def decide(self, gate_id: str, decision: contract.Decision) -> str:
        """A person's decision on a gate of this session — from the rail, a delegated share,
        Microsoft Teams: the ONE path, whatever the engine. ``decision.by`` is who decided,
        ``decision.via`` through what (rail | share | teams); both go to the journal of
        validations and, with the decision, to the engine (`Harness.decide`, echoed in
        `gate.resolved`). → "ok", "unknown" (not waiting: decided, expired, gone) or
        "four_eyes" (refused by the rule, the request still waits)."""
        if gate_id in self._questions:
            fut = self._questions.get(gate_id)
            if fut is None or fut.done():
                return "unknown"
            value = decision.value if decision.kind == contract.ANSWER else None
            if decision.kind == contract.ANSWER:
                hitl_journal.decided(gate_id, "answer", actor=decision.by,
                                     origin=self._origin(decision), message=value,
                                     secrets=self.redact_values, fallback_owner=self.user)
            else:
                hitl_journal.decided(gate_id, "deny", actor=decision.by,
                                     origin=self._origin(decision), message=decision.message,
                                     secrets=self.redact_values, fallback_owner=self.user)
            fut.set_result(decision)
            self._emit({"type": "question_resolved", "id": gate_id, "ts": time.time()})
            return "ok"
        fut = self._perms.get(gate_id)
        if fut is None or fut.done():
            return "unknown"
        if self.four_eyes_refusal(gate_id, decision):
            return "four_eyes"
        gate, asked = self._pending.get(gate_id, (None, {}))
        if decision.allowed:
            final = decision.input if decision.input is not None else (
                asked if gate is not None else None)
            hitl_journal.decided(gate_id, "allow", actor=decision.by,
                                 origin=self._origin(decision), edited_input=final,
                                 secrets=self.redact_values, fallback_owner=self.user)
        else:
            hitl_journal.decided(gate_id, "deny", actor=decision.by,
                                 origin=self._origin(decision),
                                 message=decision.message or "",
                                 secrets=self.redact_values, fallback_owner=self.user)
        fut.set_result(decision)
        # évent de réconciliation : au replay (refresh), les cartes déjà
        # résolues ne doivent pas se ré-afficher comme actionnables
        self._emit({"type": "permission_resolved", "id": gate_id, "ts": time.time(),
                    "decision": "allow" if decision.allowed else "deny"})
        return "ok"

    def decide_threadsafe(self, gate_id: str, decision: contract.Decision,
                          timeout: float = 10.0) -> str:
        """`decide` from a worker thread (a sync route, the Teams bot): applied on the
        api's event loop, which holds the waiting gate; the outcome is returned."""
        fut = self._perms.get(gate_id) or self._questions.get(gate_id)
        loop = fut.get_loop() if fut is not None else None
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if loop is None or here is loop or not loop.is_running():
            return self.decide(gate_id, decision)

        async def _apply() -> str:
            return self.decide(gate_id, decision)
        return asyncio.run_coroutine_threadsafe(_apply(), loop).result(timeout)

    def _origin(self, decision: contract.Decision) -> str:
        """The journal's origin of a person's decision: share / teams as said, else owner or
        project member (who opened the session or not)."""
        if decision.via in ("share", "teams"):
            return decision.via
        return hitl_journal.origin_of(decision.by, self.sid, self.user) if decision.by else ""

    def resolve_permission(self, pid: str, decision: dict) -> str:
        """The pane's dict ({decision, updated_input, message}) → `decide`. Who decides is
        the attribution declared just before (hitl_journal.attribute), when there is one."""
        actor, origin = hitl_journal.peek_attribution(pid)
        via = origin if origin in ("share", "teams") else "rail"
        if decision.get("decision") == "allow":
            d = contract.Decision.allow(decision.get("updated_input"), by=actor, via=via)
        else:
            d = contract.Decision.deny(decision.get("message") or "Denied by the user",
                                       by=actor, via=via)
        out = self.decide(pid, d)
        if out == "ok":
            hitl_journal.forget_attribution(pid)
        return out

    def resolve_question(self, qid: str, answers: dict) -> str:
        actor, origin = hitl_journal.peek_attribution(qid)
        via = origin if origin in ("share", "teams") else "rail"
        out = self.decide(qid, contract.Decision.answer(answers, by=actor, via=via))
        if out == "ok":
            hitl_journal.forget_attribution(qid)
        return out

    # ---- un tour ------------------------------------------------------------
    async def handle_user(self, text: str) -> None:
        if self._busy:
            self._emit({"type": "error",
                        "message": "A turn is already running — interrupt it first."})
            return
        # budget par session (hard stop HITL) : au-delà, plus de nouveau tour —
        # relever le budget (Setup › Organization) ou ouvrir une session neuve.
        budget = instance.budgets().get("budget_session_usd", 0.0)
        if budget and self.cost_usd >= budget:
            self._emit({"type": "error",
                        "message": (f"Session budget reached (${self.cost_usd:.2f} ≥ "
                                    f"${budget:.2f}). Raise the budget in Setup › "
                                    "Organisation, or spawn a fresh session.")})
            return
        # 3.2 lot 4: the project's day / month ceiling — warn at 80 %, hard stop at 100 %
        if not self.policy:
            # study mode: a person works only once they have consented (audited free tier)
            blocked = study.gate(self.user)
            if blocked:
                self._emit({"type": "error", "message": blocked})
                return
        bstate, bmsg = budgets.check(session_project(self.sid))
        if bstate == "stop":
            self._emit({"type": "error", "message": bmsg})
            return
        if bstate == "warn" and not self._project_budget_warned:
            self._project_budget_warned = True
            self._emit({"type": "error", "message": f"Heads-up: {bmsg}"})
        self.last_active = time.monotonic()
        await self.ensure_started()
        assert self.harness is not None
        self._busy = True
        self._refusal_said = None
        self._approved_saves = set()     # 3.6.7: documents the person approved in this turn
        self._emit({"type": "status", "state": "working"})
        # study mode: duration and tier of the turn of a consenting participant (never its text)
        turn = None if self.policy else study.turn_start(self.sid, self._study_owner(), text)
        before, self.last_turn = self.last_result, None
        failed = False
        try:
            turn_id = await self.harness.send(text)
            async for ev in self._stream:
                if isinstance(ev, contract.Error) and not ev.recoverable:
                    failed = True      # the engine's call failed: said below, turn lost
                self._consume(ev)
                if isinstance(ev, contract.RunEnded) or (
                        isinstance(ev, contract.TurnEnded) and ev.turn_id == turn_id):
                    break
        except Exception as e:  # noqa: BLE001
            failed = True
            self._emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            self._explain_refusal(str(e))
        finally:
            self._busy = False
            # the journal of the study reads the turn's record (usage_events): a turn the
            # engine ended without a result counts no token there
            rec = self.last_turn if (self.last_turn or {}).get("reported") else None
            study.turn_end(turn, model=self._model_seen or (rec or {}).get("model") or "",
                           tokens_in=(rec["in_tokens"] + rec["cache_read"] + rec["cache_write"]
                                      if rec else 0),
                           tokens_out=rec["out_tokens"] if rec else 0,
                           is_error=failed or bool(self.last_result is not before and (
                               self.last_result or {}).get("is_error")))
            self._emit({"type": "status", "state": "idle"})

    def _explain_refusal(self, text: str) -> None:
        """The gateway refused for a study reason (free period over, study mode required,
        personal ceiling, too many people today): say it in words, in English and in French,
        once per turn. The event carries the gateway's code: the simple profile says it in
        the person's own language (backend/plain.py → `no.<code>`)."""
        why = study.explain_refusal(text)
        if why and why != self._refusal_said:
            self._refusal_said = why
            self._emit({"type": "error", "code": study.refusal_code(text), "message": why})

    def _study_owner(self) -> str:
        """The person a session's work belongs to: who opened it (board), else who attached."""
        try:
            return board.get_session_owner(self.sid) or self.user
        except Exception:  # noqa: BLE001
            return self.user

    async def interrupt(self) -> None:
        if self.harness is not None:
            try:
                await self.harness.interrupt()
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "error", "message": f"interrupt: {e}"})

    async def set_mode(self, mode: str, actor: str = "") -> None:
        """Change the session's approval mode (default / acceptEdits / bypassPermissions /
        plan). bypass & acceptEdits are rules of the cockpit, applied in `gate` (the engine
        keeps asking, so a question stays interactive); only `plan` is engaged in the
        engine."""
        if mode not in VALID_MODES:
            return
        if mode == "plan" and self.harness is not None \
                and not self.harness.capabilities().plan:
            # a capability the engine does not have is not emulated: the mode is not
            # engaged, and the person is told why
            self._emit({"type": "error",
                        "message": "This engine has no plan mode: the approval mode is "
                                   "unchanged."})
            self._emit({"type": "perm_mode", "mode": self.mode})
            return
        if mode != self.mode:
            hitl_journal.mode_changed(self.sid, self.mode, mode, actor=actor, owner=self.user)
        self.mode = mode
        if self.harness is not None:
            try:
                await self.harness.set_mode("plan" if mode == "plan" else "default")
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "error", "message": f"set_mode: {e}"})
        self._emit({"type": "perm_mode", "mode": mode})

    # ---- contract event → what the cockpit knows (WS flow, costs, journal) ----
    def _emit_model(self, model: str | None) -> None:
        """Emits the session's real model (once / when it changes) → cockpit badge.
        Answers « we do not know which model the session runs on »."""
        if model and model != self._model_seen:
            self._model_seen = model
            self._emit({"type": "model", "model": model})

    def _consume(self, ev: contract.Event) -> None:
        """One event of the run's stream. The flow sent to the front keeps its format: this
        is where the contract is translated into it."""
        self._journal(ev)
        if isinstance(ev, contract.RunInfo):
            sid = ev.engine_session
            if sid and sid != self.claude_session_id:
                self.claude_session_id = sid
                try:  # persisted → resume possible after a restart of the api
                    board.set_claude_session_id(self.sid, sid)
                except Exception:  # noqa: BLE001
                    pass
                self._emit({"type": "session", "claude_session_id": sid})
            self._emit_model(ev.model or ev.requested_model)
            if ev.model:
                # 3.6.5: the model THE API ANSWERED WITH (the badge of each answer, of each
                # save, of each validation) — not the alias the engine was given
                self._turn_model = ev.model
        elif isinstance(ev, contract.AssistantText):
            if ev.text.strip():
                out = _ws_text(ev.text, self._turn_model)
                if self._open_gates:
                    # written while a decision is awaited: shown as a discreet note, the
                    # answer comes after the decision (4.0 decision: shown, not hidden)
                    out["pending"] = True
                self._emit(out)
                if ev.text.lstrip().startswith("API Error"):   # the engine's line for a refusal
                    self._explain_refusal(ev.text)
        elif isinstance(ev, contract.AssistantThink):
            self._emit({"type": "thinking", "text": ev.text})
        elif isinstance(ev, contract.ToolCalled):
            self._emit(_ws_tool_use(ev.call_id, ev.name, ev.input, ev.classification,
                                    self._turn_model))
        elif isinstance(ev, contract.ToolResult):
            self._emit(_ws_tool_result(ev.call_id, ev.output, ev.ok))
        elif isinstance(ev, contract.TurnStarted):
            self._turn_calls, self._turn_usage = {}, None
            self._turn_started = time.time()
        elif isinstance(ev, contract.Usage):
            self._on_usage(ev)
        elif isinstance(ev, contract.TurnEnded):
            self._on_turn_ended(ev)
        elif isinstance(ev, contract.GateRequested):
            self._open_gates.add(ev.gate.gate_id)
            self._on_gate(ev.gate)
        elif isinstance(ev, contract.GateResolved):
            self._open_gates.discard(ev.gate_id)
            self._gate_resolved(ev)
        elif isinstance(ev, contract.ContextInjected):
            self.context_log.append({"turn_id": ev.turn_id, "source": ev.source,
                                     "channel": ev.channel, "notes": list(ev.note_names),
                                     "chars": ev.chars})
        elif isinstance(ev, contract.Error):
            if ev.code == "confined":
                # a tool refused by the project boundary: the model was told; the person is
                # told once when it is the shell that is off (hooks-only sandbox)
                if ev.data.get("shell_off"):
                    self._sandbox_bash_off()
            elif ev.code == "mcp_unavailable":
                # the engine runs without the cockpit's tool servers: said once, as what it is
                self._emit({"type": "error", "code": ev.code, "message": ev.message})
            else:
                self._emit({"type": "error", "message": f"{ev.code}: {ev.message}"})
                self._explain_refusal(ev.message)
        # run.started, turn.started, run.ended: nothing to show

    def _on_usage(self, ev: contract.Usage) -> None:
        if ev.scope == "turn":
            self._turn_usage = ev       # the engine's own total: read at turn.ended
            return
        self._record_usage(ev)
        key = ev.message_id or f"_call{len(self._turn_calls)}"
        prev = self._turn_calls.get(key)
        self._turn_calls[key] = ev if prev is None else dataclasses.replace(
            ev, **{k: max(getattr(prev, k), getattr(ev, k))
                   for k in ("input", "output", "cache_read", "cache_write",
                             "cache_write_long")}, model=prev.model or ev.model)
        if ev.revision:
            # the final figures of a call already counted: for the ledger only. The meter
            # of a run counts a call as it is first reported — unchanged in this lot.
            return
        if self.meter is not None and (self.meter.active or self.meter.guarding):
            if self.meter.active:
                self.meter.add(_usage_dict(ev), ev.message_id)
            else:  # 3.4.4: a model the engine prices — SOKKAN guards the next call too
                self.meter.add_sdk(_usage_dict(ev), ev.message_id, ev.model)
            why = self.meter.over() if self.budget_stop is None else None
            if why:
                self.budget_stop = why
                self._emit({"type": "error", "message": f"Agent run stopped: {why}."})
                try:
                    asyncio.get_running_loop().create_task(self.interrupt())
                except RuntimeError:  # no loop (unit test of _consume): the caller stops
                    pass

    def _record_usage(self, ev: contract.Usage) -> None:
        """The `events` source of Operate › Costs (usage.py): every model call the engine
        reports, recorded as it comes. Best-effort: a ledger that cannot be written never
        breaks a turn."""
        try:
            import usage
            usage.record_call(
                session_id=self.sid, message_id=ev.message_id, model=ev.model,
                in_tokens=ev.input, out_tokens=ev.output, cache_read=ev.cache_read,
                cache_write=ev.cache_write, cache_write_long=ev.cache_write_long,
                turn_id=ev.turn_id, harness=getattr(self.harness, "name", "") or "",
                engine_session=self.claude_session_id, project=self._usage_project(),
                basis=ev.basis)
        except Exception as e:  # noqa: BLE001
            print(f"[sokkan] usage event of {self.sid} not recorded ({e!r})", file=sys.stderr)

    @staticmethod
    def _opaque(harness) -> bool:
        try:
            return harness.capabilities().identity == "opaque"
        except Exception:  # noqa: BLE001
            return False

    def _journal(self, ev: contract.Event) -> None:
        """Every event of the run, journalled by the cockpit (its shape only: run_journal).
        The decider of a gate is journalled as the person, even when the engine only ever
        saw a handle of them."""
        if isinstance(ev, contract.GateResolved) and (ev.by or "").startswith("usr-"):
            try:
                from harness import identity
                who = identity.resolve(ev.by)
                if who and who[0] == "user":
                    ev = dataclasses.replace(ev, by=who[1])
            except Exception:  # noqa: BLE001
                pass
        try:
            j = self._run_journal
        except AttributeError:
            import run_journal
            j = self._run_journal = run_journal.Journal(
                self.sid, self._usage_project(), getattr(self.harness, "name", "") or "")
        j.add(ev)

    def _turn_record(self, ev: contract.TurnEnded, calls: dict, total: contract.Usage | None,
                     started: float | None) -> dict:
        """The normalised record of the turn (usage.turn_record): what the engine reported,
        whatever the engine."""
        import usage
        return usage.turn_record(
            session_id=self.sid, turn_id=ev.turn_id, engine=getattr(self.harness, "name", "")
            or "", calls=[{"model": c.model or "", "usage": _usage_dict(c)}
                          for c in calls.values()],
            turn_total=_usage_dict(total) if total is not None else None,
            model=self._turn_model or self._model_seen, engine_session=self.claude_session_id,
            project=self._usage_project(),
            basis=next((c.basis for c in calls.values() if c.basis), None),
            started_at=started, duration_ms=ev.duration_ms, reported=ev.reported,
            is_error=ev.is_error, stop_reason=ev.stop_reason)

    def _store_turn(self, rec: dict) -> None:
        """Best-effort, like every write of the ledger: it never breaks a turn."""
        self.last_turn = rec
        try:
            import usage
            usage.record_turn(rec)
        except Exception as e:  # noqa: BLE001
            print(f"[sokkan] usage record of {self.sid} not written ({e!r})", file=sys.stderr)

    def _usage_project(self) -> str:
        p = getattr(self, "_usage_proj", None)
        if p is None:
            p = self._usage_proj = session_project(self.sid) or "default"
        return p

    def _on_turn_ended(self, ev: contract.TurnEnded) -> None:
        usage, self._turn_usage = self._turn_usage, None
        calls, self._turn_calls = self._turn_calls, {}
        started, self._turn_started = self._turn_started, None
        turn_cost = ev.cost_usd
        cost_source = "engine" if ev.cost_usd is not None else ""
        rec = self._turn_record(ev, calls, usage, started)
        if not ev.reported:
            # no result of the engine: the error was said; what its calls used is in the
            # ledger, nothing is counted against the session
            self._store_turn(rec)
            return
        tokens = {"input_tokens": rec["in_tokens"], "output_tokens": rec["out_tokens"],
                  "cache_read_input_tokens": rec["cache_read"],
                  "cache_creation_input_tokens": rec["cache_write"]}
        if self.meter is not None and self.meter.active:
            # the engine's figure is at another model's tariff: use SOKKAN's own count
            # exact totals of the turn: the calls were counted as first reported, and an
            # engine reports a call before its output is known (3.6.4)
            self.meter.settle_turn(tokens if (calls or usage is not None) else None)
            turn_cost = max(0.0, self.meter.cost_usd - self.cost_usd)
            self.cost_usd = self.meter.cost_usd
            cost_source = "meter"
        elif turn_cost:
            self.cost_usd += turn_cost
        if calls or usage is not None:
            self.tokens_in += rec["in_tokens"] + rec["cache_read"] + rec["cache_write"]
            self.tokens_out += rec["out_tokens"]
        rec["cost_usd"], rec["cost_source"] = turn_cost, cost_source
        self._store_turn(rec)
        self.num_turns += int(ev.num_turns or 0)
        self.last_result = {"text": ev.text, "is_error": ev.is_error, "subtype": ev.stop_reason}
        if ev.is_error:
            self._explain_refusal(ev.text)
        self._emit({
            "type": "result",
            "text": ev.text,
            "is_error": ev.is_error,
            "num_turns": ev.num_turns,
            "cost_usd": turn_cost,
            "session_cost_usd": round(self.cost_usd, 4),
        })
        # 3.6: an inference refusal (402 credit, 429 quota, endpoint unreachable) said in
        # words a person can act on, instead of the engine's raw `API Error: …` line alone
        # — unless the study already said why: a 429 of the study is not « wait a minute
        # and send again » (the generic sentence of a 429), it would contradict it
        if (ev.is_error or "API Error" in ev.text) and not study.refusal_code(ev.text):
            import llmerrors
            h = llmerrors.from_text(ev.text)
            if h:
                self._emit({"type": "error", "code": h["code"], "title": h["title"],
                            "message": f"{h['title']} — {h['message']}"})
        budget = instance.budgets().get("budget_session_usd", 0.0)
        if budget and not self._budget_warned and self.cost_usd >= 0.8 * budget:
            self._budget_warned = True
            self._emit({"type": "error",
                        "message": (f"Heads-up: this session has used ${self.cost_usd:.2f} "
                                    f"of its ${budget:.2f} budget (≥80%). It will stop "
                                    "accepting new turns at the limit.")})


def _opaque_env_rewrite(env: dict, handles: dict) -> dict:
    """What only the cockpit can re-mint for an opaque engine: the forge ticket names the
    session and the person (base64) — the same ticket over their handles, which the
    credential helper resolves back on the api (forge.gitcred.verify)."""
    if env.get("SOKKAN_FORGE_TICKET") and handles.get("run") and handles.get("user"):
        from forge import gitcred
        env = {**env, "SOKKAN_FORGE_TICKET": gitcred.ticket(handles["run"], handles["user"])}
    return env


def _allow(gate: contract.Gate, final: dict, *, by: str, via: str) -> contract.Decision:
    """Allow a gate with the input that may run: `edit` when it is not what was asked."""
    if final != gate.input:
        return contract.Decision.edit(final, by=by, via=via)
    return contract.Decision.allow(final, by=by, via=via)


def _usage_dict(u: contract.Usage) -> dict:
    """A `usage` event in the shape the price table and the meter read (pricing.cost,
    agentcost.Meter): the fields of the Messages API."""
    return {"input_tokens": u.input, "output_tokens": u.output,
            "cache_read_input_tokens": u.cache_read,
            "cache_creation_input_tokens": u.cache_write,
            "cache_creation": {"ephemeral_1h_input_tokens": u.cache_write_long,
                               "ephemeral_5m_input_tokens": u.cache_write - u.cache_write_long}}


def _is_deliverable_path(path: str) -> bool:
    """A file of a work's deliverables folder (``…/deliverables/<session id>/<name>``)."""
    parts = Path(os.path.abspath(path)).parts if path else ()
    return len(parts) >= 3 and parts[-3] == "deliverables"


def _is_memory_path(path: str) -> bool:
    """Does the path aim at a memory directory or at the quarantine — and at nothing else?
    The memory of the default project, `<data>/projects/<slug>/memory` of any other, the
    quarantine. NOT the rest of a project's space: `<data>/projects/<slug>/work` is where its
    sessions work, where the dropped documents and the deliverables live (the broader
    `_in_memory_dirs` below would refuse the deliverable itself — seen on the first journey
    of the integration, 11.11.2026)."""
    if not path:
        return False
    try:
        import quarantine
        p = Path(path).expanduser().resolve()
        for d in (quarantine.memory_dir(), quarantine.qdir()):
            d = d.expanduser().resolve()
            if p == d or d in p.parents:
                return True
        projects = (Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser(
            "~/.local/share/sokkan"))) / "projects").expanduser().resolve()
        if projects in p.parents:
            rel = p.relative_to(projects).parts
            return len(rel) >= 2 and rel[1] == "memory"
    except Exception:  # noqa: BLE001
        return False
    return False


def _in_memory_dirs(path: str) -> bool:
    """Le chemin vise-t-il le dossier mémoire, la quarantaine, ou l'arbre des projets
    (`<data>/projects`, en entier) ? Trop large pour décider seul : voir `_closed_to_agent_run`."""
    if not path:
        return False
    try:
        import quarantine
        p = Path(path).expanduser().resolve()
        data = Path(os.environ.get("SOKKAN_DATA_DIR",
                                   os.path.expanduser("~/.local/share/sokkan")))
        # 3.2: every project's memory directory lives under <data>/projects
        for d in (quarantine.memory_dir(), quarantine.qdir(), data / "projects"):
            d = d.expanduser().resolve()
            if p == d or d in p.parents:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _closed_to_agent_run(path: str, project: str) -> bool:
    """May an agent run NOT write this file with a file tool? The memory directories and
    the quarantine, always (`memory_write` quarantines instead). The rest of
    `<data>/projects` too — EXCEPT the work folder of the run's own project,
    `<data>/projects/<project>/work`: that is its working directory (`project_cwd`), where a
    Crew agent of a project other than `default` writes its files. Since 3.2 the rule matched
    the whole tree (`_in_memory_dirs`), and such an agent could not write anything at home.
    Nothing else opens: another project's space stays closed, as before."""
    if _is_memory_path(path):
        return True
    if not _in_memory_dirs(path):
        return False
    if not project or project == "default":
        return True
    try:
        p = Path(path).expanduser().resolve()
        work = (Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser(
            "~/.local/share/sokkan"))) / "projects" / project / "work").expanduser().resolve()
        return work not in p.parents
    except Exception:  # noqa: BLE001 — unknown = closed
        return True


# ---- registry (1 AgentSession par sid, en mémoire) --------------------------
_registry: dict[str, AgentSession] = {}


def engine_of(sid: str) -> str:
    """The engine a session ran on: the one its journal names (run_journal), else the first
    declared engine — every session before 4.0 ran on it."""
    try:
        import run_journal
        name = run_journal.engine_of(sid)
    except Exception:  # noqa: BLE001 — no journal: the default engine
        name = ""
    return name if name in registry.declared() else registry.default()


def thread_history(sid: str, ref: str | None = None
                   ) -> tuple[str, list[contract.HistoryItem] | None]:
    """(engine, the thread of session ``sid`` as its engine keeps it). ``None`` = this
    engine cannot read a thread back (`Capabilities.history`). ``ref`` = the engine's
    reference to the run (default: the one persisted for the session). The thread is read
    in the session's own working directory only, never another project's."""
    name = engine_of(sid)
    ref = ref or board.get_claude_session_id(sid) or ""
    try:
        h = registry.create(name)
        caps = h.capabilities()
    except Exception as e:  # noqa: BLE001 — the engine is not installed here
        print(f"[sokkan] engine {name} of {sid} unavailable ({e!r}): thread not read",
              file=sys.stderr)
        return name, None
    if not caps.history:
        return name, None
    if not ref:
        return name, []
    cwd = project_cwd(sid)
    if caps.identity == "opaque":
        from harness import identity
        cwd = identity.opaque_cwd(cwd)
    try:
        return name, list(h.history(ref, cwd=cwd))
    except contract.Unsupported:
        return name, None
    except Exception as e:  # noqa: BLE001 — an unreadable thread never breaks the session
        print(f"[sokkan] thread of {sid} unreadable on {name} ({type(e).__name__})",
              file=sys.stderr)
        return name, []


def _ws_text(text: str, model: str | None) -> dict:
    out = {"type": "text", "text": text}
    if model:
        out["model"] = model
    return out


def _ws_tool_use(call_id, name: str, inp: dict, classification: str, model: str | None) -> dict:
    out = {"type": "tool_use", "id": call_id, "tool": name, "title": _tool_title(name, inp),
           "input": inp, "kind": classification}
    if model:
        out["model"] = model
    return out


def _ws_tool_result(call_id, output: str, ok: bool) -> dict:
    return {"type": "tool_result", "tool_use_id": call_id, "text": output[:8000],
            "is_error": not ok, "truncated": len(output) > 8000}


def history_events(items: list[contract.HistoryItem]) -> list[dict]:
    """A thread read back from its engine, as the events the pane reads: the SAME
    translation as a live run's (`_consume`), so a thread reads back identically whatever
    the engine that kept it."""
    evs: list[dict] = []
    for it in items:
        if it.kind == "prompt":
            evs.append({"type": "user", "text": it.text})
        elif it.kind == "text":
            if it.text.strip():
                evs.append(_ws_text(it.text, it.model))
        elif it.kind == "think":
            evs.append({"type": "thinking", "text": it.text})
        elif it.kind == "tool":
            evs.append(_ws_tool_use(it.call_id, it.name, dict(it.input), it.classification,
                                    it.model))
            if it.output is not None and it.call_id:
                evs.append(_ws_tool_result(it.call_id, it.output, it.ok))
    return evs


def history_messages(items: list[contract.HistoryItem]) -> list[dict]:
    """The same thread in the shape of a session's detail (`GET /api/sessions/{sid}`):
    {role, kind, text | tool, title, input, id, result}."""
    out: list[dict] = []
    for it in items:
        if it.kind == "prompt":
            out.append({"role": "user", "kind": "text", "text": it.text})
        elif it.kind in ("text", "think"):
            m = {"role": "assistant", "kind": "text" if it.kind == "text" else "thinking",
                 "text": it.text}
            if it.model and it.kind == "text":
                m["model"] = it.model
            out.append(m)
        elif it.kind == "tool":
            m = {"role": "assistant", "kind": "tool", "tool": it.name,
                 "title": _tool_title(it.name, dict(it.input)), "input": dict(it.input),
                 "id": it.call_id, "classification": it.classification,
                 "result": None if it.output is None else {
                     "text": it.output[:8000], "is_error": not it.ok,
                     "truncated": len(it.output) > 8000}}
            if it.model:
                m["model"] = it.model
            out.append(m)
    return out


def _seed_ring_from_history(s: AgentSession, ref: str) -> None:
    """After a restart of sokkan-api the ring buffer is empty: the pane of an existing
    session would open empty although its engine keeps the whole thread. The ring is filled
    again from the engine's `history()` — whatever the engine — so that the WebSocket
    replay (cockpit pane, Atelier thread) shows the whole conversation."""
    if s.events:
        return
    _engine, items = thread_history(s.sid, ref)
    if items is None:
        # this engine cannot read a thread back: said in the pane, never rebuilt elsewhere
        s.events.append({"type": "history", "available": False})
        return
    evs = history_events(items)
    if evs:
        evs.append({"type": "status", "state": "idle"})
        if s.redact_values:  # a reopened agent run: the engine kept raw values
            import agents
            evs = agents.redact_obj(evs, s.redact_values)
        s.events.extend(evs[-RING_MAX:])


def get_or_create(sid: str, resume: str | None = None, user: str = "",
                  model: str | None = None, policy: dict | None = None,
                  secrets: list[str] | None = None) -> AgentSession:
    s = _registry.get(sid)
    if s is None:
        # après un restart de sokkan-api : reprendre le claude_session_id persisté
        resume = resume or (board.get_claude_session_id(sid) or None)
        s = AgentSession(sid, cwd=project_cwd(sid), resume=resume, user=user,
                         model=model or MODEL, policy=policy, secrets=secrets)
        if not policy:
            # a finished agent run reopened from Crew → History / Live, or (3.4.4) a
            # person's session after a restart: mask the vault values its env held
            try:
                import agents
                s.redact_values = agents.session_secret_values(sid)
            except Exception:  # noqa: BLE001
                pass
        if resume:
            _seed_ring_from_history(s, resume)
        _registry[sid] = s
    elif user and not s.user:
        s.user = user  # rattachement avant le premier start (client pas encore créé)
    return s


def peek(sid: str) -> AgentSession | None:
    """Session vivante en mémoire (None si pas encore rattachée)."""
    return _registry.get(sid)


def new_sid() -> str:
    return uuid.uuid4().hex


def idle_close_s() -> float:
    """SOKKAN_SESSION_IDLE_CLOSE_S: seconds without any event after which an idle session's
    CLI process is closed (resumed at the next turn). 0 = never (before 3.6.7)."""
    try:
        return max(0.0, float(os.environ.get("SOKKAN_SESSION_IDLE_CLOSE_S") or 1800))
    except ValueError:
        return 1800.0


async def park_idle(idle_s: float | None = None) -> list[str]:
    """Close the CLI of every idle session (see AgentSession.park). → the sids parked."""
    idle_s = idle_close_s() if idle_s is None else idle_s
    if idle_s <= 0:
        return []
    out = []
    for sid, s in list(_registry.items()):
        if s.parkable(idle_s):
            try:
                if await s.park(idle_s):
                    out.append(sid)
            except Exception as e:  # noqa: BLE001 — one session never stops the sweep
                print(f"[sokkan] idle session {sid}: close failed: {e!r}", file=sys.stderr)
    return out


async def idle_loop(stop: asyncio.Event) -> None:
    """The sweep, every minute at most (a quarter of the delay when it is short)."""
    while not stop.is_set():
        idle_s = idle_close_s()
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(5.0, min(60.0, (idle_s or 240) / 4)))
            return
        except asyncio.TimeoutError:
            pass
        parked = await park_idle(idle_s)
        if parked:
            print(f"[sokkan] closed the CLI of {len(parked)} idle session(s) "
                  f"(SOKKAN_SESSION_IDLE_CLOSE_S={idle_s:g}); resumed at their next turn",
                  file=sys.stderr)


async def drop(sid: str) -> None:
    s = _registry.pop(sid, None)
    if s is not None:
        await s.close()


# ---- slash commands disponibles (palette web) -------------------------------
def list_commands() -> list[dict]:
    """Slash commands découvrables : built-ins fréquents + fichiers .claude/commands."""
    builtin = [
        {"name": "/clear", "desc": "réinitialise le contexte de la conversation"},
        {"name": "/compact", "desc": "résume et compacte le contexte"},
        {"name": "/review", "desc": "revue de la pull request / du diff"},
        {"name": "/init", "desc": "génère un CLAUDE.md pour le repo"},
    ]
    found: list[dict] = []
    for root in (Path.home() / ".claude" / "commands",
                 Path(CWD) / ".claude" / "commands"):
        if not root.is_dir():
            continue
        for f in sorted(root.glob("*.md")):
            desc = ""
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    s = line.strip()
                    if s.startswith("description:"):
                        desc = s.split(":", 1)[1].strip()
                        break
                    if s and not s.startswith("---"):
                        desc = s[:80]
                        break
            except OSError:
                pass
            found.append({"name": f"/{f.stem}", "desc": desc})
    seen = set()
    out = []
    for c in builtin + found:
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        out.append(c)
    return out
