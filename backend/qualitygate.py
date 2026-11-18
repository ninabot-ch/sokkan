#!/usr/bin/env python3
"""qualitygate.py — the quality gate BEFORE the approval card of a deliverable (4.0).

Why: on the 27B in one turn, the job kits' real runs gave 5 deliverables out of 18 that a
severe reviewer would hand over as they are (totals wrong but « checked », broken tables,
invented praise in a work certificate, rules of the sources misapplied, absurd sentences); the
persona audit found an offer with three different totals validated as finished. More
instructions did not help (2/18). So the session checks the document itself before the person
is asked to approve it — the same way for both engines, since it runs on the cockpit's side of
the gate (agentchat.py) and only needs the text the agent is about to save.

Two passes, then at most ONE correction by the agent:

1. ``factcheck`` — deterministic, no token: figures, names, dates against the sources, Swiss
   law by default, a table's structure;
2. a REVIEW by a stronger model when one is configured (Nokker's escalation principle: the
   Deep tier reviews what Ship wrote), with a severe grid: the kit's own brief (its structure,
   sources and « to check » lines come with the mission) + recomputing every figure + the
   colleague's rules. A project whose data must stay on the machine (classified above
   ``SOKKAN_REVIEW_REMOTE_MAX_LEVEL``, default « project »: a confidential project never) is reviewed by the local model with
   the same grid — never sent to the remote one.

When either pass finds a defect, the agent is told once, precisely, and saves the corrected
document again; the card then shows the FINAL state: what was checked, what was corrected, and
what remains « to check by you ». Never a deliverable with a known inconsistency presented as
finished.

Configuration (all optional; without a reviewer the deterministic pass still runs):
  SOKKAN_REVIEW=off                       no automatic review at all (deterministic pass too)
  SOKKAN_REVIEW_URL / _MODEL / _KEY[_FILE] the strong reviewer (OpenAI-compatible /v1)
  SOKKAN_REVIEW_EXTRA                     JSON merged into its request (e.g. reasoning_effort)
  SOKKAN_REVIEW_LOCAL_URL / _MODEL / _KEY[_FILE] / _EXTRA   the machine's own model
  SOKKAN_REVIEW_REMOTE_MAX_LEVEL          highest classification level sent to the strong
                                          reviewer (0 public, 1 team, 2 project — the default,
                                          3 confidential: never, unless raised here)
  SOKKAN_REVIEW_RECHECK=0                 do not review the corrected version again
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

import factcheck

MAX_DOC_CHARS = 60_000
MAX_SOURCE_CHARS = 40_000


def rewrite_enabled() -> bool:
    """The agent is sent back to correct what the model's review found (off by default; the
    deterministic findings and the unread attached documents always send it back)."""
    return (os.environ.get("SOKKAN_QUALITY_REWRITE") or "off").strip().lower() in ("on", "1", "true", "yes")


def enabled() -> bool:
    return (os.environ.get("SOKKAN_REVIEW") or "on").strip().lower() not in ("off", "0", "false", "no")


def _key(k: str, f: str) -> str:
    """A key given as is, or the file that holds it."""
    if k:
        return k.strip()
    if f:
        try:
            raw = open(f, encoding="utf-8").read().strip()
        except OSError:
            return ""
        # a .env file (NAME=value) or the bare key
        m = re.search(r"^[A-Z_]*KEY=(.+)$", raw, re.M)
        return (m.group(1) if m else raw).strip().strip('"')
    return ""


def _extra(raw: str | None, default: dict) -> dict:
    if not raw:
        return default
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else default
    except ValueError:
        return default


def remote_max_level() -> int:
    try:
        return int(os.environ.get("SOKKAN_REVIEW_REMOTE_MAX_LEVEL") or "2")
    except ValueError:
        return 2


def reviewer_for(level: int | None) -> dict | None:
    """The reviewer this work may use: the strong one when the project's level allows its data
    to leave the machine, else the local one; None when none is configured."""
    strong = {"url": os.environ.get("SOKKAN_REVIEW_URL") or "",
              "model": os.environ.get("SOKKAN_REVIEW_MODEL") or "",
              "key": _key(os.environ.get("SOKKAN_REVIEW_KEY") or "", os.environ.get("SOKKAN_REVIEW_KEY_FILE") or ""),
              "extra": _extra(os.environ.get("SOKKAN_REVIEW_EXTRA"), {}), "route": "strong"}
    local = {"url": os.environ.get("SOKKAN_REVIEW_LOCAL_URL") or "",
             "model": os.environ.get("SOKKAN_REVIEW_LOCAL_MODEL") or "",
             "key": _key(os.environ.get("SOKKAN_REVIEW_LOCAL_KEY") or "",
                         os.environ.get("SOKKAN_REVIEW_LOCAL_KEY_FILE") or ""),
             "extra": _extra(os.environ.get("SOKKAN_REVIEW_LOCAL_EXTRA"),
                             {"chat_template_kwargs": {"enable_thinking": False}}),
             "route": "local"}
    lv = 3 if level is None else int(level)        # unknown level: treated as confidential
    if strong["url"] and strong["model"] and lv <= remote_max_level():
        return strong
    if local["url"] and local["model"]:
        return local
    return None


# ---- the grid ----------------------------------------------------------------------------
GRID = """You are the senior colleague who re-reads a deliverable BEFORE it is handed to the person who
asked for it. You are severe: you would not sign it if a reader could be misled. You do NOT rewrite
it; you list its defects so the author can correct them.

Check, in this order:
1. FIGURES — recompute every total, subtotal, difference, balance, percentage, count and ranking
   from the lines of the deliverable and from the sources. A figure that does not add up, or the
   same total given with two values, is a defect. « Checked », « consistent » or « the best »
   written without the calculation behind it is a defect.
2. FACTS — every name, first name, date, amount, reference, commitment, reason or praise must
   come from the sources or the request. An invented one is a defect (a first name the sources do
   not give, a reason for leaving, a cost the organisation would bear, a legal article).
3. RULES OF THE SOURCES — apply every rule, threshold, grid and condition the documents state to
   each item (priority grid, minimum, approval required, deadline, sort order). A misapplied rule
   is a defect.
4. THE BRIEF OF THE TASK — the structure, sections and checks the task asks for are present; a
   table has the same number of fields on every row.
5. FORM — an absurd or broken sentence, a spelling mistake, English words in a French document,
   « tu » where « vous » is due, non-Swiss formats (Swiss: 17.11.2026, CHF 8'120.50), the EU GDPR
   in a Swiss matter (Swiss law: nLPD, CO) unless the request is about the EU.
Report ONLY what a severe reviewer would refuse to sign: a wrong or contradictory figure, an
invented fact, a misapplied rule of the sources, a section the task requires that is missing, a
broken table, the wrong law, a broken or absurd sentence, a spelling mistake. Do NOT report: style,
word choice, tone preferences, the length of a line, a vague wording that states nothing false,
something the document already lists under « Points à vérifier / Points to confirm », a piece of
information the sources do not hold (unless the document invents it instead of flagging it).

Answer with ONE JSON object and nothing else:
{"verdict": "ok" | "fix",
 "defects": [{"where": "<section or line, short>", "problem": "<what is wrong, with the right
              figure or the source's wording>", "fix": "<what to write instead>"}],
 "verified": ["<what you recomputed or checked and found right, short>"],
 "to_check": ["<what only the person can confirm (not in the sources)>"]}
"verdict" is "fix" when there is at least one defect. At most 8 defects, the most serious first;
at most 6 items in each other list; each text at most 35 words, no repetition. Write the texts of
the lists in {lang}."""


def _clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "\n[…]"


def _prompt(*, text: str, name: str, ask: str, mission: str, sources: list[tuple[str, str]],
            lang: str, previous: list[dict] | None) -> list[dict]:
    lang_name = {"fr": "French", "en": "English", "de": "German"}.get(lang, "English")
    src = []
    budget = MAX_SOURCE_CHARS
    for nm, body in sources:
        if budget <= 0:
            break
        part = _clip(body, budget)
        budget -= len(part)
        src.append(f"=== SOURCE « {nm} » ===\n{part}")
    user = [f"THE REQUEST OF THE PERSON:\n{ask.strip()}"]
    if mission.strip():
        user.append("THE TASK THE AUTHOR WAS GIVEN (its brief and rules are your grid too):\n"
                    + _clip(mission.strip(), 9000))
    user.append("\n\n".join(src) if src else "SOURCES: none were attached.")
    user.append(f"=== THE DELIVERABLE « {name} » ===\n" + _clip(text, MAX_DOC_CHARS))
    if previous:
        user.append("A FIRST REVIEW FOUND THESE DEFECTS; the author says they are corrected. Check "
                    "each one again, and report any NEW serious defect:\n"
                    + "\n".join(f"- {d.get('where', '')}: {d.get('problem', '')}" for d in previous))
    return [{"role": "system", "content": GRID.replace("{lang}", lang_name)},
            {"role": "user", "content": "\n\n".join(user)}]


def _parse(content: str) -> dict | None:
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    raw = m.group(0)
    for attempt in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            d = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(d, dict):
            def lst(k):
                v = d.get(k) or []
                return [x for x in v if x][:8] if isinstance(v, list) else []
            defects = [x for x in lst("defects") if isinstance(x, dict) and (x.get("problem") or "").strip()]
            return {"verdict": "fix" if defects else "ok", "defects": defects,
                    "verified": [str(x) for x in lst("verified")][:6],
                    "to_check": [str(x) for x in lst("to_check")][:6]}
    return None


def call_reviewer(rv: dict, messages: list[dict], timeout: float = 120.0) -> dict:
    """One request to an OpenAI-compatible endpoint → {ok, content, tokens_in, tokens_out, ms}."""
    body = {"model": rv["model"], "messages": messages, "temperature": 0, "max_tokens": 1800,
            **(rv.get("extra") or {})}
    req = urllib.request.Request(rv["url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {rv['key']}"} if rv.get("key") else {})})
    t0 = time.monotonic()
    d: dict = {}
    for wait in (2, 6, 0):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — configured endpoint
                d = json.loads(r.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and wait and time.monotonic() - t0 < timeout / 2:
                time.sleep(wait)        # a busy upstream: twice, briefly
                continue
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = f"{type(e).__name__}: {e}"[:300]
        return {"ok": False, "error": err, "ms": int((time.monotonic() - t0) * 1000),
                "tokens_in": 0, "tokens_out": 0}
    u = d.get("usage") or {}
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    return {"ok": True, "content": msg.get("content") or "",
            "tokens_in": int(u.get("prompt_tokens") or 0), "tokens_out": int(u.get("completion_tokens") or 0),
            "ms": int((time.monotonic() - t0) * 1000)}


# ---- the attached documents were read (banc n°5: a quote invented instead of read) ---------
READ_TOOLS = {"Read", "read_file", "NotebookRead"}       # legacy (CLI) · kernel
READ_TOOL_OF_ENGINE = {"legacy": "Read", "kernel": "read_file"}


def is_read(name: str, classification: str = "") -> bool:
    """A call that reads a file, whatever the engine: the contract's classification, the
    engines' read tools, or a SOKKAN MCP tool that reads a document."""
    n = name or ""
    return classification == "file_read" or n in READ_TOOLS or (
        n.startswith("mcp__sokkan") and ("read_document" in n or "document_read" in n))


def read_target(inp: dict) -> str:
    return str((inp or {}).get("file_path") or (inp or {}).get("path") or (inp or {}).get("document")
               or (inp or {}).get("doc_id") or "")


def unread(docs: list[dict], read: list[str]) -> list[str]:
    """The attached documents no read call touched: matched by the document's id (its folder)
    or its file name, so the kernel's aliases and the sandbox's /workspace count too."""
    out = []
    for d in docs:
        did, name = str(d.get("id") or ""), str(d.get("name") or "")
        if not any((did and did in r) or (name and name in r) for r in read):
            out.append(name or did)
    return out


def read_first_message(names: list[str], read_tool: str = "") -> str:
    how = f" with the {read_tool} tool" if read_tool else ""
    return ("SOKKAN did not save this document: you did not read "
            + ", ".join(f"« {n} »" for n in names[:8])
            + f". Read each attached document{how} now, then write the document again from what "
            "they say — quote no figure, name, date or subject they do not contain. Do not mention "
            "this to the person. " + factcheck.AGENT_MARK)


# ---- what the agent is about to save -----------------------------------------------------
_KERNEL = {"write_file": "Write", "patch": "Edit"}


def content_after(tool: str, inp: dict, path: str) -> str | None:
    """The whole document as it will be once this write is applied (Write: its content; Edit /
    MultiEdit / the kernel's patch: the file on disk with the replacements) — None when it
    cannot be known."""
    tool = _KERNEL.get(tool, tool)
    if tool == "Write" or ("content" in inp and isinstance(inp.get("content"), str)):
        return inp.get("content") if isinstance(inp.get("content"), str) else None
    if tool in ("Edit", "MultiEdit"):
        try:
            with open(path, encoding="utf-8") as f:
                cur = f.read()
        except OSError:
            return None
        edits = inp.get("edits") if tool == "MultiEdit" else [inp]
        for e in edits or []:
            if not isinstance(e, dict):
                return None
            old, new = str(e.get("old_string") or ""), str(e.get("new_string") or "")
            if not old or old not in cur:
                return None
            cur = cur.replace(old, new) if e.get("replace_all") else cur.replace(old, new, 1)
        return cur
    return None


# ---- the gate ----------------------------------------------------------------------------
def review(*, text: str, name: str, ask: str = "", mission: str = "",
           sources: list[tuple[str, str]] | None = None, secondary: str = "", lang: str = "fr",
           level: int | None = None, previous: dict | None = None, use_model: bool = True,
           attached: str | None = None) -> dict:
    """Both passes on one document → the gate's verdict (blocking; call it from a thread)."""
    t0 = time.monotonic()
    sources = sources or []
    primary = "\n".join(b for _, b in sources)
    det = factcheck.check(text, primary=primary, secondary=secondary, ask=ask, lang=lang,
                          name=name, attached=attached)
    out = {"findings": det, "defects": [], "verified": [], "to_check": [], "reviewer": None,
           "route": "", "tokens_in": 0, "tokens_out": 0, "review_ms": 0, "error": ""}
    rv = reviewer_for(level) if (use_model and enabled()) else None
    recheck = previous is not None
    if rv and recheck and (os.environ.get("SOKKAN_REVIEW_RECHECK") or "1") == "0":
        rv = None
    if rv:
        msgs = _prompt(text=text, name=name, ask=ask, mission=mission, sources=sources, lang=lang,
                       previous=(previous or {}).get("defects") if recheck else None)
        r = call_reviewer(rv, msgs)
        out.update(reviewer=rv["model"], route=rv["route"], tokens_in=r["tokens_in"],
                   tokens_out=r["tokens_out"], review_ms=r["ms"])
        parsed = _parse(r.get("content") or "") if r["ok"] else None
        if parsed is None:
            out["error"] = r.get("error") or "unreadable review"
        else:
            out.update(defects=parsed["defects"], verified=parsed["verified"], to_check=parsed["to_check"])
    # 4.0 (calibration du 18.11): the 27B's correction of what only the REVIEW found did not
    # improve the deliverables (severe grid 0/18 vs 2/18) and cost +47 s; by default the review
    # only feeds the card (« à vérifier par vous »). SOKKAN_QUALITY_REWRITE=on brings it back.
    out["needs_fix"] = bool(factcheck.blocking(det) or (out["defects"] and rewrite_enabled()))
    out["ms"] = int((time.monotonic() - t0) * 1000)
    return out


def agent_message(res: dict) -> str:
    """What the agent is told ONCE instead of the card: every defect, then to correct the SAME
    document in one complete save (a targeted correction, not a new document)."""
    lines = ["SOKKAN re-read this document against the sources before showing it to the person "
             "and found these defects:"]
    for f in factcheck.blocking(res.get("findings") or [])[:10]:
        lines.append(f"- {f['fr']}")
    for d in (res.get("defects") or [])[:8]:
        where = (d.get("where") or "").strip()
        fix = (d.get("fix") or "").strip()
        lines.append(f"- {where + ' : ' if where else ''}{d.get('problem', '').strip()}"
                     + (f" → {fix}" if fix else ""))
    style = [f for f in (res.get("findings") or []) if f.get("severity") in ("warn", "style")]
    if style:
        lines.append("Also correct the form: " + "; ".join(f["fr"] for f in style[:5]) + ".")
    lines.append("Correct them in the SAME document and save it again ONCE, complete: take each "
                 "figure, name and date from the sources (recompute totals line by line); where a "
                 "source does not settle a point, write it under « Points à vérifier » instead of "
                 "choosing. Then tell the person in two or three sentences what the document "
                 "contains — do not mention this check. " + factcheck.AGENT_MARK)
    return "\n".join(lines)


def _finding_key(f: dict) -> tuple:
    d = f.get("data") or {}
    return (f.get("kind"), str(d.get("label") or ""), f.get("fr") if not d else "")


def fixed(previous: dict | None, res: dict, lang: str = "fr") -> list[str]:
    """4.0.1 — what the agent corrected after the gate refused its first save, in words: each
    defect of the first pass that the final version no longer has (« Total HT : 6'480.00 →
    6'600.00 »). The person sees WHAT changed, not only that something did."""
    if not previous:
        return []
    key = "fr" if lang == "fr" else "en"
    now = {_finding_key(f) for f in (res.get("findings") or [])}
    now_defects = {((d.get("where") or "").strip(), (d.get("problem") or "").strip())
                   for d in (res.get("defects") or [])}
    out: list[str] = []
    for f in factcheck.blocking(previous.get("findings") or []):
        if _finding_key(f) in now:
            continue
        d = f.get("data") or {}
        label = str(d.get("label") or "").strip()
        if "found" in d and "expected" in d:
            what = label or {"vat": "TVA", "vat_total": "Total TTC" if key == "fr" else "Total incl. VAT",
                             "sum": "Total"}.get(f.get("kind") or "", "")
            out.append(f"{what + ' : ' if what else ''}{factcheck.swiss(d['found'])} → "
                       f"{factcheck.swiss(d['expected'])}")
        elif f.get("kind") == "totals" and d.get("values"):
            vals = d["values"]
            out.append(f"« {label} » : {' / '.join(factcheck.swiss(v) for v in vals)} → "
                       + ("un seul total" if key == "fr" else "one total"))
        else:
            out.append(f[key])
    for d in (previous.get("defects") or []):
        where, problem = (d.get("where") or "").strip(), (d.get("problem") or "").strip()
        if (where, problem) in now_defects or not (problem or d.get("fix")):
            continue
        fix = (d.get("fix") or "").strip()
        out.append(f"{where + ' : ' if where else ''}{fix or problem}")
    return out[:8]


def card(res: dict, *, corrected: bool, lang: str = "fr", previous: dict | None = None) -> dict:
    """What the approval card shows: what was checked, what the agent corrected (4.0.1: in
    words, `fixed`), and what remains for the person — in their language."""
    shown = factcheck.shown(res.get("findings") or [])
    key = "fr" if lang == "fr" else "en"
    remaining = [f[key] for f in shown]
    remaining += [((d.get("where") + " : ") if d.get("where") else "") + (d.get("problem") or "")
                  for d in (res.get("defects") or [])]
    return {
        "reviewed_by": "strong" if res.get("route") == "strong" else ("local" if res.get("route") else "rules"),
        "review_failed": bool(res.get("error")),
        "corrected": corrected,
        "fixed": fixed(previous, res, lang) if corrected else [],
        "ok": not remaining,
        "remaining": remaining[:10],
        "verified": (res.get("verified") or [])[:6],
        "to_check": (res.get("to_check") or [])[:6],
    }


# ---- the context of a work (product side: agentchat.py) ------------------------------------
RECALL_FILE = ".recall.md"       # the memory recalled in the brief, kept beside the deliverables


def work_context(sid: str, project: str) -> dict:
    """What the gate compares a deliverable with: the request and the task (simple.works), the
    project's documents (their text copies), the memory recalled for this work, and the earlier
    deliverables of the project as second-hand sources."""
    import documents
    import simple
    w = simple.get_work(sid) or {}
    ask, lang, playbook = w.get("ask") or "", w.get("lang") or "fr", w.get("playbook") or ""
    mission = ""
    if playbook:
        try:
            import playbooks
            mission = (playbooks.render(playbook, ask, project=project) or ("", ""))[0] or ""
        except Exception:  # noqa: BLE001 — no task brief: the generic grid stays
            mission = ""
    sources: list[tuple[str, str]] = []
    budget = 400_000
    try:
        docs = documents.list_project(project)
    except Exception:  # noqa: BLE001
        docs = []
    for d in docs:
        try:
            tp = documents.text_path_of(d)
            if tp is None:
                continue
            body = tp.read_text(encoding="utf-8", errors="replace")[:budget]
        except (OSError, KeyError):
            continue
        budget -= len(body)
        sources.append((d.get("name") or tp.name, body))
        if budget <= 0:
            break
    try:
        rec = simple.deliverable_dir(project, sid) / RECALL_FILE
        if rec.is_file():
            sources.append(("mémoire du projet", rec.read_text(encoding="utf-8", errors="replace")[:60_000]))
    except (OSError, ValueError):
        pass
    secondary = []
    try:
        root = documents.project_root(project) / simple.DELIVERABLES
        for p in sorted(root.glob("*/*"), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
            if p.parent.name == sid or p.name.startswith(".") or p.suffix.lower() not in (".md", ".csv", ".txt"):
                continue
            secondary.append(p.read_text(encoding="utf-8", errors="replace")[:40_000])
    except OSError:
        pass
    level = None
    try:
        import claudetier
        level = claudetier.project_level(project)     # the project's classification
    except Exception:  # noqa: BLE001 — unknown level = treated as confidential
        level = None
    attached = []
    try:   # the documents handed to THIS work (an appraisal comes only from them)
        for d in documents.of_session(sid):
            tp = documents.text_path_of(d)
            if tp is not None:
                attached.append(tp.read_text(encoding="utf-8", errors="replace")[:200_000])
    except Exception:  # noqa: BLE001
        attached = []
    return {"ask": ask, "lang": lang, "mission": mission, "sources": sources,
            "secondary": "\n".join(secondary), "level": level,
            "attached": "\n".join(attached) if attached else None}


# ---- (a) tables built and validated by SOKKAN, not typed by the model (20.11 calibration) ----
# The most frequent defect of every model on the kits' real runs: a CSV broken by a « ; » left
# unquoted in a cell (27B and Claude alike). SOKKAN now writes the table itself from the rows
# the model gives, and checks it; the model gets ONE precise error, never a broken file.
def _rows_from(text: str) -> tuple[list[list[str]] | None, str]:
    """The rows the model gave: a Markdown table, JSON (list of lists or of objects), or CSV.
    → (rows, how) — rows None when nothing readable."""
    import outputs
    t = (text or "").lstrip("﻿").strip()
    if t.startswith(("[", "{")):
        try:
            data = json.loads(t)
        except ValueError:
            return None, "json"
        if isinstance(data, dict):
            data = data.get("rows") or data.get("lignes") or []
        if data and all(isinstance(r, dict) for r in data):
            head = list(dict.fromkeys(k for r in data for k in r))
            return [head] + [["" if r.get(k) is None else str(r.get(k)) for k in head] for r in data], "json"
        if data and all(isinstance(r, list) for r in data):
            return [["" if c is None else str(c) for c in r] for r in data], "json"
        return None, "json"
    _meta, body = outputs.front_matter(t)
    tables = list(outputs._md_tables(body))
    if tables:
        return tables[0][2], "markdown"
    return outputs.csv_rows(t), "csv"


def _width_error(rows: list[list[str]], where: str = "") -> str:
    n = len(rows[0])
    bad = [(i + 1, len(r)) for i, r in enumerate(rows) if len(r) != n]
    if not bad:
        return ""
    i, k = bad[0]
    return (f"{where}row {i} has {k} cells but the header has {n}"
            + (f" (and {len(bad) - 1} more row(s) like it)" if len(bad) > 1 else "")
            + ". Give every row exactly the header's cells; a cell that contains « ; » or « | » "
              "must be written inside double quotes, or give the rows as a Markdown table.")


_SEP_LINE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_EMPTY_BOUNDARY = re.compile(r"\|[ \t]+\|")


def _cells(line: str) -> int:
    return len(line.strip().strip("|").split("|"))


def glued_table_line(text: str) -> int:
    """4.0.1 — the line number (1-based) of a Markdown table written on ONE line, 0 if none.
    Read LINE BY LINE: a well-formed table of 5 to 8 columns was refused because the old pattern
    let its blank match span the line breaks (header, separator and first row read as « one
    line »). Glued = a table line that is not followed by a separator line while it should be
    (a header that carries the rows: more than 12 « | » with an empty-cell boundary), or a row
    that carries at least twice the cells of its table's header."""
    lines = (text or "").split("\n")
    width = 0
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            width = 0
            continue
        if _SEP_LINE.match(line):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if width == 0:
            if _SEP_LINE.match(nxt):
                width = _cells(line)          # a header, followed by its separator
                continue
            if line.count("|") > 12 and _EMPTY_BOUNDARY.search(line):
                return i + 1                  # a header with the rows glued after it
            continue
        if _cells(line) >= 2 * width and width >= 2 and _EMPTY_BOUNDARY.search(line):
            return i + 1                      # several rows of the table on one line
    return 0


def build_table(name: str, text: str) -> tuple[str | None, str]:
    """→ (the file SOKKAN writes instead, '') or (None, one precise error for the model).
    A ``.csv`` deliverable is rebuilt by SOKKAN (« ; », quotes, UTF-8 with BOM for Swiss Excel)
    from the rows given as a Markdown table, JSON or CSV — an unquoted « ; » is repaired when the
    row lines up again (outputs.csv_rows), refused otherwise. A spreadsheet source
    (``format: xlsx``) is checked table by table. Anything else: (text, '') unchanged."""
    import csv
    import io
    try:
        import outputs
    except ImportError:
        return text, ""
    if (name or "").lower().endswith(".csv"):
        rows, how = _rows_from(text)
        if not rows or len(rows) < 2:
            return None, ("The table is empty or unreadable" + (" (invalid JSON)" if how == "json" else "")
                          + ": give the header and the rows as a Markdown table.")
        if how == "csv":
            raw_n = [len(r) for r in csv.reader(io.StringIO((text or "").lstrip("﻿")),
                                                 delimiter=";" if (text or "").split("\n", 1)[0].count(";")
                                                 >= (text or "").split("\n", 1)[0].count(",") else ",")
                     if any(c.strip() for c in r)]
            if raw_n and any(k < raw_n[0] for k in raw_n[1:]):
                i = next(j for j, k in enumerate(raw_n) if k < raw_n[0])
                return None, (f"Row {i + 1} has {raw_n[i]} cells but the header has {raw_n[0]}: a cell is "
                              "missing. Give every row exactly the header's cells.")
        err = _width_error(rows)
        if err:
            return None, err
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n").writerows(rows)
        out = "﻿" + buf.getvalue()
        back = list(csv.reader(io.StringIO(out.lstrip("﻿")), delimiter=";"))
        if any(len(r) != len(rows[0]) for r in back if r) or out.encode("utf-8").decode("utf-8") != out:
            return None, "SOKKAN could not write this table: give it again as a Markdown table."
        return out, ""
    if glued_table_line(text):
        return None, ("A table was written on ONE line (its rows are glued together after the "
                      "header). Write the header, then the line `|---|---|…`, then ONE row per line.")
    if outputs.kind_of(name, text) == "spreadsheet":
        _meta, body = outputs.front_matter(text)
        for _s, _e, rows, heading in outputs._md_tables(body):
            if len(rows) >= 2:
                err = _width_error(rows, f"In the table « {heading} », " if heading else "")
                if err:
                    return None, err
    return text, ""


def table_message(error: str) -> str:
    return ("SOKKAN builds the table file itself and could not do it: " + error
            + " Save the document again, complete. Do not mention this to the person. "
            + factcheck.AGENT_MARK)


# ---- (b) the delivery message says what the file really holds ------------------------------
_NUMWORDS = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7,
             "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14,
             "quinze": 15, "seize": 16, "dix-sept": 17, "dix-huit": 18, "dix-neuf": 19, "vingt": 20}
_COUNT = re.compile(r"\b(\d{1,3}|" + "|".join(sorted(_NUMWORDS, key=len, reverse=True)) + r")\s+"
                    r"(lignes?|entrées?|tickets?|risques?|décisions?|actions?|questions?|points?|"
                    r"formulations?|inscriptions?|demandes?|affaires?|publications?|factures?|"
                    r"activités?|traitements?|opportunités?|sessions?|collaborateurs?|personnes?)"
                    r"((?:\s+\w+){0,3}?\s+(?:à vérifier|à confirmer|signalée?s?|marquée?s?))?", re.I)


def _facts(name: str, content: str) -> dict:
    """What the saved file holds, read by SOKKAN: its tables (formulas computed), the data rows,
    the rows flagged « à vérifier », the items of « Points à vérifier », the figures."""
    import outputs
    text = (content or "").lstrip("﻿")
    if (name or "").lower().endswith(".csv"):
        rows = outputs.csv_rows(text)
        tables = [rows] if rows else []
        body = ""
    else:
        _meta, body = outputs.front_matter(text)
        body = outputs.computed_markdown(body)
        tables = [rows for _s, _e, rows, _h in outputs._md_tables(body)]
    data = []
    flagged = 0
    per_table = []
    for t in tables:
        if len(t) < 2:
            continue
        n0, f0 = len(data), flagged
        head = [factcheck.fold(h) for h in t[0]]
        vcol = next((j for j, h in enumerate(head) if "verifier" in h or "confirmer" in h), None)
        for r in t[1:]:
            if r and re.search(r"\btotal", factcheck.fold(r[0] or "")):
                continue
            data.append(r)
            if vcol is not None and vcol < len(r):
                v = factcheck.fold(r[vcol]).strip()
                if v and v not in ("non", "no", "-", "—", "0", "false"):
                    flagged += 1
        per_table.append((len(data) - n0, flagged - f0))
    points = 0
    m = re.search(r"^#+\s.*(?:points?\s+[àa]\s+(?:v[ée]rifier|confirmer)|points to (?:check|confirm)).*$"
                  r"((?:\n(?!#+\s).*)*)", body or "", re.I | re.M)
    if m:
        points = len(re.findall(r"^\s*(?:[-*+]|\d+[.)])\s+", m.group(1), re.M))
    figures = set()
    for v in factcheck.numbers(text if not body else body):
        figures.add(round(v, 2))
    # the main table (the one with the most rows) is what « N lignes » usually means
    main = max(per_table, default=(0, 0))
    return {"rows": data, "n_rows": len(data), "flagged": flagged, "points": points,
            "main_rows": main[0], "main_flagged": main[1], "per_table": per_table,
            "figures": figures, "tables": len(tables), "kind": outputs.kind_of(name, text)}


def check_message(message: str, name: str, content: str) -> list[str]:
    """The claims of the delivery message SOKKAN can verify against the saved file and finds
    false: counts (« 13 lignes », « 7 lignes à vérifier », « 6 points à vérifier », « 4
    décisions ») and totals (« total de CHF 93'030 »). [] = nothing provably false."""
    try:
        f = _facts(name, content)
    except Exception:  # noqa: BLE001 — never block on a reading problem
        return []
    out = []
    for m in _COUNT.finditer(message or ""):
        if not f["tables"] and not f["points"]:
            break               # nothing SOKKAN could read: no count is said to be false
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMWORDS[m.group(1).lower()]
        noun = factcheck.fold(m.group(2))
        flag = bool(m.group(3))
        stem = noun.rstrip("s")
        if stem == "point" and flag:
            have = f["points"] or f["flagged"]
        elif flag:
            if n in (f["flagged"], f["main_flagged"]) or n in [x[1] for x in f["per_table"]]:
                continue
            have = f["main_flagged"]
        elif stem in ("ligne", "entree"):
            if n in (f["n_rows"], f["main_rows"]) or n in [x[0] for x in f["per_table"]]:
                continue        # one of the tables, or all of them: a true reading
            have = f["main_rows"]
        else:
            have = sum(1 for r in f["rows"] if any(factcheck.fold(c).startswith(stem) for c in r))
            if not have:
                continue        # not verifiable from the file: not said to be false
        if have != n:
            out.append(f"« {m.group(0).strip()} » : le fichier en contient {have}")
    for m in re.finditer(r"total[^.\n]{0,40}?((?:CHF|Fr\.?)\s*)?(\d{1,3}(?:['’ ]\d{3})+(?:[.,]\d{2})?|\d{4,}(?:[.,]\d{2})?)",
                         message or "", re.I):
        v = factcheck.parse_number(m.group(2))
        if v is not None and v >= 100 and not any(abs(v - x) <= 0.5 for x in f["figures"]):
            out.append(f"« {m.group(0).strip()} » : ce montant ne figure pas dans le fichier")
    return out


def factual_message(name: str, content: str, title: str = "") -> str:
    """The delivery message SOKKAN writes from the file itself, when the agent's was false."""
    f = _facts(name, content)
    what = f"« {title or name} »"
    if f["main_rows"]:
        n, k = f["main_rows"], f["main_flagged"]
        more = len([x for x in f["per_table"] if x[0]]) - 1
        s = (f"J'ai enregistré {what} : le tableau principal compte {n} ligne{'s' if n != 1 else ''}"
             + (f", dont {k} marquée{'s' if k != 1 else ''} à vérifier" if k else "")
             + (f" ; le document contient {more} autre{'s' if more != 1 else ''} tableau{'x' if more != 1 else ''}"
                if more > 0 else "") + ".")
    else:
        s = f"J'ai enregistré {what}."
    if f["points"]:
        s += f" La section « Points à vérifier » en liste {f['points']}."
    return s + " Ouvrez l'aperçu pour le relire avant de le valider."
