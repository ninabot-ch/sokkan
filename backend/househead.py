#!/usr/bin/env python3
"""househead.py — 4.0: the stable context of a work of the Atelier (« the coworker knows the
house »), built once when its engine starts:

1. the organisation's profile (`orgprofile.block`);
2. the project's recently VALIDATED deliverables (continuity between works): a short list —
   title, who validated it and when, a summary, where to read the full text — so that a new
   work does not send reminders for invoices that the reconciliation of the same project shows
   paid, nor write « no document about this committee » when the minutes of its last meeting
   were validated a month ago (audit 17.11.2026, fin1/fin2 and sop4).

Both are bounded (`MAX_ITEMS`, `SUMMARY_CHARS`, `MAX_CHARS`): the list costs a few hundred
tokens, and the agent opens a full text only when it is about the same subject. Classification:
a deliverable is listed only when the person may read its level in the project (the highest
level of the documents its work read and of what its session obtained), and, for a work
written with Claude, at most the level Claude may receive.

The engines deliver the result as they can (harness/contract.RunSpec.head): the legacy engine
in its system prompt (a cached prefix), the kernel as the `project` block of the host contract.
"""
from __future__ import annotations

import re
import time

MAX_ITEMS = 6                  # validated deliverables listed
MAX_AGE_DAYS = 180
SUMMARY_CHARS = 320
MAX_CHARS = 4200               # the whole continuity block (≈ 1 000 tokens at most)


# ---- continuity ---------------------------------------------------------------------------
def _summary(text: str, n: int = SUMMARY_CHARS) -> tuple[str, list[str]]:
    """(the first sentences of the body, its section titles) of a Markdown deliverable."""
    heads, body = [], []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if m:
            if len(m.group(1)) >= 2 and len(heads) < 8:
                heads.append(re.sub(r"[*_`]", "", m.group(2)).strip()[:60])
            continue
        if re.fullmatch(r"[|:\-\s]+", s):
            continue
        s = re.sub(r"^[-*>]\s+|^\d+[.)]\s+", "", s)
        s = re.sub(r"\*\*|__|`", "", s).replace("|", " · ")
        body.append(" ".join(s.split()))
        if sum(len(x) + 1 for x in body) > n * 2:
            break
    out = " ".join(body)
    if len(out) > n:
        cut = out[:n]
        out = cut[: cut.rfind(" ")] if " " in cut[n // 2:] else cut
        out = out.rstrip(" ,;:·") + "…"
    return out, heads


def _level_of(sid: str) -> int:
    """The level of a work's deliverable: the highest of its documents and of its session."""
    import classification
    import documents
    lv = [2]
    try:
        lv += [int(d.get("level") if d.get("level") is not None else 2)
               for d in documents.of_session(sid)]
    except Exception:  # noqa: BLE001 — no document feature
        pass
    s = classification.session_level(sid)
    if s is not None:
        lv.append(int(s))
    return max(lv)


def _cap(sid: str, email: str, project: str) -> int | None:
    """The highest level this work may be shown (None = no filter: classification off)."""
    import classification
    cap = None
    if classification.enabled() and email:
        try:
            cap = classification.clearance(classification.user_for(email), project)
        except Exception:  # noqa: BLE001 — unknown identity: the default level
            cap = 2
        if cap is None:
            return -1                       # no access to the project: nothing
    try:
        import claudetier
        if claudetier.work_tier(sid)[0] == "claude":
            cap = min(cap if cap is not None else 4, claudetier.max_level())
    except Exception:  # noqa: BLE001
        pass
    return cap


def validated(project: str, *, exclude_sid: str = "", email: str = "", cap_sid: str = "",
              limit: int = MAX_ITEMS, now: float | None = None) -> list[dict]:
    """The project's deliverables whose CURRENT content is validated, newest validation first."""
    import board
    import simple
    now = time.time() if now is None else now
    con = simple._con()
    rows = con.execute(
        "SELECT v.sid, v.name, v.sha256, v.by, v.at, w.ask FROM validations v "
        "JOIN works w ON w.sid = v.sid WHERE w.project = ? AND v.sid <> ? AND v.at >= ? "
        "ORDER BY v.at DESC", (project, exclude_sid or "", now - MAX_AGE_DAYS * 86400)).fetchall()
    con.close()
    cap = _cap(cap_sid or exclude_sid, email, project)
    if cap == -1:
        return []
    titles = None
    out, seen = [], set()
    for r in rows:
        key = (r["sid"], r["name"])
        if key in seen:
            continue
        seen.add(key)
        try:
            p = simple.deliverable_path(project, r["sid"], r["name"])
        except (FileNotFoundError, ValueError):
            continue
        if simple._sha(p) != r["sha256"]:
            continue                        # changed since: not the validated text any more
        if cap is not None and _level_of(r["sid"]) > cap:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue
        import plain
        title = plain.title_from_markdown(text[:4000]) or plain.human_title(r["name"])
        summary, heads = _summary(text)
        if titles is None:
            try:
                titles = {x["session_id"]: x.get("title") or "" for x in board.list_sessions()}
            except Exception:  # noqa: BLE001
                titles = {}
        out.append({"sid": r["sid"], "name": r["name"], "title": title, "path": str(p),
                    "by": r["by"], "at": r["at"], "work": titles.get(r["sid"], ""),
                    "summary": summary, "sections": heads})
        if len(out) >= limit:
            break
    return out


def _who(email: str) -> str:
    try:
        import iam
        return iam.display_name(email) or email
    except Exception:  # noqa: BLE001
        return email


def continuity_block(project: str, *, sid: str, email: str = "", lang: str = "fr") -> str:
    items = validated(project, exclude_sid=sid, email=email)
    if not items:
        return ""
    lines = ["<sokkan-project>",
             "VALIDATED WORK OF THIS PROJECT — most recent first. A person of the project read and "
             "validated each one: it is the reference for what was decided, sent, paid or agreed."]
    for i, x in enumerate(items, 1):
        day = time.strftime("%d.%m.%Y", time.localtime(x["at"]))
        lines.append(f"{i}. « {x['title']} » — validated on {day} by {_who(x['by'])}"
                     + (f" (work « {x['work']} »)" if x["work"] and x["work"] != x["title"] else ""))
        if x["sections"]:
            lines.append("   Sections: " + " · ".join(x["sections"][:6]))
        if x["summary"]:
            lines.append(f"   Begins with: {x['summary']}")
        lines.append(f"   Full text: {x['path']}")
        if sum(len(s) + 1 for s in lines) > MAX_CHARS:
            lines = lines[:-3 if x["sections"] else -2]
            break
    lines += [
        "Before you write: when one of these concerns the same subject (the same client, invoice, "
        "offer, meeting, committee, person or document), READ its full text first and build on it "
        "(« as last time », the actions still open, the amounts already agreed). When the request "
        "contradicts it — an invoice it shows as paid, a decision it records, a date it fixes — do "
        "not follow blindly: say it to the person in one sentence and list it under « Points to "
        "confirm ».",
        "</sokkan-project>"]
    return "\n".join(lines)


# ---- the head of a work -------------------------------------------------------------------
def blocks(sid: str, *, email: str, project: str) -> list:
    """The head blocks (harness.contract.ContextBlock) of an Atelier work: the organisation,
    then the project's continuity. [] when there is nothing to say (or anything fails: a head
    never breaks a work)."""
    from harness.contract import ContextBlock
    out = []
    try:
        import simple
        w = simple.get_work(sid) or {}
    except Exception:  # noqa: BLE001
        w = {}
    try:
        import orgprofile
        # no person here: the same head for everyone of the project (one cached prefix);
        # who the work is for goes with its first message (orgprofile.person_line)
        txt = orgprofile.block(ask=w.get("ask") or "", playbook=w.get("playbook") or "")
        if txt:
            out.append(ContextBlock(text=txt, source="organisation"))
    except Exception as e:  # noqa: BLE001
        print(f"[sokkan] organisation block of {sid} skipped: {e!r}")
    try:
        txt = continuity_block(project or "default", sid=sid, email=email,
                               lang=w.get("lang") or "fr")
        if txt:
            out.append(ContextBlock(text=txt, source="project"))
    except Exception as e:  # noqa: BLE001
        print(f"[sokkan] continuity block of {sid} skipped: {e!r}")
    return out


def text_of(head) -> str:
    return "\n\n".join(b.text for b in (head or ()) if getattr(b, "text", ""))


def tokens(text: str) -> int:
    """A rough count (4 characters a token for French/English prose; the real count comes from
    the engine's usage, measured on real runs — see the report of the lot)."""
    return (len(text or "") + 3) // 4
