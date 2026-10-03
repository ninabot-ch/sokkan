#!/usr/bin/env python3
"""memrecall.py — SOKKAN 3.0 : the memory recall installed in every session it launches.

Two hooks, in every session spawned by SOKKAN (P0-3):

* ``UserPromptSubmit`` → the notes related to each user message are injected as
  ``additionalContext`` (top 4, threshold per model, quoted note names, no note twice in
  the same session);
* ``PreToolUse`` on ``Task|Agent`` → the recall of the sub-agent's subject is appended to
  its prompt (``updatedInput``): a sub-agent otherwise starts with nothing.

Chat sessions (Claude Agent SDK) get them as in-process callbacks (``sdk_hooks``): no
process start per message, the store pool and the embedding client stay warm. Terminal
sessions (tmux, plain ``claude``) get the same logic as command hooks through
``claude --settings <file>`` (``cli_settings_path``), running ``memory/recall_hook.py``.
Both are keyed on the SOKKAN session id, the one the spawn pre-recall is logged under.

Off when the memory store is not the backend (2.x SQLite) or ``CORTHEXIS_RECALL=0``.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MEMORY = _HERE.parent / "memory"
if str(_MEMORY) not in sys.path:
    sys.path.insert(0, str(_MEMORY))

import store_backend  # noqa: E402

HOOK_TIMEOUT_S = 5          # hard limit given to Claude Code; the recall budget is ~1.5 s
SUBAGENT_MATCHER = "Task|Agent"

_lock = threading.Lock()
_recaller = None


def active() -> bool:
    try:
        from core import recall
    except ImportError:  # pragma: no cover — memory/core missing
        return False
    # during the 2.x -> 3.0 migration the recall reads memory.db (LegacyIndex)
    return recall.enabled() and (store_backend.enabled() or store_backend.migrating())


_legacy_recaller = None


def recaller():
    """Process-wide Recaller on the backend's store and query embedder — or, while the
    migration has not switched yet, on the 2.x memory.db with the 2.x model."""
    global _recaller, _legacy_recaller
    if not store_backend.enabled():
        if _legacy_recaller is None:
            from core.recall import Recaller, RecallConfig

            _legacy_recaller = Recaller(store_backend.LegacyIndex(),
                                        store_backend.LegacyQueryEmbedder(),
                                        RecallConfig.from_env(), profile="legacy",
                                        log=lambda m: print(f"[sokkan] {m}", file=sys.stderr))
        return _legacy_recaller
    if _recaller is None:
        with _lock:
            if _recaller is None:
                from core import embed
                from core.recall import Recaller, RecallConfig

                try:
                    profile = embed.current_profile()
                except ValueError:
                    profile = None
                _recaller = Recaller(store_backend.get_store(), store_backend.embedder(),
                                     RecallConfig.from_env(), profile=profile,
                                     log=lambda m: print(f"[sokkan] {m}", file=sys.stderr))
    # the query embedder can change (2.x generation replaced by a 3.0 one)
    _recaller.embedder = store_backend.embedder()
    return _recaller


def reset() -> None:
    global _recaller, _legacy_recaller
    with _lock:
        _recaller = _legacy_recaller = None


def hook_output(payload: dict, session_id: str) -> dict:
    """Synchronous core of both hooks (also used by the tests)."""
    from core import recall

    return recall.hook_output(payload, recaller(), session_id=session_id)


# --------------------------------------------------------------------------- SDK sessions

def sdk_hooks(session_id: str) -> dict:
    """``ClaudeAgentOptions.hooks`` for one chat session ({} when recall is off)."""
    if not active():
        return {}
    from claude_agent_sdk import HookMatcher  # type: ignore

    async def _run(payload: dict) -> dict:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(hook_output, dict(payload), session_id),
                timeout=HOOK_TIMEOUT_S - 1)
        except Exception as e:  # noqa: BLE001 — a recall never breaks a turn
            print(f"[sokkan] recall hook failed: {e!r}", file=sys.stderr)
            return {}

    async def on_prompt(payload, _tool_use_id, _context):
        return await _run(payload)

    async def on_subagent(payload, _tool_use_id, _context):
        return await _run(payload)

    return {
        "UserPromptSubmit": [HookMatcher(hooks=[on_prompt], timeout=HOOK_TIMEOUT_S)],
        "PreToolUse": [HookMatcher(matcher=SUBAGENT_MATCHER, hooks=[on_subagent],
                                   timeout=HOOK_TIMEOUT_S)],
    }


# --------------------------------------------------------------------------- CLI sessions

def _data_dir() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))


def token_path() -> Path:
    return _data_dir() / "claude-hooks" / "recall-token"


def hook_token() -> str:
    """Shared secret between the backend and the command hooks of terminal sessions
    (file 0600 in the data dir; the hook reads it, it never appears in a command line)."""
    p = token_path()
    try:
        tok = p.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except OSError:
        pass
    import secrets

    tok = secrets.token_urlsafe(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def api_url() -> str:
    """Where the command hook reaches the backend (loopback, same container)."""
    return os.environ.get("SOKKAN_RECALL_API_URL") or \
        f"http://127.0.0.1:{os.environ.get('SOKKAN_API_PORT', '8097')}"


def cli_settings(session_id: str | None = None) -> dict:
    """Claude Code settings fragment with the two command hooks. The hook asks the warm
    backend (``POST /api/memory/hook``, ~50 ms of process start) and only falls back to
    an in-process recall (~1 s: psycopg + numpy imports) when the API does not answer."""
    py = os.environ.get("SOKKAN_PYTHON", sys.executable)
    hook_token()
    env = (f"CORTHEXIS_RECALL_API_URL={shlex.quote(api_url())} "
           f"CORTHEXIS_RECALL_TOKEN_FILE={shlex.quote(str(token_path()))} ")
    cmd = f"{env}{shlex.quote(py)} {shlex.quote(str(_MEMORY / 'recall_hook.py'))}"
    if session_id:
        cmd = f"CORTHEXIS_RECALL_SESSION_ID={shlex.quote(session_id)} {cmd}"
    hook = {"type": "command", "command": cmd, "timeout": HOOK_TIMEOUT_S}
    return {"hooks": {
        "UserPromptSubmit": [{"hooks": [hook]}],
        "PreToolUse": [{"matcher": SUBAGENT_MATCHER, "hooks": [hook]}],
    }}


def cli_settings_path() -> str | None:
    """A settings file for ``claude --settings`` (None when recall is off). The session id
    comes from the hook payload: SOKKAN starts terminal sessions with ``--session-id``."""
    if not active():
        return None
    path = _data_dir() / "claude-hooks" / "memory-recall.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(cli_settings(), indent=1)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
    except OSError as e:
        print(f"[sokkan] cannot write the recall hook settings: {e}", file=sys.stderr)
        return None
    return str(path)


def check_token(given: str | None) -> bool:
    import hmac

    try:
        return bool(given) and hmac.compare_digest(given, hook_token())
    except OSError:
        return False
