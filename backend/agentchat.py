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
import llm  # config LLM par instance (BYOK / inférence incluse)
import notify  # HITL push : ping si une permission traîne sans réponse

CWD = os.environ.get("SOKKAN_AGENT_CWD") or (
    "/workspace" if os.path.isdir("/workspace") else os.getcwd())
# serveurs MCP SOKKAN injectés dans chaque session (mémoire RAG + board) —
# indépendants du .mcp.json du workspace, donc le moat marche out-of-the-box
_HERE = os.path.dirname(os.path.abspath(__file__))
_MEM_SRV = os.path.join(_HERE, "..", "memory", "memory_search_server.py")
_BOARD_SRV = os.path.join(_HERE, "board_mcp.py")
_OBS_SRV = os.path.join(_HERE, "observability_mcp.py")
_PY = os.environ.get("SOKKAN_PYTHON", sys.executable)
MCP_SERVERS = {
    "sokkan-memory": {"command": _PY, "args": [os.path.abspath(_MEM_SRV)]},
    "sokkan-board": {"command": _PY, "args": [os.path.abspath(_BOARD_SRV)]},
    # opérer la prod : lire métriques/logs, composer des dashboards
    "sokkan-observability": {"command": _PY, "args": [os.path.abspath(_OBS_SRV)]},
}
MODEL = os.environ.get("SOKKAN_AGENT_MODEL") or None  # None → défaut du CLI
# lectures auto-approuvées (UX fluide) ; tout le reste passe par les boutons.
# Les outils MCP SOKKAN en lecture (mémoire RAG, board) sont sûrs → le seed
# « check ta mémoire » ne bloque pas sur une permission avant qu'on ouvre le pane.
SAFE_TOOLS = [
    "Read", "Glob", "Grep", "TodoWrite", "NotebookRead",
    "mcp__sokkan-memory__memory_search", "mcp__sokkan-memory__memory_get",
    "mcp__sokkan-board__list_tags", "mcp__sokkan-board__list_board",
    # observabilité en LECTURE : diagnostiquer sans gate ; create_dashboard
    # (écriture) reste soumis à permission.
    "mcp__sokkan-observability__query_metrics", "mcp__sokkan-observability__query_logs",
    "mcp__sokkan-observability__list_dashboards",
]
# modes de permission pilotables depuis le cockpit (équivalent web du Shift+Tab du TUI)
VALID_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}
# outils traités comme « édition de fichier » par le mode acceptEdits
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
RING_MAX = 500  # events bufferisés par session pour le replay au reconnect

# titre court d'une carte d'outil (aligné sur transcript.py)
_TOOL_TITLE_FIELD = {
    "Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
    "NotebookEdit": "file_path", "Glob": "pattern", "Grep": "pattern",
    "Task": "description", "Agent": "description", "WebFetch": "url",
    "WebSearch": "query", "Skill": "skill",
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
                 model: str | None = MODEL, user: str = ""):
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
        self._model_seen: str | None = None
        self.mode = "default"  # default | acceptEdits | bypassPermissions | plan

    # ---- diffusion ----------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _emit(self, event: dict) -> None:
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
            opts_kwargs: dict[str, Any] = dict(
                cwd=self.cwd,
                can_use_tool=self._can_use_tool,
                allowed_tools=SAFE_TOOLS,
                # bypass/acceptEdits sont gérés dans _can_use_tool ; seul `plan`
                # doit être engagé au niveau du SDK (change le comportement du modèle)
                permission_mode="plan" if self.mode == "plan" else "default",
                setting_sources=["user", "project", "local"],
                mcp_servers=MCP_SERVERS,
            )
            # config LLM par instance (BYOK / inférence gérée) injectée par session
            env_extra = llm.session_env(self.user)
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

    # ---- callback de permission (cœur de l'interactivité) -------------------
    async def _can_use_tool(self, tool_name: str, input_data: dict, context: Any):
        loop = asyncio.get_event_loop()

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
        try:
            decision = await fut
        except asyncio.CancelledError:
            return PermissionResultDeny(message="Request cancelled")
        finally:
            self._perms.pop(pid, None)
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
            self._emit({
                "type": "result",
                "text": getattr(msg, "result", "") or "",
                "is_error": bool(getattr(msg, "is_error", False)),
                "num_turns": getattr(msg, "num_turns", None),
                "cost_usd": getattr(msg, "total_cost_usd", None),
            })
            return

        # AssistantMessage (et UserMessage portant des tool_result)
        self._emit_model(getattr(msg, "model", None))  # le modèle réel du tour
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


# ---- registry (1 AgentSession par sid, en mémoire) --------------------------
_registry: dict[str, AgentSession] = {}


def get_or_create(sid: str, resume: str | None = None, user: str = "") -> AgentSession:
    s = _registry.get(sid)
    if s is None:
        # après un restart de sokkan-api : reprendre le claude_session_id persisté
        resume = resume or (board.get_claude_session_id(sid) or None)
        s = AgentSession(sid, resume=resume, user=user)
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
