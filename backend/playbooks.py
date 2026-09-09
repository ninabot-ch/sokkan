#!/usr/bin/env python3
"""playbooks.py — session templates.

A playbook = a named way of spawning a session: a tag, a system-style prompt
template with the user's subject injected, and a one-line description for the
spawn UI. The memory pre-seed and the HITL rules of `board.seed_text` apply on
top — playbooks shape the *mission*, not the guardrails.

Kept as plain data so self-hosters can read them in one screen; the registry is
intentionally small and curated. The historical hardcoded prompts (digest,
incident) are rendered from here too — one source of truth.
"""
from __future__ import annotations

import os

# {subject} = what the user typed in the spawn form (may be empty for
# subject-less playbooks like digest / onboard-memory).
_REGISTRY: dict[str, dict] = {
    "onboard-memory": {
        "label": "Onboard memory",
        "tag": "docs",
        "description": "Scan the repo and write the first memory notes (conventions, ports, architecture) — useful memory in 5 minutes.",
        "subject_optional": True,
        "prompt": (
            "Seed this project's memory so future sessions start informed. {subject}\n\n"
            "1. Survey the repository: README and docs, top-level structure, package/build "
            "manifests, main entrypoints, and `git log --oneline -20` if it is a git repo.\n"
            "2. Write 5-8 memory notes — ONE durable fact per file, in your persistent "
            "memory directory ({mem_dir}), format:\n"
            "   ---\n"
            "   name: short-kebab-slug\n"
            "   description: one strong line (it is embedded for recall — write it well)\n"
            "   priority: high        # only for hard constraints / conventions\n"
            "   metadata:\n"
            "     type: project\n"
            "   ---\n"
            "   The fact. Link related notes with [[wikilinks]].\n"
            "   Cover: what the project is + architecture; conventions (lint, tests, commit "
            "style); how to run/build/test it (commands, ports); key decisions visible in "
            "code or history; anything surprising a newcomer must know.\n"
            "3. Do NOT modify any project file — memory notes only. Finish by listing the "
            "notes you wrote, one line each."
        ),
    },
    "refactor": {
        "label": "Refactor",
        "tag": "backend",
        "description": "Careful refactor: map the blast radius first, keep behavior identical, tests before/after.",
        "prompt": (
            "Refactor task: {subject}\n\n"
            "Ground rules: map every caller/usage BEFORE touching code; behavior must stay "
            "identical (no drive-by features); run the existing tests before and after; "
            "prefer small reviewable steps. If the refactor reveals a bug, report it — "
            "don't silently fix it in the same change."
        ),
    },
    "debug": {
        "label": "Debug",
        "tag": "bugfix",
        "description": "Reproduce first, then diagnose root cause, then propose the minimal fix.",
        "prompt": (
            "Bug to investigate: {subject}\n\n"
            "Method: reproduce it first (or explain precisely why you can't); read the "
            "actual error/logs, don't guess; identify the ROOT cause, not the symptom; "
            "then propose the minimal fix and what test would have caught it."
        ),
    },
    "ops-incident": {
        "label": "Ops / incident",
        "tag": "ops",
        "description": "On-call style: metrics + logs first, likely cause, concrete fix, post-mortem note.",
        "prompt": (
            "Operational issue: {subject}\n\n"
            "You are the on-call engineer. Use mcp__sokkan-observability__query_metrics and "
            "query_logs to investigate, check recent changes, and identify the likely "
            "cause. Propose a concrete fix and wait for my go-ahead before applying "
            "anything. When resolved, write a short post-mortem note to memory so next "
            "time is faster."
        ),
    },
    "review": {
        "label": "Code review",
        "tag": "backend",
        "description": "Review the current diff for correctness and simplification — findings only, no edits.",
        "prompt": (
            "Review the working tree changes (git diff HEAD){subject_suffix}. Look for real "
            "correctness bugs first, then meaningful simplifications. Report findings with "
            "file:line and a concrete failure scenario each — do NOT edit any file."
        ),
    },
    "digest": {
        "label": "Memory digest",
        "tag": "docs",
        "description": "The memory summarizes itself: refresh the project-status note.",
        "subject_optional": True,
        "prompt": (
            "Memory digest — refresh the project-status note. {subject}\n\n"
            "1. Survey the memory: memory_search on the project's main topics, then "
            "memory_get on the recent and priority notes.\n"
            "2. If /workspace is a git repo, skim `git log --oneline -30` for recent work.\n"
            "3. Write (or update) the note `project-status.md` in the memory directory "
            "({mem_dir}): what shipped recently, what is in flight, the durable "
            "conventions and decisions a fresh session must know, open risks. One page "
            "max, `[[wikilinks]]` to the source notes, standard frontmatter "
            "(name: project-status, a strong description:, metadata.type: project)."
        ),
    },
}


def catalog() -> list[dict]:
    """Playbooks for the spawn UI — id, label, description, default tag."""
    return [{"id": pid, "label": p["label"], "description": p["description"],
             "tag": p["tag"], "subject_optional": bool(p.get("subject_optional"))}
            for pid, p in _REGISTRY.items()]


def get(playbook_id: str) -> dict | None:
    return _REGISTRY.get(playbook_id)


def render(playbook_id: str, subject: str = "") -> tuple[str, str] | None:
    """→ (prompt, default_tag) or None if unknown. `subject` is the user's text."""
    p = _REGISTRY.get(playbook_id)
    if not p:
        return None
    subject = (subject or "").strip()
    mem_dir = os.environ.get("SOKKAN_MEMORY_DIR", "the workspace memory directory")
    prompt = p["prompt"].format(
        subject=subject,
        subject_suffix=f" — focus: {subject}" if subject else "",
        mem_dir=mem_dir,
    ).strip()
    return prompt, p["tag"]
