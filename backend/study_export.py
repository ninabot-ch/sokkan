#!/usr/bin/env python3
"""study_export.py — the researcher's package of the `study_mode` feature.

One package per request (JSON, or a zip of CSV files + manifest): what the study protocol
asks for, and nothing that identifies a person.

* **Metadata only by default**: durations, validations / refusals / delays, corrections,
  tier served, recalled sources with their age, the two answers, the tags.
* **Content only with consent**: the text of the exchanges of a session is included only
  when its participant ticked the session's scenario (study.consents.content_scenarios) —
  the requests, the agent's answers, the tool calls it proposed; never what a tool returned
  (the text of the files it read), which leaves as a size.
* **Pseudonyms only**: every person is P1, P2… (study.participants, never exported); a
  person who is not a participant is `non-participant`. Sessions, projects and notes are
  keyed hashes (HMAC with a secret of the instance): stable inside a study, not reversible
  by trying candidates.
* Every free-text field that carries what a user typed or what a tool received
  (`recall_log.query`, `audit.detail`, `sessions.prompt` / title, tool title and input, a
  refusal's message, a question's answers, a note's name) is WITHHELD in metadata mode:
  replaced by its size and a keyed hash (`_withhold`). In content mode it goes through the
  secret masking (`hitl_journal.mask`: vault values then credential patterns) and the
  e-mail pass (`_depersonalize`) when the package is written.
* Last line of defence, `_guard`: the finished package is searched for anything that looks
  like an e-mail address and for every known person; a hit refuses the export (fail closed)
  rather than hand it over.

Only the sessions of a participant whose consent still holds are exported, and only those
tagged since that consent was given (a withdrawal takes everything collected under it out).
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import re
import statistics
import time
import zipfile

import audit
import hitl_journal
import study

FORMAT = "sokkan-study-export/1"
NON_PARTICIPANT = "non-participant"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


class ExportLeak(Exception):
    """The finished package still held something that identifies a person: not delivered."""


# ---- the filters ------------------------------------------------------------------------
def _withhold(text) -> dict:
    """METADATA MODE FILTER. A text that carries user content leaves the instance as its
    size and a keyed hash — never as text."""
    s = text if isinstance(text, str) else ("" if text is None else json.dumps(
        text, ensure_ascii=False, sort_keys=True, default=str))
    return {"chars": len(s), "hmac": study.keyed(s) if s else ""}


def _depersonalize(text: str, people: dict[str, str]) -> str:
    """CONTENT MODE: a known person's e-mail becomes their pseudonym, any other address
    `[email]` — the content a participant agreed to share names nobody."""
    if not text:
        return text

    def sub(m: re.Match) -> str:
        return people.get(m.group(0).lower(), "[email]")
    return _EMAIL_RE.sub(sub, text)


def _clean(obj, people: dict[str, str], secrets: dict[str, str] | None = None):
    """Content as it may be written: secrets masked, then nobody named."""
    obj = hitl_journal.mask(obj, secrets)
    if isinstance(obj, str):
        return _depersonalize(obj, people)
    if isinstance(obj, list):
        return [_clean(x, people) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean(v, people) for k, v in obj.items()}
    return obj


def _guard(package: dict, forbidden: list[str]) -> None:
    """Refuse a package that names somebody. `forbidden`: every e-mail the instance knows
    (and their local parts when long enough to be meaningful)."""
    blob = json.dumps(package, ensure_ascii=False, default=str)
    m = _EMAIL_RE.search(blob)
    if m:
        raise ExportLeak("an e-mail address reached the package (export refused)")
    low = blob.lower()
    for f in forbidden:
        if f and f.lower() in low:
            raise ExportLeak("a known person's identifier reached the package (export refused)")


# ---- helpers ----------------------------------------------------------------------------
def _iso(ts) -> str | None:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _sid(session_id: str) -> str:
    return "s_" + study.keyed("session:" + (session_id or ""), 12)


def _prj(project: str) -> str:
    return "prj_" + study.keyed("project:" + (project or ""), 8)


def _note(project: str, name: str) -> str:
    return "n_" + study.keyed(f"note:{project}:{name}", 12)


def _person(email: str, people: dict[str, str]) -> str:
    e = (email or "").lower().strip()
    if not e:
        return ""
    return people.get(e, NON_PARTICIPANT)


def _parse_dt(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime.datetime):
        d = v
    else:
        try:
            d = datetime.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                d = datetime.datetime.fromisoformat(str(v).strip()[:10])
            except ValueError:
                return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def _session_secrets(session_id: str) -> dict[str, str]:
    try:
        import agents
        return agents.session_secret_values(session_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def _scope(study_id: str, since: float | None, until: float | None) -> tuple[list[dict], dict]:
    """(exported tags, {pseudonym: consent}) — sessions of a holding consent, tagged since."""
    cons = {c["pseudonym"]: c for c in study.consents(study_id) if c["withdrawn_at"] is None}
    con = study._con()
    tags = [dict(r) for r in con.execute(
        "SELECT * FROM tags WHERE study_id = ? ORDER BY tagged_at", (study_id,))]
    con.close()
    out = []
    for t in tags:
        c = cons.get(t["pseudonym"])
        if c is None or t["tagged_at"] < c["given_at"]:
            continue
        if since is not None and t["tagged_at"] < since:
            continue
        if until is not None and t["tagged_at"] >= until:
            continue
        out.append(t)
    return out, cons


def _sources(tags: list[dict], content_ok: dict[str, bool], people: dict[str, str],
             notes: list[str]) -> list[dict]:
    """Recalled sources of the exported sessions, with the age of each note at the moment
    it was handed to the session (recall_log + note_access of the memory store)."""
    try:
        import store_backend
        if not store_backend.enabled():
            notes.append("sources: this instance's memory backend keeps no recall log")
            return []
        st = store_backend.get_store()
    except Exception as e:  # noqa: BLE001
        notes.append(f"sources: memory store unavailable ({type(e).__name__})")
        return []
    dates: dict[tuple[str, str], float | None] = {}

    def note_date(project: str, name: str) -> float | None:
        k = (project, name)
        if k not in dates:
            try:
                n = st.get_note(name, project)
                dates[k] = _parse_dt(getattr(n, "modified", None)) if n else None
            except Exception:  # noqa: BLE001
                dates[k] = None
        return dates[k]

    out = []
    for t in tags:
        sid = t["session_id"]
        ok = content_ok.get(sid, False)
        rows: list[tuple[str, dict]] = []
        try:
            rows += [("recall", r) for r in st.recall_log(session_id=sid, limit=2000)]
            seen = {(r.get("note_name"), str(r.get("at"))[:19]) for _k, r in rows}
            rows += [("access", r) for r in st.access_log(session_id=sid, limit=5000)
                     if (r.get("note_name"), str(r.get("at"))[:19]) not in seen]
        except Exception as e:  # noqa: BLE001
            notes.append(f"sources: unreadable for one session ({type(e).__name__})")
            continue
        for kind, r in rows:
            project = r.get("project") or "default"
            at = _parse_dt(r.get("at"))
            nd = note_date(project, r.get("note_name") or "")
            row = {"session": _sid(sid), "participant": t["pseudonym"],
                   "scenario": t["scenario"], "at": _iso(at), "log": kind,
                   "channel": r.get("channel") or r.get("via") or "",
                   "note": _note(project, r.get("note_name") or ""),
                   "note_modified": _iso(nd),
                   "note_age_days": None if (nd is None or at is None)
                   else round(max(0.0, at - nd) / 86400, 1),
                   "level": r.get("level"), "rank": r.get("rank"), "score": r.get("score"),
                   "rerank": r.get("rerank"),
                   "query": _withhold(r.get("query") or "")}
            if ok:
                row["content"] = {"note_name": _clean(r.get("note_name") or "", people),
                                  "query": _clean(r.get("query") or "", people)}
            out.append(row)
    out.sort(key=lambda x: (x["at"] or "", x["session"]))
    return out


def _events(tags: list[dict], content_ok: dict[str, bool], people: dict[str, str],
            since: float | None, until: float | None) -> list[dict]:
    """Actions of the instance's journal about the exported sessions (audit.events)."""
    by_res = {f"session:{t['session_id']}": t for t in tags}
    by_sid = {t["session_id"]: t for t in tags}
    if not tags:
        return []
    con = audit._con()
    lo = min(t["tagged_at"] for t in tags) - 3600
    rows = [dict(r) for r in con.execute(
        "SELECT ts, user, action, resource, detail, project FROM events WHERE ts >= ?"
        " ORDER BY ts", (max(lo, since or 0),))]
    con.close()
    out = []
    for r in rows:
        if until is not None and r["ts"] >= until:
            continue
        t = by_res.get(r["resource"]) or by_sid.get(r["resource"])
        if t is None:
            continue
        row = {"at": _iso(r["ts"]), "session": _sid(t["session_id"]),
               "participant": t["pseudonym"], "scenario": t["scenario"],
               "actor": _person(r["user"], people), "action": r["action"],
               "detail": _withhold(r["detail"] or "")}
        if content_ok.get(t["session_id"]):
            row["content"] = {"detail": _clean(r["detail"] or "", people,
                                               _session_secrets(t["session_id"]))}
        out.append(row)
    return out


def _content(tag: dict, people: dict[str, str]) -> dict | None:
    """The exchanges of ONE session whose participant agreed for its scenario: read from
    the transcript at export time, secrets masked, nobody named."""
    try:
        import agentchat
        import board
        import transcript
        csid = board.get_claude_session_id(tag["session_id"])
        path = agentchat._transcript_path(csid) if csid else None
        if not path or not path.exists():
            return {"session": _sid(tag["session_id"]), "messages": [],
                    "note": "no transcript on this instance"}
        msgs = transcript.parse_file(path).get("messages", [])
    except Exception as e:  # noqa: BLE001
        return {"session": _sid(tag["session_id"]), "messages": [],
                "note": f"transcript unreadable ({type(e).__name__})"}
    sec = _session_secrets(tag["session_id"])
    out = []
    for m in msgs:
        item = {"role": m.get("role"), "kind": m.get("kind")}
        if m.get("kind") == "tool":
            item.update(tool=m.get("tool"), title=m.get("title"), input=m.get("input"))
            if m.get("result"):
                # what a tool returned (the text of a file it read…) is never exported,
                # consent or not: documents hold other people's data — its size only
                item["result"] = {"chars": len(m["result"].get("text") or ""),
                                  "is_error": bool(m["result"].get("is_error"))}
        else:
            item["text"] = m.get("text", "")
        out.append(_clean(item, people, sec))
    return {"session": _sid(tag["session_id"]), "participant": tag["pseudonym"],
            "scenario": tag["scenario"], "messages": out}


def _delay_stats(delays: list[int]) -> dict:
    if not delays:
        return {"count": 0, "median_ms": None, "mean_ms": None, "max_ms": None}
    return {"count": len(delays), "median_ms": int(statistics.median(delays)),
            "mean_ms": int(statistics.fmean(delays)), "max_ms": max(delays)}


# ---- the package ------------------------------------------------------------------------
def build(study_id: str | None = None, *, since: float | None = None,
          until: float | None = None, by: str = "") -> dict:
    """Build the package of `study_id` (default: the instance's study). Raises ExportLeak
    when the result names somebody."""
    st = study.state()
    study_id = study_id or st["study_id"]
    if not study_id:
        raise study.StudyError(409, "no study on this instance")
    notes: list[str] = []
    tags, cons = _scope(study_id, since, until)
    corr = study.correspondence()
    people = {r["email"]: r["pseudonym"] for r in corr}
    content_ok = {t["session_id"]: bool(t["scenario"]) and t["scenario"]
                  in (cons[t["pseudonym"]]["content_scenarios"] or []) for t in tags}
    sids = [t["session_id"] for t in tags]
    by_sid = {t["session_id"]: t for t in tags}

    # sessions: project, title and prompt of board.sessions are user content
    board_rows: dict[str, dict] = {}
    try:
        import board
        board_rows = {s["session_id"]: s for s in board.list_sessions()}
    except Exception:  # noqa: BLE001
        notes.append("sessions: the board is unavailable (project and title left out)")

    con = study._con()
    q = ",".join("?" * len(sids)) or "''"
    turns_raw = [dict(r) for r in con.execute(
        f"SELECT * FROM turns WHERE session_id IN ({q}) ORDER BY started_at, id", sids)]
    fb_raw = {r["session_id"]: dict(r) for r in con.execute(
        f"SELECT * FROM feedback WHERE session_id IN ({q})", sids)}
    con.close()
    hitl_raw = hitl_journal.query(sessions=sids, limit=100000) if sids else []

    turns, validations = [], []
    per: dict[str, dict] = {s: {"turns": [], "hitl": []} for s in sids}
    counters: dict[str, int] = {}
    for r in turns_raw:
        t = by_sid[r["session_id"]]
        counters[r["session_id"]] = counters.get(r["session_id"], 0) + 1
        per[r["session_id"]]["turns"].append(r)
        turns.append({"session": _sid(r["session_id"]), "participant": t["pseudonym"],
                      "scenario": t["scenario"], "n": counters[r["session_id"]],
                      "started_at": _iso(r["started_at"]), "ended_at": _iso(r["ended_at"]),
                      "duration_ms": r["duration_ms"], "tier": r["model"],
                      "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
                      "is_error": bool(r["is_error"]),
                      "prompt": {"chars": r["prompt_chars"], "hmac": r["prompt_hmac"]}})
    for r in hitl_raw:
        t = by_sid[r["session_id"]]
        per[r["session_id"]]["hitl"].append(r)
        row = {"session": _sid(r["session_id"]), "participant": t["pseudonym"],
               "scenario": t["scenario"], "kind": r["kind"], "tool": r["tool"],
               "decision": r["decision"], "auto": bool(r["auto"]),
               "auto_reason": r["auto_reason"], "origin": r["origin"],
               "actor": _person(r["actor"], people), "approval_mode": r["mode"],
               "requested_at": _iso(r["requested_at"]), "decided_at": _iso(r["decided_at"]),
               "delay_ms": r["delay_ms"], "edited": bool(r["edited"]),
               "title": _withhold(r["title"]), "input": _withhold(r["input_summary"]),
               "message": _withhold(r["message"])}
        if content_ok.get(r["session_id"]):
            sec = _session_secrets(r["session_id"])
            row["content"] = {k: _clean(r[k] or "", people, sec)
                              for k in ("title", "input_summary", "edited_summary", "message")}
        validations.append(row)

    sessions, feedback = [], []
    for t in tags:
        sid = t["session_id"]
        tr, hi = per[sid]["turns"], per[sid]["hitl"]
        b = board_rows.get(sid, {})
        human = [h for h in hi if not h["auto"] and h["kind"] == "permission"]
        done = [h for h in tr if h["ended_at"] is not None]
        tiers: dict[str, int] = {}
        for h in tr:
            if h["model"]:
                tiers[h["model"]] = tiers.get(h["model"], 0) + 1
        count = lambda d: sum(1 for h in hi if h["decision"] == d)  # noqa: E731
        active_ms = sum(int(h["duration_ms"] or 0) for h in done)
        fb = fb_raw.get(sid)
        row = {
            "session": _sid(sid), "participant": t["pseudonym"], "scenario": t["scenario"],
            "reference_minutes": t["reference_minutes"], "tagged_by_hand": not t["auto"],
            "project": _prj(b.get("project") or ""),
            "tagged_at": _iso(t["tagged_at"]), "closed_at": _iso(t["closed_at"]),
            "first_turn_at": _iso(tr[0]["started_at"]) if tr else None,
            "last_turn_end_at": _iso(max((h["ended_at"] for h in done), default=None)),
            "agent_seconds": round(active_ms / 1000, 1),
            "elapsed_seconds": (round(max(h["ended_at"] for h in done) - tr[0]["started_at"], 1)
                                if done else None),
            "turns": len(tr), "followup_prompts": max(0, len(tr) - 1),
            "turn_errors": sum(1 for h in tr if h["is_error"]),
            "tokens_in": sum(h["tokens_in"] for h in tr),
            "tokens_out": sum(h["tokens_out"] for h in tr),
            "tiers_served": tiers,
            "validations": {
                "asked": len([h for h in hi if h["kind"] == "permission" and not h["auto"]]),
                "allowed": count("allow"), "denied": count("deny"), "edited": count("edit"),
                "questions_answered": count("answer"), "auto_allowed": count("auto_allow"),
                "auto_denied": count("auto_deny"), "cancelled": count("cancelled"),
                "interrupted": count("interrupted"), "mode_changes": count("mode_change"),
                "decision_delay": _delay_stats([int(h["delay_ms"]) for h in human
                                                if h["delay_ms"] is not None
                                                and h["decision"] in ("allow", "deny", "edit")]),
            },
            # corrections of the agent by the person: refused or rewritten actions
            "corrections": count("deny") + count("edit"),
            "feedback": None if fb is None else {"useful": fb["useful"],
                                                 "verify_minutes": fb["verify_minutes"],
                                                 "at": _iso(fb["created_at"])},
            "content_shared": content_ok.get(sid, False),
            "title": _withhold(b.get("title") or ""), "prompt": _withhold(b.get("prompt") or ""),
        }
        sessions.append(row)
        if fb is not None:
            feedback.append({"session": _sid(sid), "participant": t["pseudonym"],
                             "scenario": fb["scenario"] or t["scenario"], "useful": fb["useful"],
                             "verify_minutes": fb["verify_minutes"],
                             "reference_minutes": t["reference_minutes"],
                             "at": _iso(fb["created_at"])})

    sources = _sources(tags, content_ok, people, notes)
    events = _events(tags, content_ok, people, since, until)
    content = [c for c in (_content(t, people) for t in tags if content_ok[t["session_id"]])
               if c is not None]
    participants = [{"participant": p, "consent_given_at": _iso(c["given_at"]),
                     "consent_version": c["version"],
                     "content_scenarios": c["content_scenarios"]}
                    for p, c in sorted(cons.items(), key=lambda kv: int(kv[0][1:] or 0))]
    counts = {"participants": len(participants), "sessions": len(sessions), "turns": len(turns),
              "validations": len(validations), "sources": len(sources), "events": len(events),
              "feedback": len(feedback), "sessions_with_content": len(content)}
    try:
        from pathlib import Path
        version = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        version = ""
    package = {
        "manifest": {
            "format": FORMAT, "study_id": study_id, "generated_at": _iso(time.time()),
            "sokkan": version,
            "mode": "metadata+content" if content else "metadata",
            "period": {"since": _iso(since), "until": _iso(until)},
            "counts": counts, "notes": notes,
            "fields": {
                "withheld": "a text field carrying user content is {chars, hmac}: its size and "
                            "a keyed hash (equal texts = equal hash inside this study)",
                "content": "present only for the sessions whose participant agreed for the "
                           "scenario; secrets masked, addresses replaced",
                "identities": "P<n> = a participant; non-participant = anybody else; the "
                              "correspondence table stays on the instance",
            },
        },
        "participants": participants, "sessions": sessions, "turns": turns,
        "validations": validations, "sources": sources, "events": events,
        "feedback": feedback, "content": content,
    }
    forbidden = [r["email"] for r in corr]
    try:
        import iam
        forbidden += [u["email"] for u in iam.list_users()]
    except Exception:  # noqa: BLE001
        pass
    forbidden += [e.split("@", 1)[0] for e in list(forbidden)
                  if len(e.split("@", 1)[0]) >= 6 and "." in e.split("@", 1)[0]]
    _guard(package, sorted(set(forbidden)))
    digest = hashlib.sha256(json.dumps(package, sort_keys=True, default=str).encode()).hexdigest()
    package["manifest"]["sha256_of_payload"] = digest
    study.log_export(by, study_id, package["manifest"]["mode"], counts, digest)
    audit.log(by if "@" in by else "", "study.export", study_id,
              f"{package['manifest']['mode']}: {counts['sessions']} sessions, "
              f"{counts['validations']} validations" + ("" if "@" in by else f" ({by})"),
              project="")
    return package


# ---- zip of CSV files -------------------------------------------------------------------
def _flat(row: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in row.items():
        if k == "content":
            continue
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        elif isinstance(v, list):
            out[prefix + k] = " ".join(str(x) for x in v)
        else:
            out[prefix + k] = v
    return out


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v     # a spreadsheet must not read a cell as a formula
    return v


def _csv(rows: list[dict]) -> str:
    flat = [_flat(r) for r in rows]
    cols: list[str] = []
    for r in flat:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in flat:
        w.writerow([_cell(r.get(c)) for c in cols])
    return buf.getvalue()


README = """SOKKAN study export
===================
manifest.json      what this package is: study, period, counts, mode, field conventions
participants.csv   pseudonyms and what each agreed to (no name, no address)
sessions.csv       one line per tagged session: scenario, reference time, durations,
                   validations / refusals / delays, corrections, tiers served, the two answers
turns.csv          one line per request to the agent: duration, tier, tokens (never the text)
validations.csv    one line per approval asked and its decision (journal of validations)
sources.csv        notes recalled to the agent, with their age when recalled
events.csv         actions of the instance's journal about these sessions
feedback.csv       the two answers given at the end of a task
content/           exchanges of the sessions whose participant agreed for the scenario

A column ending in .chars / .hmac is a withheld text: its size and a keyed hash.
"""


def to_zip(package: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)
        z.writestr("manifest.json", json.dumps(package["manifest"], indent=2, ensure_ascii=False))
        for name in ("participants", "sessions", "turns", "validations", "sources", "events",
                     "feedback"):
            z.writestr(f"{name}.csv", _csv(package[name]))
        for c in package["content"]:
            z.writestr(f"content/{c['session']}.json", json.dumps(c, indent=2, ensure_ascii=False))
        extra = [r for name in ("validations", "sources", "events") for r in package[name]
                 if "content" in r]
        if extra:
            z.writestr("content/fields.json", json.dumps(
                {name: [{**{k: r[k] for k in ("session", "at", "requested_at") if k in r},
                         **r["content"]} for r in package[name] if "content" in r]
                 for name in ("validations", "sources", "events")}, indent=2, ensure_ascii=False))
    return buf.getvalue()
