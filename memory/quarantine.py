#!/usr/bin/env python3
"""quarantine.py — SOKKAN 3.1: memory written by agent runs waits for a human.

An agent run reads the outside world (CVE pages, logs, PR diffs…). What it writes
to memory would be recalled into every later session — a perfect channel for a
prompt injection. So a note written by an agent run never lands in the memory
directory: it is stored here, OUTSIDE the indexed directory, with its provenance
(agent, run, session, date). Nothing indexes this directory, so a quarantined note
cannot be recalled — not by the spawn pre-seed, not by memory_search, not by the
per-turn hooks — until a human reads it and approves it in the cockpit (Crew run
detail or the CortHeXis tab). Approve = moved into the memory directory, provenance
kept in its frontmatter. Reject = archived under `rejected/` (or deleted).

Shared by the API (backend) and the sokkan-memory MCP server (separate process).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def qdir() -> Path:
    d = os.environ.get("SOKKAN_MEMORY_QUARANTINE_DIR") or os.path.join(
        os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
        "memory-quarantine")
    return Path(d)


def memory_dir() -> Path:
    return Path(os.environ.get("SOKKAN_MEMORY_DIR", os.path.expanduser("~/.sokkan/memory")))


def _split(text: str) -> tuple[dict, str]:
    """(provenance header, rest) — the header is a JSON line we own, before the note."""
    if text.startswith("<!-- sokkan-quarantine "):
        head, _, rest = text.partition(" -->\n")
        try:
            return json.loads(head.removeprefix("<!-- sokkan-quarantine ")), rest
        except ValueError:
            return {}, rest
    return {}, text


def write(name: str, description: str, body: str, provenance: dict) -> dict:
    """Store a note in quarantine (a newer version of the same name replaces it)."""
    name = (name or "").strip().removesuffix(".md")
    if not NAME_RE.match(name):
        return {"ok": False, "error": "invalid name: lowercase kebab-case slug, 2-64 chars"}
    if not (description or "").strip() or not (body or "").strip():
        return {"ok": False, "error": "description and body are required"}
    prov = {**provenance, "quarantined_at": time.time()}
    fm = ["---", f"name: {name}",
          f"description: {json.dumps(' '.join(description.split()), ensure_ascii=False)}",
          "metadata:", "  type: project",
          f"  provenance: {json.dumps(_prov_line(prov), ensure_ascii=False)}", "---", ""]
    note = "\n".join(fm) + body.strip() + "\n"
    d = qdir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(f"<!-- sokkan-quarantine {json.dumps(prov)} -->\n{note}", encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "note": name, "quarantined": True,
            "info": "written to QUARANTINE: it is not recalled until a human approves it "
                    "in the cockpit (Crew run or CortHeXis tab)"}


def _prov_line(p: dict) -> str:
    when = datetime.fromtimestamp(p.get("quarantined_at") or time.time(), timezone.utc)
    bits = [f"agent {p['agent']}" if p.get("agent") else "agent run"]
    if p.get("run"):
        bits.append(f"run #{p['run']}")
    bits.append(when.strftime("%Y-%m-%d %H:%M UTC"))
    return " · ".join(bits)


def list_notes() -> list[dict]:
    d = qdir()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        prov, rest = _split(f.read_text(encoding="utf-8", errors="replace"))
        m = re.search(r"^description:\s*(.+)$", rest, re.M)
        desc = m.group(1).strip() if m else ""
        try:
            desc = json.loads(desc) if desc.startswith('"') else desc
        except ValueError:
            pass
        out.append({"name": f.stem, "description": desc, "provenance": prov,
                    "exists_in_memory": (memory_dir() / f.name).exists()})
    return out


def get(name: str) -> dict | None:
    if not NAME_RE.match(name or ""):
        return None
    f = qdir() / f"{name}.md"
    if not f.exists():
        return None
    prov, rest = _split(f.read_text(encoding="utf-8", errors="replace"))
    return {"name": name, "provenance": prov, "text": rest,
            "exists_in_memory": (memory_dir() / f.name).exists()}


def approve(name: str, user: str) -> dict:
    """Human-read and approved → into the memory directory (replaces a previous
    version), provenance kept in the frontmatter, plus who approved it."""
    q = get(name)
    if q is None:
        raise KeyError(name)
    text = q["text"].replace(
        "\n---\n", f"\n  approved_by: {json.dumps(user)}\n---\n", 1) if "\nmetadata:" in \
        q["text"] else q["text"]
    md = memory_dir()
    md.mkdir(parents=True, exist_ok=True)
    dest = md / f"{name}.md"
    tmp = dest.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
    (qdir() / f"{name}.md").unlink()
    return {"ok": True, "note": name, "path": str(dest)}


def reject(name: str, user: str, delete: bool = False) -> dict:
    src = qdir() / f"{name}.md"
    if not NAME_RE.match(name or "") or not src.exists():
        raise KeyError(name)
    if delete:
        src.unlink()
        return {"ok": True, "note": name, "deleted": True}
    rej = qdir() / "rejected"
    rej.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    src.replace(rej / f"{name}.{stamp}.md")
    return {"ok": True, "note": name, "archived": True, "by": user}
