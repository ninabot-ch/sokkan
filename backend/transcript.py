#!/usr/bin/env python3
"""transcript.py — SOKKAN P1 : parse a Claude Code session transcript (JSONL) into
a clean chat message list for the web "chat" render (Claude-web look).

A transcript is `~/.claude/projects/<proj>/<sessionId>.jsonl`, one JSON object per
line. We care about `type` in {user, assistant, system} whose `message.content` is
a string or a list of typed blocks: text / thinking / tool_use / tool_result.

Output = a flat list of render events, with tool_result paired back onto its
tool_use (by tool_use_id) so the UI shows one collapsible tool card (call+output).
Meta/command lines (/clear, local-command-caveat, snapshots) are collapsed to small
system chips or dropped.

Standalone:  python transcript.py <file.jsonl> [--limit N]
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

# tool name → which input field makes the best one-line card title
_TOOL_TITLE_FIELD = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Task": "description",
    "Agent": "description",
    "WebFetch": "url",
    "WebSearch": "query",
    "Skill": "skill",
}


def _text_of(content: Any) -> str:
    """Flatten a tool_result / message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[image]")
                elif "text" in b:
                    parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return ""


def _tool_title(name: str, inp: dict) -> str:
    field = _TOOL_TITLE_FIELD.get(name)
    val = inp.get(field) if field else None
    if isinstance(val, str) and val.strip():
        return val.strip().splitlines()[0][:200]
    return name


def _is_meta_user(rec: dict, text: str) -> str | None:
    """Return a short system-chip label if this user line is a command/meta, else None."""
    if rec.get("isMeta"):
        return "meta"
    if "<command-name>" in text:
        i = text.find("<command-name>") + len("<command-name>")
        j = text.find("</command-name>", i)
        return text[i:j].strip() if j != -1 else "command"
    if "<local-command-caveat>" in text or "<command-stdout>" in text:
        return "command-output"
    return None


def parse_lines(lines: Iterable[str]) -> dict:
    """Parse transcript lines → {title, git_branch, session_id, messages:[...]}."""
    messages: list[dict] = []
    tool_by_id: dict[str, dict] = {}
    title = ""
    git_branch = ""
    session_id = ""

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        session_id = rec.get("sessionId", session_id)
        git_branch = rec.get("gitBranch", git_branch) or git_branch
        rtype = rec.get("type")
        ts = rec.get("timestamp")

        if rtype not in ("user", "assistant", "system"):
            continue

        if rtype == "system":
            # keep only the rare meaningful system notes, as a chip
            sub = rec.get("subtype") or "system"
            txt = _text_of(rec.get("content"))
            if txt and not rec.get("isMeta"):
                messages.append({"role": "system", "kind": "note", "text": txt[:500], "ts": ts})
            continue

        msg = rec.get("message", {}) or {}
        content = msg.get("content")

        # string-content user message
        if isinstance(content, str):
            chip = _is_meta_user(rec, content)
            if chip:
                messages.append({"role": "system", "kind": "chip", "text": chip, "ts": ts})
            else:
                messages.append({"role": "user", "kind": "text", "text": content, "ts": ts})
                if not title:
                    title = content.strip().splitlines()[0][:80]
            continue

        if not isinstance(content, list):
            continue

        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                t = b.get("text", "")
                if t.strip():
                    messages.append({"role": rtype, "kind": "text", "text": t, "ts": ts})
                    if rtype == "user" and not title:
                        title = t.strip().splitlines()[0][:80]
            elif bt == "thinking":
                messages.append({"role": "assistant", "kind": "thinking",
                                 "text": b.get("thinking", ""), "ts": ts})
            elif bt == "tool_use":
                name = b.get("name", "tool")
                inp = b.get("input", {}) or {}
                ev = {"role": "assistant", "kind": "tool", "tool": name,
                      "title": _tool_title(name, inp), "input": inp,
                      "id": b.get("id"), "result": None, "ts": ts}
                messages.append(ev)
                if b.get("id"):
                    tool_by_id[b["id"]] = ev
            elif bt == "tool_result":
                tid = b.get("tool_use_id")
                out = _text_of(b.get("content"))
                is_err = bool(b.get("is_error"))
                ev = tool_by_id.get(tid)
                if ev is not None:
                    ev["result"] = {"text": out[:8000], "is_error": is_err,
                                    "truncated": len(out) > 8000}
                else:  # orphan result (tool_use in an earlier, unloaded slice)
                    messages.append({"role": "user", "kind": "tool_result_orphan",
                                     "text": out[:2000], "is_error": is_err, "ts": ts})

    return {"session_id": session_id, "title": title or "(sans titre)",
            "git_branch": git_branch, "messages": messages}


def parse_file(path: str | Path) -> dict:
    p = Path(path)
    with p.open(encoding="utf-8", errors="replace") as fh:
        data = parse_lines(fh)
    st = p.stat()
    data["mtime"] = st.st_mtime
    data["size"] = st.st_size
    data["n_messages"] = len(data["messages"])
    return data


def session_summary(path: str | Path) -> dict:
    """Lightweight metadata for the session rail (no full message list)."""
    d = parse_file(path)
    return {
        "session_id": d["session_id"] or Path(path).stem,
        "title": d["title"],
        "git_branch": d["git_branch"],
        "mtime": d["mtime"],
        "size": d["size"],
        "n_messages": d["n_messages"],
        "last_role": d["messages"][-1]["role"] if d["messages"] else None,
    }


def quick_summary(path: str | Path, head_lines: int = 60) -> dict:
    """Cheap rail metadata: title from the first user prompt (head only) + stat.

    Avoids a full parse of every transcript when listing — the first real user
    message lands early, so reading the head is enough for the title.
    """
    p = Path(path)
    st = p.stat()
    title = ""
    git_branch = ""
    session_id = p.stem
    with p.open(encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh):
            if i >= head_lines:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            git_branch = rec.get("gitBranch", git_branch) or git_branch
            session_id = rec.get("sessionId", session_id) or session_id
            if rec.get("type") != "user":
                continue
            content = rec.get("message", {}).get("content")
            text = content if isinstance(content, str) else _text_of(content)
            if not text or _is_meta_user(rec, text):
                continue
            title = text.strip().splitlines()[0][:80]
            break
    return {
        "session_id": session_id,
        "title": title or "(sans titre)",
        "git_branch": git_branch,
        "mtime": st.st_mtime,
        "size": st.st_size,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args()
    d = parse_file(args.file)
    print(f"title   : {d['title']}")
    print(f"branch  : {d['git_branch']}  session: {d['session_id']}")
    print(f"messages: {d['n_messages']}  size: {d['size']}  mtime: {d['mtime']}")
    print("-" * 70)
    for m in d["messages"][: args.limit]:
        if m["kind"] == "tool":
            res = m.get("result")
            tag = "ERR" if (res and res["is_error"]) else ("ok " if res else "...")
            print(f"[{m['role']:9}] 🔧 {m['tool']}({tag}): {m['title']}")
        elif m["kind"] == "thinking":
            print(f"[assistant ] 💭 {m['text'][:80]}…")
        elif m["kind"] in ("chip", "note"):
            print(f"[system    ] · {m['text'][:60]}")
        else:
            print(f"[{m['role']:9}] {m['text'][:100]}")
