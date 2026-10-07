#!/usr/bin/env python3
"""agent_templates.py — ready-made Crew agents (3.3).

A template = the fields of an agent, pre-filled; the person picks it in Crew → « + New
agent », adjusts it in the form and saves it like any agent (same validation, same
approval rules: a template never activates anything by itself).

`morning-brief` (Helm): every weekday morning, the brief of a person or a team — cards
that moved, blockers, approvals waiting, incidents, agents in error, recent decisions,
and the agenda when a calendar is configured. The data comes from ONE read-only MCP tool
(`morning_brief` of sokkan-board, computed by SOKKAN, not guessed by the model); the
deliverable is a memory note — in QUARANTINE, like everything an agent run writes — and
a notification.
"""
from __future__ import annotations

import copy

_BRIEF_TOOL = "mcp__sokkan-board__morning_brief"

TEMPLATES: dict[str, dict] = {
    "morning-brief": {
        "label": "Morning brief",
        "feature": "helm",
        "description": "Every weekday at 07:30: what moved, what is blocked, what waits for you, "
                       "incidents, agents in error, decisions, today's agenda. Delivered as a "
                       "memory note (quarantined until you approve it) and a notification.",
        "params": {"person": "the person the brief is for (their email), or empty",
                   "team": "or a team (sso:<group>), or empty = the whole project"},
        "fields": {
            "name": "morning-brief",
            "model": "haiku",
            "purpose": (
                "Write the morning brief of {who}. Call the mcp__sokkan-board__morning_brief tool "
                "ONCE with person=\"{person}\" and team=\"{team}\"; it returns the facts already "
                "gathered by SOKKAN (cards that moved, blockers, approvals waiting, incidents, agents "
                "in error, recent decisions, agenda) and a markdown draft. Do not invent anything "
                "that is not in the tool's answer. Tighten the draft: most urgent first (blockers, "
                "approvals, incidents), then the agenda, then the rest; keep card numbers (#12) so "
                "the reader can open them."),
            "deliverable": "The brief in markdown, at most one screen, sections in the order: "
                           "Blocked, Waiting for you, Incidents, Agenda, Moved, Decisions.",
            "done_criteria": "Every item of the tool's answer is either in the brief or deliberately "
                             "grouped; nothing in the brief is absent from the tool's answer.",
            "trigger": "cron",
            "schedule": "30 7 * * 1-5",
            "tools": [_BRIEF_TOOL],
            "auto_approve": [_BRIEF_TOOL],
            "mcp": ["sokkan-memory", "sokkan-board"],
            "secrets": [],
            "budget_usd": 0.2,
            "max_minutes": 10,
            "outputs": ["memory", "notify"],
            "notify_on": ["failure", "timeout", "budget", "success"],
        },
    },
}


def catalog(enabled=None) -> list[dict]:
    """Templates whose feature is on (`enabled(fid)`), without the filled fields."""
    out = []
    for tid, t in TEMPLATES.items():
        if t.get("feature") and enabled is not None and not enabled(t["feature"]):
            continue
        out.append({"id": tid, "label": t["label"], "description": t["description"],
                    "params": t["params"]})
    return out


def render(tid: str, person: str = "", team: str = "") -> dict | None:
    """The agent fields of a template for a person / a team (None = unknown template)."""
    t = TEMPLATES.get(tid)
    if t is None:
        return None
    f = copy.deepcopy(t["fields"])
    person, team = (person or "").strip().lower(), (team or "").strip()
    who = person or (f"team {team}" if team else "the whole project")
    f["purpose"] = f["purpose"].format(who=who, person=person, team=team)
    slug = (person.split("@")[0] if person else team.split(":")[-1] if team else "")
    slug = "".join(ch if ch.isalnum() else "-" for ch in slug.lower()).strip("-")[:24]
    if slug:
        f["name"] = f"{f['name']}-{slug}"
    return f
