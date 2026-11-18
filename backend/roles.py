#!/usr/bin/env python3
"""roles.py — the job kits of SOKKAN Atelier (« Postes »): data, not code.

A person who arrives picks their job (executive assistant, HR, finance, sales…) and finds,
from the first minute, work ready to start, a team memory to fill in and the guard-rails of
that job. Everything a kit says lives in ``backend/roles/*.json`` (one file per job) and
``backend/roles/briefs.json`` (one brief per type of deliverable); this module only loads,
checks and renders it. A self-hoster adds a job by dropping a file in the folder named by
``SOKKAN_ROLES_DIR`` (read after the built-in one; same id = replaces it).

* a **job** (`role`): label and description (fr, en), the project classification it suggests
  (`level`), whether its work is about people (`people`: the « PII sandwich » card is shown before
  Claude even when nothing personal was spotted), its guard-rails (what the person reads, and the rule the agent is given), 6-10 task
  templates and 3-5 starter notes. Whether a work may be written with Claude is decided by the
  classification of its project (backend/claudetier.py), never by the job (4.0, decision of 18.11).
* a **task template**: what to attach, what the agent produces, the deliverable's format
  (`docx` = a Markdown file downloaded as Word, `xlsx` = a `format: xlsx` source rendered as Excel by `outputs`,
  `md` = text), the points a human must check, the mission given to the agent, and the TYPE of
  deliverable (`brief`) whose quality brief is added to the mission.
* a **starter note**: a memory note « to fill in with your team » (charter, tone, house
  templates) — written into a project's memory when the job is set up there, with its blanks
  marked « [À compléter : …] » / « [To fill: …] »; the agent never fills a blank by guessing.

The deliverable brief travels inside the work's first message (``simple.brief`` → the turn's
text), which is what every engine of the harness contract receives: the legacy CLI and the
kernel get the same words. Template ids are ``<job>.<template>``; `playbooks.get()` resolves
them, so a start through ``/api/simple/start`` needs nothing else.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

BUILTIN = Path(__file__).with_name("roles")
LANGS = ("fr", "en")
FORMATS = ("docx", "xlsx", "md")
USES = ("mail", "calendar", "files")   # connector families a template's work touches
LEVELS = ("public", "team", "project", "confidential", "restricted")
_ID = re.compile(r"[a-z][a-z0-9-]{1,40}")
_NOTE = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
_BLANK = {"fr": "[À compléter", "en": "[To fill"}
_lock = threading.Lock()
_cache: dict = {}


class CatalogError(ValueError):
    """A kit file that does not hold together (named with the file and the field)."""


# ---- loading -------------------------------------------------------------------------------
def _dirs() -> list[Path]:
    out = [BUILTIN]
    extra = (os.environ.get("SOKKAN_ROLES_DIR") or "").strip()
    if extra:
        out.append(Path(extra))
    return out


def _stamp() -> tuple:
    st = []
    for d in _dirs():
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                try:
                    st.append((str(p), p.stat().st_mtime_ns))
                except OSError:
                    pass
    return tuple(st)


def load(force: bool = False) -> dict:
    """{"briefs": {type: brief}, "roles": {id: role}} — checked; re-read when a file changed."""
    stamp = _stamp()
    with _lock:
        if not force and _cache.get("stamp") == stamp:
            return _cache["data"]
        briefs: dict = {}
        roles: dict = {}
        for d in _dirs():
            if not d.is_dir():
                continue
            bp = d / "briefs.json"
            if bp.is_file():
                briefs.update(_read(bp).get("types") or {})
            for p in sorted(d.glob("*.json")):
                if p.name == "briefs.json":
                    continue
                r = _read(p)
                roles[r.get("id", p.stem)] = {**r, "_file": p.name}
        data = {"briefs": briefs, "roles": roles}
        problems = check(data)
        if problems:
            raise CatalogError("; ".join(problems[:10]))
        _cache.update(stamp=stamp, data=data)
        return data


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CatalogError(f"{p.name}: {e}") from e
    if not isinstance(d, dict):
        raise CatalogError(f"{p.name}: an object is expected")
    return d


# ---- checking ------------------------------------------------------------------------------
def _texts(where: str, i18n, keys: tuple[str, ...], out: list[str], lists: tuple[str, ...] = ()) -> None:
    if not isinstance(i18n, dict):
        out.append(f"{where}: i18n missing")
        return
    for lang in LANGS:
        t = i18n.get(lang)
        if not isinstance(t, dict):
            out.append(f"{where}: no {lang} text")
            continue
        for k in keys:
            if not isinstance(t.get(k), str) or not t[k].strip():
                out.append(f"{where}: {lang}.{k} missing")
        for k in lists:
            v = t.get(k)
            if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
                out.append(f"{where}: {lang}.{k} must be a non-empty list of sentences")
    if all(isinstance(i18n.get(lang), dict) for lang in LANGS):
        a, b = set(i18n["fr"]), set(i18n["en"])
        if a != b:
            out.append(f"{where}: fr and en differ ({', '.join(sorted(a ^ b))})")
        for k in lists:
            if isinstance(i18n["fr"].get(k), list) and isinstance(i18n["en"].get(k), list) \
                    and len(i18n["fr"][k]) != len(i18n["en"][k]):
                out.append(f"{where}: {k} has {len(i18n['fr'][k])} fr and {len(i18n['en'][k])} en items")


def check(data: dict) -> list[str]:
    """Every problem of a catalog, as sentences (empty = valid)."""
    out: list[str] = []
    briefs = data.get("briefs") or {}
    if not briefs:
        out.append("briefs.json: no deliverable type")
    for bid, b in briefs.items():
        w = f"briefs.json/{bid}"
        if not _ID.fullmatch(bid):
            out.append(f"{w}: bad id")
        for k in ("structure", "length", "sources", "verify"):
            v = b.get(k)
            if k == "structure":
                if not isinstance(v, list) or len(v) < 2 or not all(isinstance(x, str) for x in v):
                    out.append(f"{w}: structure must list at least two parts")
            elif not isinstance(v, str) or len(v.strip()) < 10:
                out.append(f"{w}: {k} missing")
        if b.get("format") not in FORMATS:
            out.append(f"{w}: format one of {FORMATS}")
        _texts(w, b.get("i18n"), ("label",), out)
    seen_notes: dict[str, str] = {}
    for rid, r in (data.get("roles") or {}).items():
        w = r.get("_file") or rid
        if not _ID.fullmatch(rid or ""):
            out.append(f"{w}: bad id {rid!r}")
        if r.get("level") not in LEVELS:
            out.append(f"{w}: level one of {LEVELS}")
        if not isinstance(r.get("people", False), bool):
            out.append(f"{w}: people must be true or false")
        _texts(w, r.get("i18n"), ("label", "description", "project"), out)
        gr = r.get("guardrails")
        if not isinstance(gr, list) or not 2 <= len(gr) <= 8:
            out.append(f"{w}: 2-8 guardrails")
        else:
            for i, g in enumerate(gr):
                if not isinstance(g.get("agent"), str) or len(g["agent"].strip()) < 10:
                    out.append(f"{w}: guardrail {i}: the agent's rule is missing")
                _texts(f"{w}/guardrail {i}", g.get("i18n"), ("text",), out)
        tpls = r.get("templates")
        if not isinstance(tpls, list) or not 6 <= len(tpls) <= 10:
            out.append(f"{w}: 6-10 templates (has {len(tpls) if isinstance(tpls, list) else 0})")
            tpls = tpls if isinstance(tpls, list) else []
        ids = set()
        for t in tpls:
            tw = f"{w}/{t.get('id')}"
            if not _ID.fullmatch(t.get("id") or ""):
                out.append(f"{tw}: bad id")
            if t.get("id") in ids:
                out.append(f"{tw}: duplicate id")
            ids.add(t.get("id"))
            if t.get("brief") not in briefs:
                out.append(f"{tw}: brief must be one of {sorted(briefs)}")
            if t.get("format") not in FORMATS:
                out.append(f"{tw}: format one of {FORMATS}")
            elif t.get("brief") in briefs and briefs[t["brief"]].get("format") == "xlsx" \
                    and t["format"] != "xlsx":
                out.append(f"{tw}: a table brief is an xlsx deliverable")
            u = t.get("uses", [])
            if not isinstance(u, list) or not set(u) <= set(USES):
                out.append(f"{tw}: uses must list some of {USES}")
            m = t.get("mission")
            if not isinstance(m, str) or len(m.strip()) < 80:
                out.append(f"{tw}: mission too short")
            elif "{subject}" not in m:
                out.append(f"{tw}: the mission must carry {{subject}}")
            else:
                try:
                    m.format(subject="")
                except (KeyError, IndexError, ValueError):
                    out.append(f"{tw}: the mission has a stray brace")
            _texts(tw, t.get("i18n"), ("label", "description", "hint", "attach", "produces",
                                       "deliverable"), out, lists=("checks",))
        notes = r.get("notes")
        if not isinstance(notes, list) or not 3 <= len(notes) <= 5:
            out.append(f"{w}: 3-5 starter notes")
            notes = notes if isinstance(notes, list) else []
        for n in notes:
            nw = f"{w}/note {n.get('name')}"
            if not _NOTE.fullmatch(n.get("name") or ""):
                out.append(f"{nw}: bad name")
            elif n["name"] in seen_notes and seen_notes[n["name"]] != rid:
                out.append(f"{nw}: name already used by {seen_notes[n['name']]}")
            seen_notes[n.get("name") or ""] = rid
            _texts(nw, n.get("i18n"), ("title", "description", "body"), out)
            for lang in LANGS:
                body = ((n.get("i18n") or {}).get(lang) or {}).get("body") or ""
                if _BLANK[lang] not in body:
                    out.append(f"{nw}: {lang} body has no blank « {_BLANK[lang]} : … »")
    return out


# ---- reading -------------------------------------------------------------------------------
def _lang(lang: str) -> str:
    return lang if lang in LANGS else "en"


def ids() -> list[str]:
    return [r["id"] for r in _ordered()]


def _ordered() -> list[dict]:
    return sorted(load()["roles"].values(), key=lambda r: (r.get("order", 100), r["id"]))


def get(role_id: str) -> dict | None:
    return load()["roles"].get(role_id)


def valid(role_ids) -> list[str]:
    """The known ids among ``role_ids``, in catalog order, without duplicates."""
    want = set(role_ids or [])
    return [i for i in ids() if i in want]


def brief_of(brief_id: str) -> dict | None:
    return load()["briefs"].get(brief_id)


def template(tid: str) -> tuple[dict, dict] | None:
    """``<job>.<template>`` → (job, template) or None."""
    if not isinstance(tid, str) or tid.count(".") != 1:
        return None
    rid, t = tid.split(".")
    r = get(rid)
    if r is None:
        return None
    for x in r["templates"]:
        if x["id"] == t:
            return r, x
    return None


def public_template(r: dict, t: dict, lang: str) -> dict:
    """What the Atelier shows of a template (the shape of `playbooks.catalog('simple')` plus
    the kit's own fields)."""
    lang = _lang(lang)
    tx = t["i18n"][lang]
    b = brief_of(t["brief"]) or {}
    return {"id": f"{r['id']}.{t['id']}", "tag": "docs", "label": tx["label"],
            "description": tx["description"], "hint": tx["hint"], "deliverable": tx["deliverable"],
            "attach": tx["attach"], "produces": tx["produces"], "checks": list(tx["checks"]),
            "format": t["format"], "kind": ((b.get("i18n") or {}).get(lang) or {}).get("label", ""),
            "role": r["id"], "role_label": r["i18n"][lang]["label"],
            "uses": list(t.get("uses") or [])}


def public(r: dict, lang: str, with_templates: bool = True) -> dict:
    lang = _lang(lang)
    tx = r["i18n"][lang]
    out = {"id": r["id"], "label": tx["label"], "description": tx["description"],
           "project": tx["project"], "level": r["level"], "people": bool(r.get("people")),
           "guardrails": [g["i18n"][lang]["text"] for g in r["guardrails"]],
           "notes": [{"name": n["name"], "title": n["i18n"][lang]["title"],
                      "description": n["i18n"][lang]["description"]} for n in r["notes"]],
           "templates_count": len(r["templates"])}
    if with_templates:
        out["templates"] = [public_template(r, t, lang) for t in r["templates"]]
    return out


def catalog(lang: str = "fr") -> list[dict]:
    return [public(r, lang) for r in _ordered()]


def templates_for(role_ids, lang: str = "fr") -> list[dict]:
    """The templates of the person's jobs, job after job, in the order given."""
    out = []
    for rid in role_ids or []:
        r = get(rid)
        if r:
            out += [public_template(r, t, lang) for t in r["templates"]]
    return out


# ---- what the agent is told ----------------------------------------------------------------
def deliverable_brief(brief_id: str, fmt: str) -> str:
    """The quality brief of one type of deliverable, in English (the agent's language of
    instructions; the brief of the work tells it which language to write in)."""
    b = brief_of(brief_id)
    if not b:
        return ""
    lines = ["WHAT A GOOD DELIVERABLE OF THIS KIND LOOKS LIKE (" + brief_id + ")",
             "- Structure, in this order: " + "; ".join(b["structure"]) + ".",
             "- Length: " + b["length"].strip(),
             "- Sources: " + b["sources"].strip(),
             "- To check: " + b["verify"].strip()]
    if fmt == "xlsx":
        lines += [
            "- FORMAT: this deliverable is an EXCEL file the person opens (SOKKAN builds it, "
            "`outputs`): save ONE file in the same folder that starts with the three lines `---`, "
            "`format: xlsx`, `---`, then `# Title`; each `## Heading` is a sheet, each Markdown table "
            "a block of rows, the first row of a table holds the column headers, EVERY row has as "
            "many cells as the header. A total, a balance or any figure derived from others is a "
            "FORMULA (`=SUM(ABOVE)`, `=[Montant]-[Payé]`), never a number you computed: SOKKAN "
            "computes it and gives you the result. A value the documents do not give stays an "
            "EMPTY cell (no « [à définir] », no « non documenté »). The points to check go in your "
            "message to the person, not in the table."]
    return "\n".join(lines)


def mission(tid: str, subject: str) -> str | None:
    """The full mission of a kit template: the job's context, the template's own mission, the
    deliverable brief and the job's rules — None for an unknown id."""
    found = template(tid)
    if found is None:
        return None
    r, t = found
    subject = (subject or "").strip()
    rules = "\n".join(
        ["RULES OF THIS JOB (never broken):"]
        + [f"- {g['agent'].strip()}" for g in r["guardrails"]]
        + ["- A memory note of the team may still hold blanks marked « [À compléter : …] » or "
           "« [To fill: …] »: never fill a blank by guessing — say in « Points to confirm » what "
           "the team still has to write in that note."])
    checks = "; ".join(t["i18n"]["en"]["checks"])
    if t["format"] == "xlsx":
        closing = ("- After saving the table, list in your message to the person what a human must "
                   "check before it is used: " + checks + ".")
    else:
        closing = ("- End the document with a section « Points to confirm » (in the person's "
                   "language: « Points à vérifier ») listing, as a checklist, what a human must "
                   "check before the document is used: " + checks + ".")
    quality = "\n".join([
        "HOW A GOOD COLLEAGUE WORKS (before you save):",
        "- List the rules, thresholds and conditions the documents state (a priority grid, a "
        "minimum or maximum, an approval required, a deadline, a sort order asked for) and apply "
        "each one to every item; say in your message which ones you applied.",
        "- Every total, difference and ratio is a FORMULA in a table (SOKKAN computes it and "
        "gives you the result — quote only that); rank from those results. Compare them with "
        "any total the document gives: a difference is a finding to report, never something to "
        "smooth over. "
        "Never write « checked », « consistent » or « the best » without the calculation behind it.",
        "- When the person's request or a document decides a point, apply the decision; keep "
        "« [à définir] » for what nobody decided.",
        "- The document is written for its reader: no working note, no reference to your table "
        "or to these instructions inside a letter, a certificate or a text for a third party.",
        "- Proofread spelling and grammar in the person's language before saving; no English word "
        "in a French document (signatures, labels).",
        "- Your final message describes the file exactly as saved (number of rows, order, what "
        "is flagged): re-read it if needed; never announce something the file does not contain.",
    ])
    parts = [
        f"You work as a capable colleague in the « {r['i18n']['en']['label']} » job of a small "
        "Swiss organisation. Write the way a good Swiss French-speaking professional does: "
        "formal « vous », precise, sober, no superlatives, Swiss formats (dates 17.11.2026, "
        "amounts CHF 1'250.00) unless the documents use others.",
        t["mission"].format(subject=subject).strip(),
        deliverable_brief(t["brief"], t["format"]),
        quality,
        rules + "\n" + closing,
    ]
    return "\n\n".join(p for p in parts if p)


# What the agent is told about the mailbox / calendar a template's work would touch (« uses »),
# from what the connectors really allow for this person and project — no false promise: a
# reminder or an invitation is never « sent » by a work that cannot send it.
_CONNECTOR_RULES = {
    ("mail", "off"): "The person's mailbox is NOT connected to SOKKAN: you can neither read their "
                     "e-mails nor write in their mailbox nor send anything. Save the text as the "
                     "deliverable, ready to copy, and say in one sentence that the person sends it "
                     "themselves. Never write that a message was sent, will be sent or is scheduled.",
    ("mail", "read"): "The person's mailbox is connected (tools sokkan-connectors): you may read it "
                      "and leave a DRAFT in it (mail_draft). Sending from SOKKAN is switched off by "
                      "the administrator: the person sends the draft themselves; never write that "
                      "it was sent.",
    ("mail", "act"): "The person's mailbox is connected (tools sokkan-connectors): you may leave a "
                     "DRAFT in it (mail_draft) and propose sending it (mail_propose_send). Nothing "
                     "leaves before the person approves the card in Settings › Connectors: say "
                     "« draft ready, sending awaits your approval », never « sent ».",
    ("calendar", "off"): "The person's calendar is NOT connected to SOKKAN: you can neither see "
                         "their availability nor create or send an invitation. Write the date and "
                         "time as given; the person creates the event themselves. Never write that "
                         "an invitation was sent or an event created.",
    ("calendar", "read"): "The person's calendar is readable (calendar_events, calendar_free_slots) "
                          "but no event can be created from SOKKAN: the person creates it "
                          "themselves; never write that an invitation was sent.",
    ("calendar", "act"): "The person's calendar is connected: you may propose the event "
                         "(calendar_propose_event); it is created only after the person approves "
                         "the card in Settings › Connectors — never write that it was sent before.",
}


def connector_rules(tid: str, caps: dict[str, str]) -> str:
    """The lines for the families a template uses (``caps``: connectors.service.capabilities)."""
    found = template(tid)
    if found is None:
        return ""
    lines = [_CONNECTOR_RULES.get((f, caps.get(f, "off")), "") for f in found[1].get("uses") or []]
    lines = [x for x in lines if x]
    return ("MAILBOX AND CALENDAR (what this work can really do):\n" + "\n".join(f"- {x}" for x in lines)
            ) if lines else ""


def uses_of(tid: str) -> list[str]:
    found = template(tid)
    return list(found[1].get("uses") or []) if found else []


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 2


# ---- the starter notes -----------------------------------------------------------------------
def note_file(n: dict, lang: str, role_id: str) -> str:
    """A starter note as a memory file: the standard header plus `status: to-fill` (the team
    has not written it yet) and the job it comes from."""
    lang = _lang(lang)
    tx = n["i18n"][lang]
    desc = " ".join(tx["description"].split())
    return (f"---\nname: {n['name']}\ndescription: >-\n  {desc}\ntitle: {json.dumps(tx['title'], ensure_ascii=False)}\n"
            f"metadata:\n  type: team\n  status: to-fill\n  role: {role_id}\n---\n\n"
            f"# {tx['title']}\n\n{tx['body'].strip()}\n")


def seed_notes(role_id: str, mem_dir: Path, lang: str) -> list[str]:
    """Write the job's starter notes into ``mem_dir``; a note that already exists (the team
    may have filled it) is never overwritten. → the names written."""
    r = get(role_id)
    if r is None:
        raise KeyError(role_id)
    mem_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for n in r["notes"]:
        f = mem_dir / f"{n['name'].replace('-', '_')}.md"
        if f.exists() or (mem_dir / f"{n['name']}.md").exists():
            continue
        f.write_text(note_file(n, lang, role_id), encoding="utf-8")
        written.append(n["name"])
    return written


if __name__ == "__main__":   # python3 backend/roles.py check
    import sys
    try:
        d = load(force=True)
    except CatalogError as e:
        print("INVALID:", e)
        sys.exit(1)
    n = sum(len(r["templates"]) for r in d["roles"].values())
    print(f"OK: {len(d['roles'])} jobs, {n} templates, {len(d['briefs'])} deliverable types")
