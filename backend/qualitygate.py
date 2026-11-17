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
   ``SOKKAN_REVIEW_REMOTE_MAX_LEVEL``, default « team ») is reviewed by the local model with
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
                                          reviewer (0 public, 1 team, 2 project, 3 confidential)
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


def enabled() -> bool:
    return (os.environ.get("SOKKAN_REVIEW") or "on").strip().lower() not in ("off", "0", "false", "no")


def _key(prefix: str) -> str:
    k = os.environ.get(prefix + "_KEY") or ""
    if k:
        return k.strip()
    f = os.environ.get(prefix + "_KEY_FILE") or ""
    if f:
        try:
            raw = open(f, encoding="utf-8").read().strip()
        except OSError:
            return ""
        # a .env file (NAME=value) or the bare key
        m = re.search(r"^[A-Z_]*KEY=(.+)$", raw, re.M)
        return (m.group(1) if m else raw).strip().strip('"')
    return ""


def _extra(prefix: str, default: dict) -> dict:
    raw = os.environ.get(prefix + "_EXTRA")
    if raw is None:
        return default
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else default
    except ValueError:
        return default


def remote_max_level() -> int:
    try:
        return int(os.environ.get("SOKKAN_REVIEW_REMOTE_MAX_LEVEL", "1"))
    except ValueError:
        return 1


def reviewer_for(level: int | None) -> dict | None:
    """The reviewer this work may use: the strong one when the project's level allows its data
    to leave the machine, else the local one; None when none is configured."""
    strong = {"url": os.environ.get("SOKKAN_REVIEW_URL") or "",
              "model": os.environ.get("SOKKAN_REVIEW_MODEL") or "",
              "key": _key("SOKKAN_REVIEW"), "extra": _extra("SOKKAN_REVIEW", {}), "route": "strong"}
    local = {"url": os.environ.get("SOKKAN_REVIEW_LOCAL_URL") or "",
             "model": os.environ.get("SOKKAN_REVIEW_LOCAL_MODEL") or "",
             "key": _key("SOKKAN_REVIEW_LOCAL"),
             "extra": _extra("SOKKAN_REVIEW_LOCAL", {"chat_template_kwargs": {"enable_thinking": False}}),
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
Do not report: a matter of taste, something the document already lists under « Points à
vérifier / Points to confirm », a missing piece of information the sources do not hold (only
report it if the document invents it instead of flagging it).

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
           level: int | None = None, previous: dict | None = None, use_model: bool = True) -> dict:
    """Both passes on one document → the gate's verdict (blocking; call it from a thread)."""
    t0 = time.monotonic()
    sources = sources or []
    primary = "\n".join(b for _, b in sources)
    det = factcheck.check(text, primary=primary, secondary=secondary, ask=ask, lang=lang,
                          name=name)
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
    out["needs_fix"] = bool(factcheck.blocking(det) or out["defects"])
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


def card(res: dict, *, corrected: bool, lang: str = "fr") -> dict:
    """What the approval card shows: what was checked, what the agent corrected, and what
    remains for the person — in their language."""
    shown = factcheck.shown(res.get("findings") or [])
    key = "fr" if lang == "fr" else "en"
    remaining = [f[key] for f in shown]
    remaining += [((d.get("where") + " : ") if d.get("where") else "") + (d.get("problem") or "")
                  for d in (res.get("defects") or [])]
    return {
        "reviewed_by": "strong" if res.get("route") == "strong" else ("local" if res.get("route") else "rules"),
        "review_failed": bool(res.get("error")),
        "corrected": corrected,
        "ok": not remaining,
        "remaining": remaining[:10],
        "verified": (res.get("verified") or [])[:6],
        "to_check": (res.get("to_check") or [])[:6],
    }
