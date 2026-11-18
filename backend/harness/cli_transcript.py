"""The session files of the Claude Code CLI — where the legacy engine keeps a thread.

This is a particularity of ONE engine, kept beside its adapter (`legacy.py`, whose
`history()` reads through it) and away from the cockpit: the cockpit reads a thread back
with `Harness.history`, whatever the engine. No SDK import here — the file format is the
CLI's, the SDK plays no part in reading it.

The CLI files a session as ``$CLAUDE_CONFIG_DIR/projects/<cwd>/<session>.jsonl`` where
``<cwd>`` is the session's OWN working directory with every non-alphanumeric character
replaced by « - ». A session of a project other than `default` works in its project's
folder, so its file is not in the instance's folder: only the instance's folder and the
session's own folder are searched — never another project's.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .contract import HistoryItem

# what the CLI writes INTO a person's prompt line for itself: never part of the thread
_REMINDER_RX = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
_SESSION_RX = re.compile(r"[A-Za-z0-9-]{8,64}")     # searched in the session's own folder
_REF_RX = re.compile(r"[A-Za-z0-9_-]{1,128}")          # a file name, never a path


def path(ref: str, cwd: str | None = None) -> Path:
    """Where the CLI keeps session ``ref`` (the file may not exist)."""
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    cwd_slug = (os.environ.get("SOKKAN_AGENT_CWD")
                or ("/workspace" if os.path.isdir("/workspace") else os.getcwd())).replace("/", "-")
    base = os.environ.get("SOKKAN_PROJECT_DIR", os.path.join(claude_dir, "projects", cwd_slug))
    p = Path(base) / f"{ref}.jsonl"
    if cwd and not p.exists() and _SESSION_RX.fullmatch(ref or ""):
        for d in dict.fromkeys((cwd, os.path.realpath(cwd))):
            own = Path(claude_dir) / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", d) / p.name
            if own.exists():
                return own
    return p


def read(ref: str, cwd: str | None = None) -> list[HistoryItem]:
    """The thread of session ``ref``, as contract items. No file = an empty thread."""
    if not ref or not _REF_RX.fullmatch(ref):
        return []
    p = path(ref, cwd)
    if not p.exists():
        return []
    import transcript  # the CLI's JSONL parser (backend/transcript.py)
    from .legacy import classify  # read through the adapter: the SDK is loaded already
    with p.open(encoding="utf-8", errors="replace") as fh:
        msgs = transcript.parse_lines(fh).get("messages", [])
    out: list[HistoryItem] = []
    for m in msgs:
        role, kind = m.get("role"), m.get("kind")
        if role == "user" and kind == "text":
            text = _REMINDER_RX.sub("", m.get("text") or "").strip()
            if text:
                out.append(HistoryItem("prompt", text=text))
        elif role == "assistant" and kind == "text":
            out.append(HistoryItem("text", text=m.get("text") or "", model=m.get("model")))
        elif role == "assistant" and kind == "thinking":
            out.append(HistoryItem("think", text=m.get("text") or ""))
        elif kind == "tool":
            name = m.get("tool") or "tool"
            r = m.get("result")
            out.append(HistoryItem(
                "tool", call_id=m.get("id"), name=name, input=m.get("input") or {},
                classification=classify(name), model=m.get("model"),
                output=(r.get("text") or "") if r else None,
                ok=not bool(r.get("is_error")) if r else True))
        # system chips (/clear, command output), orphan results: the CLI's, not the thread's
    return out
