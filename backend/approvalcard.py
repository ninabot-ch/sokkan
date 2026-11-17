#!/usr/bin/env python3
"""approvalcard.py — what an approval card says, in the person's words (4.0, audit coworker).

The simple profile's card (« Votre accord est demandé ») read « L'agent veut effectuer une
action » with nothing else for anything that was not a document save, a note, a web search
or a sub-task: a marketing person approved, blind, a kanban card in the developers' board; a
project manager approved, blind, an automatic agent nobody could see afterwards. A person can
only decide what they can read. This module turns ONE gated call (either engine — the
contract's `gate.requested`, CLI or kernel tool names) into:

    kind      a word of the UI's dictionary (the headline, and what « Allow » / « Refuse » do)
    subject   the thing itself: the card's title, the agent's name, the mail's subject…
    where     where it lands (« Les livrables de ce travail », « Le tableau des tâches »…)
    facts     [{label, value}] — what the person needs to decide, already in their language
    diff      [{before, after}] — a change to an existing document, shown as such
    preview   a text to read before deciding (a note's body, a mail draft)

No tool name, path, command or identifier in `subject`, `where` or a fact of a call it knows —
the raw call stays in the folded technical detail. A tool this module does NOT know still gets
its readable name and the parameters it was given (the only way not to approve it blind),
never a bare « an action ». Pure functions: unit-tested
without a model (tests/test_approval_cards.py).
"""
from __future__ import annotations

import re

_MAX_FACT = 400
_MAX_DIFF = 3000
_MAX_PREVIEW = 20_000

_DAYS = {
    "fr": ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"],
    "en": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
}

L = {
    "fr": {
        "where": "Où", "column": "Colonne", "tag": "Étiquette", "description": "Description",
        "purpose": "Mission", "deliverable": "Ce qu'il rend", "when": "Quand",
        "model": "Modèle", "budget": "Budget par exécution", "query": "Recherche",
        "url": "Adresse", "to": "À", "subject": "Objet", "task": "Tâche confiée",
        "card": "Carte", "agent": "Agent", "change": "Changement", "start": "Début",
        "end": "Fin", "attendees": "Participants", "file": "Fichier", "about": "Résumé",
        "manual": "seulement quand quelqu'un le lance", "once": "une seule fois",
        "event": "à chaque alerte", "every_day": "chaque jour à {t}",
        "every_wd": "du lundi au vendredi à {t}", "every_dow": "chaque {d} à {t}",
        "every_month": "le {n} de chaque mois à {t}", "cron": "planification « {c} »",
        "deliverables": "Les livrables de ce travail", "project": " — projet « {p} »",
        "memory": "La mémoire du projet", "board": "Le tableau des tâches de l'équipe technique",
        "crew": "L'équipe d'agents automatiques",
        "crew_wait": " — en attente de l'approbation de la personne qui administre SOKKAN",
        "web": "Internet (recherche publique)", "mailbox": "Les brouillons de votre boîte mail",
        "calendar": "Votre agenda", "shared": "Vos dossiers partagés",
        "workfolder": "Le dossier de travail de ce travail", "service": "Le service « {s} »",
        "assistant": "Un assistant de l'agent, dans ce même travail",
        "a_run": "le lancer maintenant", "a_pause": "le mettre en pause", "a_resume": "le relancer",
        "a_archive": "l'archiver", "a_update": "modifier ses réglages",
        "c_update": "modifier la carte", "c_close": "clore la carte", "c_reopen": "rouvrir la carte",
        "c_archive": "archiver la carte", "c_comment": "commenter la carte",
        "c_link": "lier la carte", "default_model": "le modèle de l'instance",
        "lines": "{n} lignes",
    },
    "en": {
        "where": "Where", "column": "Column", "tag": "Label", "description": "Description",
        "purpose": "Mission", "deliverable": "What it hands back", "when": "When",
        "model": "Model", "budget": "Budget per run", "query": "Search",
        "url": "Address", "to": "To", "subject": "Subject", "task": "Task handed over",
        "card": "Card", "agent": "Agent", "change": "Change", "start": "Start",
        "end": "End", "attendees": "Attendees", "file": "File", "about": "Summary",
        "manual": "only when someone starts it", "once": "once",
        "event": "on every alert", "every_day": "every day at {t}",
        "every_wd": "Monday to Friday at {t}", "every_dow": "every {d} at {t}",
        "every_month": "on day {n} of every month at {t}", "cron": "schedule “{c}”",
        "deliverables": "The deliverables of this work", "project": " — project “{p}”",
        "memory": "The project's memory", "board": "The technical team's task board",
        "crew": "The team of automatic agents",
        "crew_wait": " — waiting for the approval of the person who administers SOKKAN",
        "web": "The internet (public search)", "mailbox": "The drafts of your mailbox",
        "calendar": "Your calendar", "shared": "Your shared folders",
        "workfolder": "This work's working folder", "service": "The “{s}” service",
        "assistant": "An assistant of the agent, in this same work",
        "a_run": "run it now", "a_pause": "pause it", "a_resume": "resume it",
        "a_archive": "archive it", "a_update": "change its settings",
        "c_update": "change the card", "c_close": "close the card", "c_reopen": "reopen the card",
        "c_archive": "archive the card", "c_comment": "comment on the card",
        "c_link": "link the card", "default_model": "the instance's model",
        "lines": "{n} lines",
    },
}


def _l(lang: str) -> dict:
    return L.get(lang) or L["fr"]


def _cut(v, n: int = _MAX_FACT) -> str:
    s = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def mcp_name(tool: str) -> tuple[str, str]:
    """(server, tool) of an MCP tool in either naming — ``mcp__srv__tool`` (CLI) or
    ``srv__tool`` (kernel) — else ("", tool)."""
    t = tool[5:] if tool.startswith("mcp__") else tool
    if "__" in t:
        srv, _, name = t.partition("__")
        return srv, name
    return "", tool


def readable_tool(name: str) -> str:
    """« create_dashboard » → « Create dashboard »: the last resort, a name a person can read."""
    n = re.sub(r"[_\-]+", " ", name or "").strip()
    n = re.sub(r"([a-z])([A-Z])", r"\1 \2", n)
    return (n[:1].upper() + n[1:].lower()) if n else "?"


def human_schedule(trigger: str, schedule: str = "", lang: str = "fr") -> str:
    """The trigger of an agent in words: « chaque vendredi à 16:00 »; a cron that does not fit
    the common shapes is shown as it is (better than nothing, never hidden)."""
    t = _l(lang)
    trigger = (trigger or "manual").strip()
    if trigger in ("manual", "once", "event"):
        return t[trigger]
    m = re.fullmatch(r"(\d{1,2})\s+(\d{1,2})\s+(\S+)\s+\*\s+(\S+)", (schedule or "").strip())
    if not m:
        return t["cron"].format(c=schedule or "?")
    minute, hour, dom, dow = m.groups()
    at = f"{int(hour):02d}:{int(minute):02d}"
    if dom == "*" and dow == "*":
        return t["every_day"].format(t=at)
    if dom == "*" and dow in ("1-5", "MON-FRI", "mon-fri"):
        return t["every_wd"].format(t=at)
    if dom == "*" and re.fullmatch(r"[0-7]", dow):
        return t["every_dow"].format(d=_DAYS.get(lang, _DAYS["fr"])[int(dow) % 7], t=at)
    if dow == "*" and re.fullmatch(r"\d{1,2}", dom):
        return t["every_month"].format(n=int(dom), t=at)
    return t["cron"].format(c=schedule)


def _facts(*pairs) -> list[dict]:
    return [{"label": k, "value": _cut(v)} for k, v in pairs if v not in (None, "", [], {})]


def _changed(before: str, after: str) -> tuple[str, str]:
    """Only the lines that change: an agent replaces a whole section to change one sentence
    (seen on the real 27B) — the person reads that sentence, not the section twice."""
    a, b = before.splitlines(), after.splitlines()
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[len(a) - 1 - j] == b[len(b) - 1 - j]:
        j += 1
    return ("\n".join(a[i:len(a) - j]).strip("\n"), "\n".join(b[i:len(b) - j]).strip("\n")) \
        if (i or j) else (before, after)


def diff_of(tool: str, inp: dict) -> list[dict]:
    """A change to an existing document: what goes, what comes (Edit / MultiEdit, the
    kernel's patch with the same fields)."""
    if tool == "MultiEdit":
        edits = inp.get("edits") or []
    elif isinstance(inp.get("old_string"), str) or isinstance(inp.get("new_string"), str):
        edits = [inp]
    else:
        return []
    out = []
    for e in edits[:20]:
        if not isinstance(e, dict):
            continue
        before, after = _changed(str(e.get("old_string") or ""), str(e.get("new_string") or ""))
        if before or after:
            out.append({"before": before[:_MAX_DIFF], "after": after[:_MAX_DIFF]})
    return out


def describe(tool: str, inp: dict, base: dict, lang: str = "fr", project: str = "") -> dict:
    """The card of one gated call. ``base`` is plain.View.classify's answer for it (kind,
    subject, deliverable); what this adds: a precise kind when there is one, where, facts,
    diff, preview. The result always has a kind the UI has a sentence for."""
    t = _l(lang)
    inp = inp if isinstance(inp, dict) else {}
    kind = base.get("kind") or "other"
    proj = t["project"].format(p=project) if project and project != "default" else ""
    out: dict = {"kind": kind, "subject": base.get("subject") or "", "where": "", "facts": [],
                 "diff": [], "preview": ""}
    srv, name = mcp_name(tool)

    if kind == "write_draft":
        out["where"] = t["deliverables"] + proj
        out["diff"] = diff_of(tool if tool in ("Edit", "MultiEdit") else "Edit", inp) \
            if tool not in ("Write", "write_file") else []
        return out
    if kind == "write_file":
        out["where"] = t["workfolder"]
        content = inp.get("content")
        if isinstance(content, str):
            out["facts"] = _facts((t["about"], t["lines"].format(n=content.count("\n") + 1)))
        out["diff"] = diff_of(tool if tool in ("Edit", "MultiEdit") else "Edit", inp)
        return out
    if kind == "save_memory":
        out["where"] = t["memory"] + proj
        out["facts"] = _facts((t["about"], inp.get("description")))
        out["preview"] = str(inp.get("body") or inp.get("content") or "")[:_MAX_PREVIEW]
        return out
    if kind == "web_search":
        out["where"] = t["web"]
        out["facts"] = _facts((t["query"], inp.get("query")))
        return out
    if kind == "web_read":
        out["where"] = t["web"]
        out["facts"] = _facts((t["url"], inp.get("url")))
        return out
    if kind == "delegate":
        out["where"] = t["assistant"]
        out["facts"] = _facts((t["task"], inp.get("prompt") or inp.get("goal")
                               or inp.get("description")))
        return out

    # ---- the Cockpit's servers (an Atelier work no longer has them: never blind anyway) ----
    if srv == "sokkan-board":
        if name == "create_card":
            out.update(kind="board_card", subject=_cut(inp.get("title"), 120), where=t["board"])
            out["facts"] = _facts((t["column"], inp.get("bucket") or "Backlog"),
                                  (t["tag"], inp.get("tag")),
                                  (t["description"], inp.get("description")))
            return out
        verb = {"update_card": "c_update", "close_card": "c_close", "reopen_card": "c_reopen",
                "archive_card": "c_archive", "comment_card": "c_comment",
                "link_card": "c_link"}.get(name)
        out.update(kind="board_change", where=t["board"],
                   subject=_cut(inp.get("title") or (f"#{inp['card_id']}" if inp.get("card_id")
                                                     else ""), 120))
        out["facts"] = _facts((t["change"], t[verb] if verb else readable_tool(name)),
                              (t["description"], inp.get("comment") or inp.get("body")
                               or inp.get("description")))
        return out
    if srv == "sokkan-agents":
        if name in ("create_agent", "update_agent"):
            out.update(kind="create_agent" if name == "create_agent" else "agent_change",
                       subject=_cut(inp.get("name") or inp.get("agent") or "", 120),
                       where=t["crew"] + proj + (t["crew_wait"] if name == "create_agent"
                                                  else ""))
            budget = inp.get("budget_usd")
            out["facts"] = _facts(
                (t["purpose"], inp.get("purpose")),
                (t["deliverable"], inp.get("deliverable")),
                (t["when"], human_schedule(inp.get("trigger") or "", inp.get("schedule") or "",
                                           lang) if (inp.get("trigger") or name == "create_agent")
                 else ""),
                (t["model"], inp.get("model") or (t["default_model"] if name == "create_agent"
                                                  else "")),
                (t["budget"], f"{float(budget):.2f} USD" if budget else ""))
            return out
        verb = {"run_agent_now": "a_run", "pause_agent": "a_pause", "resume_agent": "a_resume",
                "archive_agent": "a_archive"}.get(name)
        out.update(kind="agent_change", where=t["crew"] + proj,
                   subject=_cut(inp.get("agent") or inp.get("name") or inp.get("ref") or "", 120))
        out["facts"] = _facts((t["change"], t[verb] if verb else readable_tool(name)))
        return out
    if srv == "sokkan-connectors":
        if name == "mail_draft":
            to = inp.get("to") or []
            out.update(kind="mail_draft", subject=_cut(inp.get("subject") or "", 160),
                       where=t["mailbox"])
            out["facts"] = _facts((t["to"], ", ".join(to) if isinstance(to, list) else to),
                                  (t["subject"], inp.get("subject")))
            out["preview"] = str(inp.get("body") or "")[:_MAX_PREVIEW]
            return out
        if name == "calendar_propose_event":
            att = inp.get("attendees") or []
            out.update(kind="calendar_event", subject=_cut(inp.get("subject") or "", 160),
                       where=t["calendar"])
            out["facts"] = _facts((t["start"], inp.get("start")), (t["end"], inp.get("end")),
                                  (t["attendees"], ", ".join(att) if isinstance(att, list) else att))
            return out
        if name == "files_propose_write":
            out.update(kind="shared_file", subject=_cut(str(inp.get("path") or "").rsplit("/", 1)[-1], 160),
                       where=t["shared"])
            out["preview"] = str(inp.get("content") or "")[:_MAX_PREVIEW]
            return out

    if kind in ("run_command",):
        return out

    # ---- anything else: its readable name and what it was given, never « an action » -------
    out["kind"] = "tool"
    out["subject"] = readable_tool(name if srv else tool)
    if srv:
        out["where"] = t["service"].format(s=srv.removeprefix("sokkan-"))
    pairs = []
    for k, v in list(inp.items())[:6]:
        if isinstance(v, (str, int, float, bool)) and str(v).strip():
            pairs.append((readable_tool(k), v))
        elif isinstance(v, list) and v and all(isinstance(x, (str, int, float)) for x in v):
            pairs.append((readable_tool(k), ", ".join(map(str, v[:10]))))
    out["facts"] = _facts(*pairs)
    return out
