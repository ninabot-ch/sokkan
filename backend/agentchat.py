#!/usr/bin/env python3
"""agentchat.py — SOKKAN Chantier B : chat interactif piloté par le Claude Agent SDK.

Remplace l'archi « marionnettiste tmux » : au lieu d'envoyer des touches au TUI et
de scraper l'écran, on pilote claude par le SDK (`claude-agent-sdk`). Les éléments
interactifs deviennent des events structurés rendus en widgets web :

  - permissions d'outils   → callback `can_use_tool` → boutons Autoriser/Refuser
  - questions à choix       → AskUserQuestion (built-in) passe par `can_use_tool`,
                              input = questions[] → boutons ; réponse renvoyée en
                              PermissionResultAllow(updated_input={..., answers})
  - texte / outils / pensée → events AssistantMessage / ToolUseBlock / ThinkingBlock

Un `AgentSession` par sid garde un `ClaudeSDKClient` ouvert (multi-tours). Les
events sortants sont diffusés aux WebSocket abonnés + bufferisés (replay au refresh).

Voir CHANTIER-B.md pour le protocole WS complet.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# --- SDK (imports tolérants aux variations de packaging entre versions) ---------
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions  # type: ignore

try:  # classes de permission
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny  # type: ignore
except ImportError:  # pragma: no cover
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny  # type: ignore

try:  # blocs de message
    from claude_agent_sdk import (  # type: ignore
        SystemMessage, ResultMessage,
        TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
    )
except ImportError:  # pragma: no cover
    from claude_agent_sdk.types import (  # type: ignore
        SystemMessage, ResultMessage,
        TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
    )

import board  # persistance sid ↔ claude_session_id (resume après restart)
import instance  # budgets de coût (hard stop HITL par session)
import memrecall  # rappel mémoire à chaque tour + sous-agents (3.0)
import llm  # config LLM par instance (BYOK / inférence incluse)
import notify  # HITL push : ping si une permission traîne sans réponse
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
    project's CLAUDE.md / MEMORY.md / .mcp.json into it. (Not a sandbox: Read/Bash can
    still reach other paths the API user can read — lot 8.)"""
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


def session_scope_env(project: str) -> str:
    """Comma list handed to the MCP servers: the project + shared (lot 3), or "" = none."""
    import projects
    return ",".join(projects.recall_scope(project)) if project else ""
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
           "SOKKAN_SESSION_SCOPE": session_scope_env(project)}
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
    # observabilité en LECTURE : diagnostiquer sans gate ; create_dashboard
    # (écriture) reste soumis à permission.
    "mcp__sokkan-observability__query_metrics", "mcp__sokkan-observability__query_logs",
    "mcp__sokkan-observability__list_dashboards",
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
}


def _tool_title(name: str, inp: dict) -> str:
    field = _TOOL_TITLE_FIELD.get(name)
    val = inp.get(field) if field else None
    if isinstance(val, str) and val.strip():
        return val.strip().splitlines()[0][:200]
    return name


def _text_of(content: Any) -> str:
    """Aplatit un content (str | list de blocs/dicts) en texte."""
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


class AgentSession:
    """Une session de chat SDK : un ClaudeSDKClient long-vivant + diffusion d'events."""

    def __init__(self, sid: str, cwd: str = CWD, resume: str | None = None,
                 model: str | None = MODEL, user: str = "", policy: dict | None = None,
                 secrets: list[str] | None = None):
        self.sid = sid
        self.cwd = cwd
        self.resume = resume
        self.model = model
        self.user = user  # email du spawneur → attribution per-user du metering (mode géré)
        self.client: ClaudeSDKClient | None = None
        self.claude_session_id: str | None = None
        self.events: list[dict] = []          # ring buffer (replay au reconnect)
        self.subscribers: set[asyncio.Queue] = set()
        self._perms: dict[str, asyncio.Future] = {}
        self._questions: dict[str, asyncio.Future] = {}
        self._notify_tasks: set[asyncio.Task] = set()  # HITL push différé
        self._busy = False
        self._start_lock = asyncio.Lock()
        self.cost_usd = 0.0          # coût estimé cumulé (ResultMessage.total_cost_usd)
        self._budget_warned = False  # avertissement 80 % émis une seule fois
        self._model_seen: str | None = None
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
            vault.session_env(list(policy.get("secrets") or [])) if policy else {})
        # 3.1.2 : comptage SOKKAN d'un run sur un modèle non-Claude (agentcost.Meter)
        self.meter = (policy or {}).get("meter")
        self.budget_stop: str | None = None

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
        self.events.append(event)
        if len(self.events) > RING_MAX:
            del self.events[: len(self.events) - RING_MAX]
        for q in list(self.subscribers):
            q.put_nowait(event)

    # ---- cycle de vie SDK ---------------------------------------------------
    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self.client is not None:
                return
            pol = self.policy
            opts_kwargs: dict[str, Any] = dict(
                cwd=self.cwd,
                can_use_tool=self._can_use_tool,
                allowed_tools=self._allowed_tools(),
                # bypass/acceptEdits sont gérés dans _can_use_tool ; seul `plan`
                # doit être engagé au niveau du SDK (change le comportement du modèle)
                permission_mode="plan" if self.mode == "plan" else "default",
                setting_sources=["user", "project", "local"],
                mcp_servers=mcp_servers_for(self.sid, self.user,
                                            only=pol.get("mcp") if pol else None,
                                            agent_run=({"agent": pol.get("agent"),
                                                        "run": pol.get("run")}
                                                       if pol else False)),
            )
            if pol:
                # défense en profondeur : une règle « allow » des settings utilisateur ou
                # projet court-circuite can_use_tool ; disallowed_tools l'emporte toujours
                opts_kwargs["disallowed_tools"] = self._disallowed_tools()
            sdk_budget = self._sdk_max_budget()
            if sdk_budget and "max_budget_usd" in _OPTION_FIELDS:
                opts_kwargs["max_budget_usd"] = sdk_budget
            # memory recall at every turn + for every sub-agent (3.0, P0-3)
            hooks = memrecall.sdk_hooks(self.sid, projects=self._recall_scope())
            if hooks:
                opts_kwargs["hooks"] = hooks
            # config LLM par instance (BYOK / inférence gérée) + coffre de secrets
            # (le vibecoder opère sa prod : $STRIPE_KEY & co dans les shells, sans
            # que la valeur ne soit jamais lue par l'UI ni le LLM) injectés par session
            env_extra = {**vault.session_env(self._secret_names()),
                         **llm.session_env(self.user)}
            # 3.2 lot 5: a forge project's session pushes with the PERSON's token, through
            # a credential helper — the env holds a ticket, never the token (forge.gitcred)
            env_extra.update(self._forge_env())
            if env_extra:
                opts_kwargs["env"] = {**os.environ, **env_extra}
            model = self.model or llm.session_model()
            if model:
                opts_kwargs["model"] = model
            if self.resume:
                opts_kwargs["resume"] = self.resume
            options = ClaudeAgentOptions(**opts_kwargs)
            self.client = ClaudeSDKClient(options=options)
            # __aenter__ plutôt que `async with` : on garde le client ouvert
            await self.client.__aenter__()

    async def close(self) -> None:
        for fut in list(self._perms.values()) + list(self._questions.values()):
            if not fut.done():
                fut.cancel()
        if self.client is not None:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    def _forge_env(self) -> dict[str, str]:
        try:
            from forge import gitcred
            return gitcred.session_env(self.sid, self.user, session_project(self.sid))
        except Exception as e:  # noqa: BLE001 — no credentials = the push fails, says so
            print(f"[sokkan] forge credentials of {self.sid} unavailable ({type(e).__name__})",
                  file=sys.stderr)
            return {}

    def _recall_scope(self) -> tuple[str, ...]:
        """Memory scope of this session (3.2): its project + shared (see session_project)."""
        import projects
        p = session_project(self.sid)
        return projects.recall_scope(p) if p else ()

    def _secret_names(self) -> list[str] | None:
        """Quels secrets du coffre vont dans l'env : ceux de l'agent pour un run ;
        pour une session humaine, tout (mode `all`) ou ceux choisis à son ouverture
        (mode `named`, rien si rien n'a été choisi)."""
        # 3.2: the vault is per instance until lot 4 → only the default project gets it
        if session_project(self.sid) != "default":
            return []
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
        """The run budget handed to the SDK — only when the SDK prices the model right
        (Claude on Anthropic). A non-Claude model would be priced at a Claude tariff
        by the CLI: the SOKKAN meter (agentcost) enforces the budget instead."""
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

    # ---- callback de permission (cœur de l'interactivité) -------------------
    async def _can_use_tool(self, tool_name: str, input_data: dict, context: Any):
        loop = asyncio.get_event_loop()

        if self.policy:
            # run d'agent : personne ne regarde → une question est refusée avec une
            # consigne, un outil hors de la liste de l'agent est refusé sans réveiller
            # personne. Le reste suit le gate humain normal (ping HITL).
            if tool_name == "AskUserQuestion":
                return PermissionResultDeny(message=(
                    "This is an unattended agent run: nobody can answer. Decide with your "
                    "best judgement, and list the open question in your final deliverable."))
            if not self._tool_permitted(tool_name):
                return PermissionResultDeny(message=(
                    f"{tool_name} is not in this agent's allowed tools. Do without it, or say "
                    "in your deliverable that the agent needs it."))
            if tool_name in _EDIT_TOOLS and _in_memory_dirs(
                    str(input_data.get("file_path") or input_data.get("notebook_path") or "")):
                return PermissionResultDeny(message=(
                    "An agent run does not write memory files directly: use "
                    "mcp__sokkan-memory__memory_write (the note is quarantined until a human "
                    "approves it)."))

        # AskUserQuestion : on rend les choix en boutons, on injecte la réponse
        if tool_name == "AskUserQuestion":
            qid = uuid.uuid4().hex
            fut: asyncio.Future = loop.create_future()
            self._questions[qid] = fut
            self._emit({"type": "question", "id": qid,
                        "questions": input_data.get("questions", [])})
            try:
                answers = await fut
            except asyncio.CancelledError:
                return PermissionResultDeny(message="Question cancelled")
            finally:
                self._questions.pop(qid, None)
            return PermissionResultAllow(
                updated_input={**input_data, "answers": answers}
            )

        # auto-approbation selon le mode courant (automode / accept-édits) — l'équivalent
        # web du Shift+Tab du TUI. AskUserQuestion (au-dessus) reste toujours interactif :
        # c'est une vraie question à l'utilisateur, pas un simple gate de permission.
        if self.mode == "bypassPermissions" or (
                self.mode == "acceptEdits" and tool_name in _EDIT_TOOLS):
            return PermissionResultAllow(updated_input=input_data)

        # outil mutant (Bash/Edit/Write/…) : demande d'autorisation
        pid = uuid.uuid4().hex
        fut = loop.create_future()
        self._perms[pid] = fut
        title = _tool_title(tool_name, input_data)
        self._emit({"type": "permission", "id": pid, "tool": tool_name,
                    "title": title, "input": input_data})
        self._arm_hitl_notify(pid, title)  # ping si tu ne réponds pas à temps
        self._set_waiting(True)
        try:
            decision = await fut
        except asyncio.CancelledError:
            return PermissionResultDeny(message="Request cancelled")
        finally:
            self._perms.pop(pid, None)
            if not self._perms:
                self._set_waiting(False)
        if decision.get("decision") == "allow":
            return PermissionResultAllow(
                updated_input=decision.get("updated_input") or input_data
            )
        return PermissionResultDeny(
            message=decision.get("message") or "Denied by the user"
        )

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

    def resolve_permission(self, pid: str, decision: dict) -> None:
        fut = self._perms.get(pid)
        if fut and not fut.done():
            fut.set_result(decision)
            # évent de réconciliation : au replay (refresh), les cartes déjà
            # résolues ne doivent pas se ré-afficher comme actionnables
            self._emit({"type": "permission_resolved", "id": pid})

    def resolve_question(self, qid: str, answers: dict) -> None:
        fut = self._questions.get(qid)
        if fut and not fut.done():
            fut.set_result(answers)
            self._emit({"type": "question_resolved", "id": qid})

    # ---- un tour ------------------------------------------------------------
    async def handle_user(self, text: str) -> None:
        if self._busy:
            self._emit({"type": "error",
                        "message": "A turn is already running — interrupt it first."})
            return
        # budget par session (hard stop HITL) : au-delà, plus de nouveau tour —
        # relever le budget (Profil → Organisation) ou ouvrir une session neuve.
        budget = instance.budgets().get("budget_session_usd", 0.0)
        if budget and self.cost_usd >= budget:
            self._emit({"type": "error",
                        "message": (f"Session budget reached (${self.cost_usd:.2f} ≥ "
                                    f"${budget:.2f}). Raise the budget in Profile → "
                                    "Organisation, or spawn a fresh session.")})
            return
        await self.ensure_started()
        assert self.client is not None
        self._busy = True
        self._emit({"type": "status", "state": "working"})
        try:
            await self.client.query(text)
            async for msg in self.client.receive_response():
                self._translate(msg)
        except Exception as e:  # noqa: BLE001
            self._emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self._busy = False
            self._emit({"type": "status", "state": "idle"})

    async def interrupt(self) -> None:
        if self.client is not None:
            try:
                await self.client.interrupt()
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "error", "message": f"interrupt: {e}"})

    async def set_mode(self, mode: str) -> None:
        """Change le mode de permission de la session (default / acceptEdits /
        bypassPermissions / plan). bypass & acceptEdits sont appliqués dans
        `_can_use_tool` (SDK laissé sur default pour garder AskUserQuestion
        interactif) ; seul `plan` est engagé au niveau du SDK."""
        if mode not in VALID_MODES:
            return
        self.mode = mode
        if self.client is not None:
            try:
                await self.client.set_permission_mode(
                    "plan" if mode == "plan" else "default")
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "error", "message": f"set_mode: {e}"})
        self._emit({"type": "perm_mode", "mode": mode})

    # ---- traduction message SDK → event WS ----------------------------------
    def _emit_model(self, model: str | None) -> None:
        """Émet le modèle réel de la session (une fois / quand il change) → badge cockpit.
        Répond à « on ne sait pas sur quel modèle tourne la session »."""
        if model and model != self._model_seen:
            self._model_seen = model
            self._emit({"type": "model", "model": model})

    def _translate(self, msg: Any) -> None:
        if isinstance(msg, SystemMessage):
            data = getattr(msg, "data", {}) or {}
            sid = data.get("session_id")
            if sid and sid != self.claude_session_id:
                self.claude_session_id = sid
                try:  # persisté → resume possible après restart de sokkan-api
                    board.set_claude_session_id(self.sid, sid)
                except Exception:  # noqa: BLE001
                    pass
                self._emit({"type": "session", "claude_session_id": sid})
            self._emit_model(data.get("model"))  # le message init porte souvent le modèle
            return

        if isinstance(msg, ResultMessage):
            turn_cost = getattr(msg, "total_cost_usd", None)
            usage = getattr(msg, "usage", None) or {}
            if self.meter is not None and self.meter.active:
                # the CLI's figure is at a Claude tariff: use SOKKAN's own count
                if not self.meter.seen:
                    self.meter.add(usage if isinstance(usage, dict) else None)
                turn_cost = max(0.0, self.meter.cost_usd - self.cost_usd)
                self.cost_usd = self.meter.cost_usd
            elif turn_cost:
                self.cost_usd += float(turn_cost)
            if isinstance(usage, dict):
                self.tokens_in += int(usage.get("input_tokens") or 0) + int(
                    usage.get("cache_read_input_tokens") or 0) + int(
                    usage.get("cache_creation_input_tokens") or 0)
                self.tokens_out += int(usage.get("output_tokens") or 0)
            self.num_turns += int(getattr(msg, "num_turns", 0) or 0)
            self.last_result = {
                "text": getattr(msg, "result", "") or "",
                "is_error": bool(getattr(msg, "is_error", False)),
                "subtype": getattr(msg, "subtype", "") or "",
            }
            self._emit({
                "type": "result",
                "text": getattr(msg, "result", "") or "",
                "is_error": bool(getattr(msg, "is_error", False)),
                "num_turns": getattr(msg, "num_turns", None),
                "cost_usd": turn_cost,
                "session_cost_usd": round(self.cost_usd, 4),
            })
            budget = instance.budgets().get("budget_session_usd", 0.0)
            if budget and not self._budget_warned and self.cost_usd >= 0.8 * budget:
                self._budget_warned = True
                self._emit({"type": "error",
                            "message": (f"Heads-up: this session has used ${self.cost_usd:.2f} "
                                        f"of its ${budget:.2f} budget (≥80%). It will stop "
                                        "accepting new turns at the limit.")})
            return

        # AssistantMessage (et UserMessage portant des tool_result)
        self._emit_model(getattr(msg, "model", None))  # le modèle réel du tour
        if self.meter is not None and self.meter.active:
            self.meter.add(getattr(msg, "usage", None), getattr(msg, "message_id", None))
            why = self.meter.over() if self.budget_stop is None else None
            if why:
                self.budget_stop = why
                self._emit({"type": "error", "message": f"Agent run stopped: {why}."})
                try:
                    asyncio.get_running_loop().create_task(self.interrupt())
                except RuntimeError:  # no loop (unit test of _translate): the caller stops
                    pass
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            return
        for b in content:
            self._translate_block(b)

    def _translate_block(self, b: Any) -> None:
        if isinstance(b, TextBlock):
            t = getattr(b, "text", "") or ""
            if t.strip():
                self._emit({"type": "text", "text": t})
        elif isinstance(b, ThinkingBlock):
            self._emit({"type": "thinking",
                        "text": getattr(b, "thinking", "") or ""})
        elif isinstance(b, ToolUseBlock):
            name = getattr(b, "name", "tool")
            inp = getattr(b, "input", {}) or {}
            self._emit({"type": "tool_use", "id": getattr(b, "id", None),
                        "tool": name, "title": _tool_title(name, inp), "input": inp})
        elif isinstance(b, ToolResultBlock):
            out = _text_of(getattr(b, "content", ""))
            self._emit({"type": "tool_result",
                        "tool_use_id": getattr(b, "tool_use_id", None),
                        "text": out[:8000], "is_error": bool(getattr(b, "is_error", False)),
                        "truncated": len(out) > 8000})


try:  # champs d'options du SDK installé (max_budget_usd n'existe pas partout)
    import dataclasses as _dc
    _OPTION_FIELDS = {f.name for f in _dc.fields(ClaudeAgentOptions)}
except Exception:  # noqa: BLE001
    _OPTION_FIELDS = set()


def _in_memory_dirs(path: str) -> bool:
    """Le chemin vise-t-il le dossier mémoire ou la quarantaine ? (runs d'agent)"""
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


# ---- registry (1 AgentSession par sid, en mémoire) --------------------------
_registry: dict[str, AgentSession] = {}


def _transcript_path(csid: str) -> Path:
    """Chemin du transcript persisté par Claude Code (même résolution que
    PROJECT_DIR côté app.py — dupliquée ici pour éviter le cycle d'import)."""
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    cwd_slug = (os.environ.get("SOKKAN_AGENT_CWD")
                or ("/workspace" if os.path.isdir("/workspace") else os.getcwd())).replace("/", "-")
    base = os.environ.get("SOKKAN_PROJECT_DIR", os.path.join(claude_dir, "projects", cwd_slug))
    return Path(base) / f"{csid}.jsonl"


def _seed_ring_from_transcript(s: AgentSession, csid: str) -> None:
    """Après un restart de sokkan-api, le ring buffer est vide : le pane d'une
    session existante s'affichait vide alors que tout l'historique est dans le
    transcript JSONL. On re-peuple le ring depuis le transcript pour que le
    replay WS montre la conversation complète."""
    path = _transcript_path(csid)
    if s.events or not path.exists():
        return
    import transcript

    try:
        msgs = transcript.parse_file(path).get("messages", [])
    except Exception:  # noqa: BLE001 — un transcript illisible ne doit pas bloquer la session
        return
    evs: list[dict] = []
    for m in msgs:
        role, kind, text = m.get("role"), m.get("kind"), m.get("text", "")
        if role == "user" and kind == "text":
            evs.append({"type": "user", "text": text})
        elif role == "assistant" and kind == "text":
            evs.append({"type": "text", "text": text})
        elif role == "assistant" and kind == "thinking":
            evs.append({"type": "thinking", "text": text})
        elif kind == "tool":
            evs.append({"type": "tool_use", "id": m.get("id"), "tool": m.get("tool", "tool"),
                        "title": m.get("title", ""), "input": m.get("input", {}) or {}})
            r = m.get("result")
            if r and m.get("id"):
                evs.append({"type": "tool_result", "tool_use_id": m["id"],
                            "text": r.get("text", ""), "is_error": bool(r.get("is_error")),
                            "truncated": bool(r.get("truncated"))})
    if evs:
        evs.append({"type": "status", "state": "idle"})
        if s.redact_values:  # a reopened agent run: the transcript holds raw values
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
            # a finished agent run reopened from Crew → History / Live: mask its secrets
            try:
                import agents
                s.redact_values = agents.secrets_for_session(sid)
            except Exception:  # noqa: BLE001
                pass
        if resume:
            _seed_ring_from_transcript(s, resume)
        _registry[sid] = s
    elif user and not s.user:
        s.user = user  # rattachement avant le premier start (client pas encore créé)
    return s


def peek(sid: str) -> AgentSession | None:
    """Session vivante en mémoire (None si pas encore rattachée)."""
    return _registry.get(sid)


def new_sid() -> str:
    return uuid.uuid4().hex


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
