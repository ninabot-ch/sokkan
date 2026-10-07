#!/usr/bin/env python3
"""helm.py — SOKKAN 3.3 "Helm": steering projects with hierarchical cards.

A manager's PROJECT card holds the intent, the constraints and the decisions; the
engineers' cards sit under it, their sub-tasks under them (board.parent_id). Three
mechanisms, all derived, never declared by hand:

* context flows DOWN — a session spawned from a card receives the intent, constraints,
  decisions and links of every card above it (`context_block`, injected with the memory
  pre-seed) and that context is a project memory note `helm-card-<id>` marked `card:<id>`;
* progress flows UP — a parent's state is computed from its descendants (done / in
  progress / waiting for approval / blocked) and from the live signals of their links
  (sessions working, agent runs, merge requests, Operate incidents). Recomputed on every
  board event (board.on_change), by the periodic job, and on every read;
* Helm suggests, a manager decides — drift from the parent's intent (embedding of the
  memory engine), work that contradicts a recorded decision, scope that grows past the
  baseline, cards nobody owns, a project that slows down or races ahead, a linked
  incident. Each suggestion waits for Approve (→ a "reframe" card) or Ignore; nothing
  is ever applied on its own.

Plus the Helm view's data (deck, filters, activity, costs) and the morning brief.
Spec: docs/HELM.md.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import board

TZ = ZoneInfo("Europe/Zurich")
STATES = ("todo", "in_progress", "waiting", "blocked", "done")
STATE_LABELS = {"todo": "To do", "in_progress": "In progress", "waiting": "Waiting for approval",
                "blocked": "Blocked", "done": "Done"}
SUGGESTION_KINDS = ("drift", "contradiction", "scope", "unparented", "unassigned", "slowing",
                    "racing", "incident")
HELM_ROLES = ("maintainer", "admin")       # project roles that steer (the "managers")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def drift_min() -> float:
    """Cosine under which a child card is said to drift from its parent's intent."""
    return _f("SOKKAN_HELM_DRIFT_MIN", 0.25)


def snooze_s() -> float:
    """An ignored (or approved) suggestion is not proposed again for this long."""
    return _f("SOKKAN_HELM_SNOOZE_DAYS", 7) * 86400


def tick_s() -> float:
    return max(60.0, _f("SOKKAN_HELM_TICK_S", 900))


# ---- storage (tables of their own, in board.db) ----------------------------------------
_init_lock = threading.Lock()
_initialized_for: str | None = None


def init(force: bool = False) -> None:
    global _initialized_for
    with _init_lock:
        if _initialized_for == str(board.DB) and not force:
            return
        con = board._con()
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS helm_rollup (
                card_id INTEGER PRIMARY KEY, state TEXT NOT NULL, progress REAL DEFAULT 0,
                counts TEXT DEFAULT '{}', updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS helm_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL, card_id INTEGER, target_id INTEGER,
                kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
                title TEXT NOT NULL, detail TEXT DEFAULT '', evidence TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                decided_by TEXT DEFAULT '', decided_at REAL, reframe_card_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS ix_helm_sugg ON helm_suggestions(project, status);
            CREATE INDEX IF NOT EXISTS ix_helm_sugg_fp ON helm_suggestions(fingerprint);
            CREATE TABLE IF NOT EXISTS helm_calendars (
                principal TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'ics',
                secret TEXT NOT NULL, updated_at REAL
            );
            """
        )
        con.commit()
        con.close()
        _initialized_for = str(board.DB)


def _con() -> sqlite3.Connection:
    init()
    return board._con()


# ---- access ---------------------------------------------------------------------------
def can_steer(user: dict, project: str) -> bool:
    """Helm (the view, suggestions) = the project's maintainers/admins ("managers"), and
    the instance admins IN the projects they belong to (decision of 07.10: an instance
    admin sees no project content without being added to it)."""
    import projects
    role = projects.effective_role(user, project)
    if role in HELM_ROLES:
        return True
    return role is not None and user.get("role") in ("admin", "owner")


def steerable_projects(user: dict) -> list[str]:
    import projects
    return [p["slug"] for p in projects.work_projects() if can_steer(user, p["slug"])]


# ---- signals (links of a card, resolved live) ----------------------------------------
_RUN_ACTIVE = ("queued", "running")
_RUN_FAILED = ("failed", "timeout", "budget", "incomplete")


def _session_working(sid: str) -> bool:
    try:
        import agentchat
        s = agentchat.peek(sid)
        return bool(s and getattr(s, "_busy", False))
    except Exception:  # noqa: BLE001
        return False


def _run(rid: int) -> dict | None:
    try:
        import agents
        return agents.get_run(int(rid))
    except Exception:  # noqa: BLE001
        return None


def _agent(aid: int) -> dict | None:
    try:
        import agents
        return agents.get(int(aid))
    except Exception:  # noqa: BLE001
        return None


def _agent_runs(aid: int, limit: int = 5) -> list[dict]:
    try:
        import agents
        con = agents._con()
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC LIMIT ?", (int(aid), limit))]
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []


def _incident(iid: int) -> dict | None:
    try:
        import observability
        con = observability._con()
        r = con.execute("SELECT id, title, status, severity, ts FROM incidents WHERE id=?",
                        (int(iid),)).fetchone()
        con.close()
        return dict(r) if r else None
    except Exception:  # noqa: BLE001
        return None


def _agent_incidents(aid: int) -> list[dict]:
    try:
        import observability
        con = observability._con()
        rows = [dict(r) for r in con.execute(
            "SELECT id, title, status, severity, ts FROM incidents WHERE agent_id=? AND"
            " status != 'resolved'", (int(aid),))]
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []


def _raw_links(card_id: int) -> list[dict]:
    con = board._con()
    rows = [dict(r) for r in con.execute(
        "SELECT kind, ref FROM card_links WHERE card_id=? ORDER BY ts, id", (card_id,))]
    con.close()
    return rows


def card_signals(card: dict) -> dict:
    """Live signals of ONE card's own links (not its children)."""
    out = {"sessions": [], "sessions_working": 0, "runs": [], "runs_active": 0,
           "runs_failed": 0, "runs_waiting": 0, "mrs": [], "incidents": [],
           "agents_pending": 0}
    links = _raw_links(card["id"])
    sids = [r["ref"] for r in links if r["kind"] == "session"]
    if card.get("session_id") and card["session_id"] not in sids:
        sids.insert(0, card["session_id"])
    for sid in sids:
        working = _session_working(sid)
        out["sessions"].append({"session_id": sid, "working": working})
        out["sessions_working"] += int(working)
    runs: list[dict] = []
    for r in links:
        if r["kind"] == "run":
            x = _run(int(r["ref"])) if str(r["ref"]).isdigit() else None
            if x:
                runs.append(x)
        elif r["kind"] == "agent" and str(r["ref"]).isdigit():
            a = _agent(int(r["ref"]))
            if a:
                if a.get("status") == "pending" or a.get("pending_change"):
                    out["agents_pending"] += 1
                last = _agent_runs(a["id"], 1)
                runs += [x for x in last if x["id"] not in {y["id"] for y in runs}]
                for inc in _agent_incidents(a["id"]):
                    out["incidents"].append({**inc, "via": f"agent {a['name']}"})
        elif r["kind"] == "mr":
            out["mrs"].append(r["ref"])
        elif r["kind"] == "incident" and str(r["ref"]).lstrip("#").isdigit():
            inc = _incident(int(str(r["ref"]).lstrip("#")))
            if inc and inc.get("status") != "resolved":
                out["incidents"].append(inc)
    for x in runs:
        st = x.get("status")
        out["runs"].append({"id": x["id"], "status": st, "agent_id": x.get("agent_id"),
                            "cost_usd": x.get("cost_usd") or 0})
        out["runs_active"] += int(st in _RUN_ACTIVE)
        out["runs_failed"] += int(st in _RUN_FAILED)
        out["runs_waiting"] += int(bool(x.get("waiting_approval")) and st in _RUN_ACTIVE)
    return out


def leaf_state(card: dict, sig: dict | None = None) -> str:
    """State of a card without children, from its column and its live signals."""
    if card.get("bucket") == "Done":
        return "done"
    sig = sig if sig is not None else card_signals(card)
    if sig["incidents"] or sig["runs_failed"]:
        return "blocked"
    if card.get("bucket") == "Review" or sig["runs_waiting"] or sig["agents_pending"]:
        return "waiting"
    if card.get("bucket") == "Doing" or sig["sessions_working"] or sig["runs_active"]:
        return "in_progress"
    return "todo"


# ---- roll-up: progress flows up -------------------------------------------------------
def rollup(card_id: int) -> dict | None:
    """Aggregated state of a card from its whole subtree. Never declared: a parent's
    own column is ignored as soon as it has children."""
    card = board.get_card(card_id)
    if card is None:
        return None
    desc = board.descendants(card_id)
    with_kids = {d["parent_id"] for d in desc}
    counts = {s: 0 for s in STATES}
    sig_all = {"sessions": 0, "sessions_working": 0, "runs": 0, "runs_active": 0,
               "runs_failed": 0, "mrs": [], "incidents": []}
    per_card: dict[int, str] = {}
    for c in [card] + desc:
        sig = card_signals(c)
        sig_all["sessions"] += len(sig["sessions"])
        sig_all["sessions_working"] += sig["sessions_working"]
        sig_all["runs"] += len(sig["runs"])
        sig_all["runs_active"] += sig["runs_active"]
        if c["bucket"] != "Done":
            sig_all["runs_failed"] += sig["runs_failed"]
        sig_all["mrs"] += [{"card_id": c["id"], "url": u} for u in sig["mrs"]]
        sig_all["incidents"] += [{**i, "card_id": c["id"]} for i in sig["incidents"]]
        if c["id"] != card_id and c["id"] not in with_kids:
            per_card[c["id"]] = leaf_state(c, sig)
        if c["id"] == card_id and not desc:
            per_card[c["id"]] = leaf_state(c, sig)
    for s in per_card.values():
        counts[s] += 1
    total = sum(counts.values())
    if not desc:
        state = per_card[card_id]
    elif total and counts["done"] == total:
        state = "done"
    elif counts["blocked"] or sig_all["incidents"] or sig_all["runs_failed"]:
        state = "blocked"
    elif counts["waiting"]:
        state = "waiting"
    elif counts["in_progress"] or counts["done"] or sig_all["sessions_working"] \
            or sig_all["runs_active"]:
        state = "in_progress"
    else:
        state = "todo"
    working = sig_all["sessions_working"] + sig_all["runs_active"]
    return {"card_id": card_id, "state": state, "label": STATE_LABELS[state],
            "progress": round(counts["done"] / total, 3) if total else (1.0 if state == "done" else 0.0),
            "counts": counts, "total": total, "children": len(board.children(card_id)),
            "descendants": len(desc), "working": working, "signals": sig_all,
            "leaves": per_card}


def refresh(card_id: int, origin_note: str = "") -> list[dict]:
    """Recompute the roll-up of `card_id` and of every card above it; a state that
    changed is stored and journaled on that card (user `helm`)."""
    changed = []
    chain = [card_id] + [a["id"] for a in reversed(board.ancestors(card_id))]
    for cid in chain:
        r = rollup(cid)
        if r is None or r["children"] == 0:
            continue                         # a leaf has no roll-up to keep
        con = _con()
        old = con.execute("SELECT state FROM helm_rollup WHERE card_id=?", (cid,)).fetchone()
        con.execute(
            "INSERT INTO helm_rollup(card_id, state, progress, counts, updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(card_id) DO UPDATE SET state=excluded.state, progress=excluded.progress,"
            " counts=excluded.counts, updated_at=excluded.updated_at",
            (cid, r["state"], r["progress"], json.dumps(r["counts"]), time.time()))
        if old is None or old["state"] != r["state"]:
            done, total = r["counts"]["done"], r["total"]
            detail = (f"{STATE_LABELS.get(old['state'], old['state']) if old else '—'} → "
                      f"{r['label']} ({done}/{total} done)")
            board._event(con, cid, "helm", "progress", detail + (f" — {origin_note}" if origin_note else ""),
                         {"via": "helm"})
            changed.append({"card_id": cid, "from": old["state"] if old else None, "to": r["state"]})
        con.commit()
        con.close()
    return changed


def _on_board_change(card_id: int | None, extra_parent: int | None = None) -> None:
    import features
    if not features.enabled("helm"):
        return
    for cid in {c for c in (card_id, extra_parent) if c}:
        if board.get_card(cid):
            refresh(cid)


board.on_change(_on_board_change)


# ---- context flows down ---------------------------------------------------------------
CONTEXT_MAX = 3200


def _has_context(c: dict) -> bool:
    return bool(c.get("intent") or c.get("constraints") or c.get("decisions"))


def context_block(card_id: int) -> str:
    """What a session of `card_id` must know from the cards ABOVE it: intent,
    constraints, decisions and links of every ancestor, root first. '' = none."""
    chain = [a for a in board.ancestors(card_id)]
    parts = []
    for a in chain:
        links = [lk for lk in board.card_links(a["id"]) if not lk.get("missing")]
        if not (_has_context(a) or links):
            continue
        lines = [f"## Card #{a['id']} “{a['title']}” ({a.get('kind') or 'task'}) — marked card:{a['id']}"]
        if a.get("intent"):
            lines.append(f"Intent: {a['intent']}")
        if a.get("constraints"):
            lines.append(f"Constraints: {a['constraints']}")
        if a.get("decisions"):
            lines.append("Decisions already taken:")
            lines += [f"- {d}" for d in a["decisions"]]
        if links:
            lines.append("Links: " + "; ".join(
                f"{lk['kind']} {lk.get('label') or lk['ref']}" + (f" <{lk['href']}>" if lk.get("href") else "")
                for lk in links[:8]))
        parts.append("\n".join(lines))
    if not parts:
        return ""
    out = ("=== Context from the parent cards (Helm) ===\n" + "\n\n".join(parts) +
           "\nStay within this intent and these constraints. If the work needs to depart from a "
           "recorded decision, say so and ask before acting.")
    return out if len(out) <= CONTEXT_MAX else out[:CONTEXT_MAX - 1] + "…"


def context_note_name(card_id: int) -> str:
    return f"helm-card-{int(card_id)}"


def write_context_note(card_id: int) -> dict | None:
    """The card's context as a project memory note marked `card:<id>` (recalled by every
    session of the project, not only by the cards below). Written by the API on a human
    edit — never by an agent run (those go to quarantine). None = nothing to write."""
    c = board.get_card(card_id)
    if c is None or not (_has_context(c) or c.get("kind") == "project"):
        return None
    import store_backend
    project = c.get("project") or "default"
    d = store_backend.memory_dir_for(project)
    name = context_note_name(card_id)
    kids = board.children(card_id)
    desc = f"card:{card_id} — Helm context of the {c.get('kind') or 'task'} card “{c['title']}”"
    if c.get("intent"):
        desc += f": {c['intent']}"
    body = [f"card:{card_id} · {c.get('kind') or 'task'} card “{c['title']}” (project {project}).",
            "Written by Helm from the card itself: edit the card, not this note.", ""]
    if c.get("intent"):
        body += ["## Intent", c["intent"], ""]
    if c.get("constraints"):
        body += ["## Constraints", c["constraints"], ""]
    if c.get("decisions"):
        body += ["## Decisions"] + [f"- {x}" for x in c["decisions"]] + [""]
    links = [lk for lk in board.card_links(card_id) if not lk.get("missing")]
    if links:
        body += ["## Links"] + [f"- {lk['kind']}: {lk.get('label') or lk['ref']}" for lk in links] + [""]
    if kids:
        body += ["## Cards under it"] + [f"- #{k['id']} {k['title']} ({k['bucket']})" for k in kids] + [""]
    fm = ["---", f"name: {name}",
          f"description: {json.dumps(' '.join(desc.split())[:300], ensure_ascii=False)}",
          "metadata:", "  type: project", f"  card: \"card:{card_id}\"", "  source: helm", "---", ""]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(fm) + "\n".join(body).strip() + "\n", encoding="utf-8")
    tmp.replace(path)
    return {"note": name, "path": str(path), "card": f"card:{card_id}"}


def spawn_context(card_id: int) -> str:
    """Called at a card's spawn: refresh the ancestors' notes and return the block."""
    for a in board.ancestors(card_id):
        try:
            write_context_note(a["id"])
        except Exception:  # noqa: BLE001 — the note is a convenience, the block is the guarantee
            pass
    return context_block(card_id)


# ---- deck (the Helm view) -------------------------------------------------------------
def project_cards(projects_: list[str]) -> list[dict]:
    """Steering cards: kind `project`, or a top-level card that has children."""
    if not projects_:
        return []
    con = board._con()
    q = ",".join("?" * len(projects_))
    rows = [board._card_out(r) for r in con.execute(
        f"SELECT * FROM cards c WHERE archived=0 AND project IN ({q}) AND (kind='project' OR"
        " (parent_id IS NULL AND EXISTS (SELECT 1 FROM cards k WHERE k.parent_id=c.id AND"
        " k.archived=0))) ORDER BY priority, COALESCE(updated_at, created_at) DESC",
        projects_)]
    con.close()
    return rows


def _people(card: dict, desc: list[dict]) -> list[str]:
    return sorted({x.get("assignee") for x in [card] + desc if x.get("assignee")})


def deck_item(card: dict) -> dict:
    r = rollup(card["id"]) or {}
    desc = board.descendants(card["id"])
    r.pop("leaves", None)
    return {"card": card, "rollup": r, "people": _people(card, desc),
            "suggestions": open_suggestion_count(card["id"]),
            "breathing": bool(r.get("working"))}


def deck(user: dict, project: str = "", team: str = "", person: str = "") -> dict:
    allowed = steerable_projects(user)
    slugs = [project] if project else allowed
    slugs = [s for s in slugs if s in allowed]
    if team:
        import projects as P
        granted = {g["project"] for s in slugs for g in P.list_grants(s)
                   if g["principal_kind"] == "team" and g["principal"] == team}
        members = _team_members(team)
    else:
        granted, members = set(), set()
    items = []
    for c in project_cards(slugs):
        it = deck_item(c)
        if person and person.lower() not in it["people"]:
            continue
        if team and c["project"] not in granted and not (members & set(it["people"])):
            continue
        items.append(it)
    return {"projects": allowed, "items": items, "states": list(STATES), "labels": STATE_LABELS,
            "project_suggestions": [s for s in list_suggestions(slugs) if not s["card_id"]]}


def _team_members(team: str) -> set[str]:
    import projects as P
    con = P._con()
    rows = {r["email"] for r in con.execute("SELECT email FROM team_members WHERE team_id=?", (team,))}
    con.close()
    return rows


def filters(user: dict) -> dict:
    import projects as P
    slugs = steerable_projects(user)
    teams = sorted({g["principal"] for s in slugs for g in P.list_grants(s) if g["principal_kind"] == "team"})
    people: set[str] = set()
    for c in project_cards(slugs):
        people.update(_people(c, board.descendants(c["id"])))
    return {"projects": [{"slug": p["slug"], "name": p["name"]} for p in P.work_projects() if p["slug"] in slugs],
            "teams": teams, "people": sorted(people)}


def kanban(card_id: int) -> dict:
    """The card's own board: its direct children by column, each with its roll-up."""
    kids = board.children(card_id)
    cols: dict[str, list] = {b: [] for b in board.BUCKETS}
    for k in kids:
        r = rollup(k["id"]) or {}
        r.pop("leaves", None)
        cols[k["bucket"]].append({**k, "rollup": r})
    return {"buckets": board.BUCKETS, "cards": cols}


def activity(card_id: int, limit: int = 120) -> list[dict]:
    ids = [card_id] + [d["id"] for d in board.descendants(card_id, include_archived=True)]
    titles = {card_id: (board.get_card(card_id) or {}).get("title", "")}
    titles.update({d["id"]: d["title"] for d in board.descendants(card_id, include_archived=True)})
    con = board._con()
    q = ",".join("?" * len(ids))
    rows = [dict(r) for r in con.execute(
        f"SELECT card_id, ts, user, action, detail, session_id, session_tag, via FROM card_events"
        f" WHERE card_id IN ({q}) ORDER BY ts DESC, id DESC LIMIT ?", (*ids, limit))]
    con.close()
    for r in rows:
        r["card_title"] = titles.get(r["card_id"], "")
    return rows


def costs(card_id: int) -> dict:
    """What the work under the card cost: its sessions (usage of their transcripts) and
    its agent runs. Estimation, like the Costs tab."""
    ids = [card_id] + [d["id"] for d in board.descendants(card_id, include_archived=True)]
    sessions: dict[str, dict] = {}
    runs: dict[int, dict] = {}
    for cid in ids:
        c = board.get_card(cid) or {}
        sig = card_signals(c) if c else {"sessions": [], "runs": []}
        for s in sig["sessions"]:
            sessions.setdefault(s["session_id"], {"session_id": s["session_id"], "card_id": cid})
        for r in sig["runs"]:
            runs.setdefault(r["id"], {**r, "card_id": cid})
    known = {s["session_id"]: s for s in board.list_sessions()}
    by_csid: dict[str, float] = {}
    try:
        import usage
        # the usage cache only (refreshed by the Costs tab): a full transcript scan here
        # would cost seconds on every popout refresh
        con = usage._con()
        keys = []
        for sid in sessions:
            keys += [sid] + ([known[sid]["claude_session_id"]] if known.get(sid, {}).get("claude_session_id") else [])
        if keys:
            q = ",".join("?" * len(keys))
            for r in con.execute(f"SELECT session_id, cost FROM files WHERE session_id IN ({q})", keys):
                by_csid[r["session_id"]] = float(r["cost"] or 0)
        con.close()
    except Exception:  # noqa: BLE001 — costs are an estimation, never a blocker
        pass
    out_s = []
    for sid, s in sessions.items():
        k = known.get(sid, {})
        cost = by_csid.get(sid, 0.0) + by_csid.get(k.get("claude_session_id") or "-", 0.0)
        out_s.append({**s, "title": k.get("title") or sid[:8], "cost_usd": round(cost, 4)})
    out_r = list(runs.values())
    total = sum(x["cost_usd"] for x in out_s) + sum(float(x.get("cost_usd") or 0) for x in out_r)
    return {"card_id": card_id, "total_usd": round(total, 4), "sessions": out_s, "runs": out_r,
            "note": "estimation (API price grid for sessions, SOKKAN cost basis for runs) — not an invoice"}


def detail(card_id: int) -> dict | None:
    c = board.card_detail(card_id)
    if c is None:
        return None
    r = rollup(card_id) or {}
    r.pop("leaves", None)
    return {**c, "rollup": r, "kanban": kanban(card_id),
            "suggestions": list_suggestions([c["project"]], card_id=card_id),
            "context_note": context_note_name(card_id) if (_has_context(c) or c.get("kind") == "project") else None}


# ---- suggestions: Helm proposes, a manager decides -----------------------------------
_EMB_CACHE: dict[str, list[float]] = {}


def _embed(texts: list[str]) -> list[list[float]]:
    """The memory engine's embedding (memory/embeddings.py): unit vectors, dot = cosine."""
    import embeddings
    return embeddings.embed_texts(texts)


def _vectors(texts: list[str]) -> list[list[float]]:
    missing = [t for t in dict.fromkeys(texts) if t not in _EMB_CACHE]
    if missing:
        for t, v in zip(missing, _embed(missing)):
            _EMB_CACHE[t] = v
        if len(_EMB_CACHE) > 4000:
            _EMB_CACHE.clear()
    return [_EMB_CACHE[t] for t in texts]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _text(c: dict) -> str:
    return " ".join(f"{c.get('title') or ''}. {c.get('description') or ''}".split())[:1500]


_NEG_PATTERNS = [
    # English
    r"^(?:we\s+)?(?:will\s+)?(?:do\s+not|don't|never|no longer|no|not|avoid|without|stop using|drop|ban)\s+"
    r"(?:use\s+|using\s+|add\s+|adding\s+|build\s+|building\s+|support\s+|supporting\s+|touch\s+|any\s+)?(?P<x>.+)$",
    r"^(?P<x>.+?)\s+(?:is|are)\s+(?:out of scope|forbidden|not allowed|banned|dropped)\b",
    r"^(?:use|keep|prefer)\s+.+?\s+(?:instead of|rather than|not)\s+(?P<x>.+)$",
    # French
    r"^(?:on\s+)?(?:n'utilise\s+pas|ne\s+pas\s+utiliser|pas\s+de|pas\s+d'|jamais|sans|plus\s+de|aucun|aucune|interdit\s*:?)\s*(?P<x>.+)$",
    r"^(?P<x>.+?)\s+(?:est|sont)\s+(?:hors\s+p[ée]rim[eè]tre|interdits?|exclus?|abandonn[ée]e?s?)\b",
    r"^.+?\s+plut[ôo]t\s+que\s+(?P<x>.+)$",
]
_STOP = re.compile(r"\s*(?:[,;:(\[]|\s-\s|\bbecause\b|\bcar\b|\bparce\b|\bsince\b|\bfor now\b|\bpour\b|\buntil\b|\bjusqu)", re.I)
_WORD = re.compile(r"[\w][\w.+#-]*", re.U)


def forbidden_terms(decision: str) -> list[str]:
    """What a recorded decision rules out ("no Kafka", "pas de microservices",
    "use REST instead of GraphQL" → kafka / microservices / graphql). Heuristic, on
    purpose: it flags, a human judges."""
    d = " ".join(decision.strip().rstrip(".!").split())
    out = []
    for p in _NEG_PATTERNS:
        m = re.match(p, d, re.I)
        if not m:
            continue
        x = _STOP.split(m.group("x"), maxsplit=1)[0]
        words = _WORD.findall(x.lower())
        words = [w for w in words if w not in ("the", "a", "an", "any", "le", "la", "les", "des", "un", "une")][:4]
        term = " ".join(words)
        if len(term) >= 3:
            out.append(term)
        break
    return out


def _mentions(text: str, term: str) -> bool:
    words = _WORD.findall(text.lower())
    tw = term.split()
    n = len(tw)
    return any(words[i:i + n] == tw for i in range(len(words) - n + 1))


def _parents(project: str) -> list[dict]:
    """Every card of the project that has children (the ones that steer something)."""
    con = board._con()
    rows = [board._card_out(r) for r in con.execute(
        "SELECT * FROM cards c WHERE project=? AND archived=0 AND (kind='project' OR EXISTS"
        " (SELECT 1 FROM cards k WHERE k.parent_id=c.id AND k.archived=0))", (project,))]
    con.close()
    return rows


def _comments_text(card_id: int, n: int = 10) -> str:
    return " ".join(x["body"] for x in board.card_comments(card_id)[-n:])


def detect(project: str, now: float | None = None) -> tuple[list[dict], list[str]]:
    """Every suggestion that holds right now for `project` (not stored). Returns
    (suggestions, notes) — a note says why a detector was skipped."""
    now = time.time() if now is None else now
    out: list[dict] = []
    notes: list[str] = []
    parents = _parents(project)

    def add(kind, card, target, key, title, detail, evidence):
        out.append({"project": project, "card_id": card["id"] if card else None,
                    "target_id": target["id"] if target else None, "kind": kind,
                    "fingerprint": f"{project}:{kind}:{card['id'] if card else '-'}:{key}",
                    "title": title, "detail": detail, "evidence": evidence})

    # 1. drift from the parent's intent (semantic)
    pairs = []
    for p in parents:
        ptxt = p.get("intent") or _text(p)
        for k in board.children(p["id"]):
            if k["bucket"] == "Done" or k.get("kind") == "reframe" or len(_text(k)) < 12:
                continue
            pairs.append((p, k, ptxt, _text(k)))
    if pairs:
        try:
            vecs = _vectors([x[2] for x in pairs] + [x[3] for x in pairs])
            n = len(pairs)
            for i, (p, k, _pt, _kt) in enumerate(pairs):
                cos = _cos(vecs[i], vecs[n + i])
                if cos < drift_min():
                    add("drift", p, k, k["id"],
                        f"#{k['id']} “{k['title']}” drifts from the goal of “{p['title']}”",
                        f"Its text is far from the parent's intent (similarity {cos:.2f} < {drift_min():.2f}). "
                        "Re-scope it, move it under another card, or record why it belongs here.",
                        {"similarity": round(cos, 3), "threshold": drift_min(),
                         "intent": (p.get("intent") or "")[:300]})
        except Exception as e:  # noqa: BLE001 — no embedding engine = no drift detector
            notes.append(f"drift: embeddings unavailable ({type(e).__name__})")

    # 2. work that contradicts a recorded decision (the card's and its ancestors')
    for p in parents:
        decisions = [(p["id"], d) for d in p.get("decisions") or []]
        for a in board.ancestors(p["id"]):
            decisions += [(a["id"], d) for d in a.get("decisions") or []]
        rules = [(src, d, t) for src, d in decisions for t in forbidden_terms(d)]
        if not rules:
            continue
        for k in board.children(p["id"]):
            if k["bucket"] == "Done" or k.get("kind") == "reframe":
                continue
            txt = _text(k) + " " + _comments_text(k["id"])
            for src, d, term in rules:
                if _mentions(txt, term):
                    add("contradiction", p, k, f"{k['id']}:{term}",
                        f"#{k['id']} “{k['title']}” may contradict a decision",
                        f"Decision on #{src}: “{d}” — this card mentions “{term}”.",
                        {"decision": d, "decision_card": src, "term": term})
                    break

    # 3. scope growing past the baseline
    for p in parents:
        base_at = p.get("baseline_at")
        if not base_at:
            continue
        kids = [k for k in board.children(p["id"], include_archived=True) if k.get("kind") != "reframe"]
        before = [k for k in kids if (k.get("created_at") or 0) <= base_at + 60]
        after = [k for k in kids if (k.get("created_at") or 0) > base_at + 60]
        limit = max(3, math.ceil(0.3 * max(len(before), 1)))
        if len(after) >= limit:
            add("scope", p, None, f"{len(before)}+{len(after)}",
                f"Scope of “{p['title']}” grew by {len(after)} card(s) since its baseline",
                f"{len(before)} card(s) at the baseline, {len(after)} added since: "
                + ", ".join(f"#{k['id']} {k['title']}" for k in after[:8])
                + ". Accept the new scope (re-baseline) or move cards out.",
                {"baseline_at": base_at, "before": len(before), "added": [k["id"] for k in after]})
    # cards added OUTSIDE any parent while the project is steered
    bases = [p["baseline_at"] for p in parents if p.get("kind") == "project" and p.get("baseline_at")]
    if bases:
        since = min(bases) + 60
        con = board._con()
        orphans = [dict(r) for r in con.execute(
            "SELECT id, title FROM cards c WHERE project=? AND archived=0 AND parent_id IS NULL AND"
            " kind='task' AND bucket != 'Done' AND created_at > ? AND NOT EXISTS (SELECT 1 FROM"
            " cards k WHERE k.parent_id=c.id)", (project, since))]
        con.close()
        if len(orphans) >= 3:
            add("unparented", None, None, ",".join(str(o["id"]) for o in orphans),
                f"{len(orphans)} cards were added outside every project card",
                "Attach them to the project card they serve, or close them: "
                + ", ".join(f"#{o['id']} {o['title']}" for o in orphans[:10]),
                {"cards": [o["id"] for o in orphans]})

    # 4. cards nobody owns
    for p in parents:
        lost = [k for k in board.children(p["id"]) if k["bucket"] != "Done"
                and not k.get("assignee") and k.get("kind") != "reframe"]
        if lost:
            add("unassigned", p, None, ",".join(str(k["id"]) for k in lost),
                f"{len(lost)} card(s) under “{p['title']}” have no owner",
                "Assign them: " + ", ".join(f"#{k['id']} {k['title']}" for k in lost[:10]),
                {"cards": [k["id"] for k in lost]})

    # 5. velocity: slowing down, or racing ahead
    week = 7 * 86400
    for p in parents:
        if p.get("parent_id"):
            continue                       # velocity is a project-level signal
        desc = [d for d in board.descendants(p["id"], include_archived=True) if d.get("kind") != "reframe"]
        if len(desc) < 3:
            continue
        open_ = [d for d in desc if d["bucket"] != "Done" and not d.get("archived")]
        done7 = [d for d in desc if d.get("closed_at") and d["closed_at"] >= now - week]
        done_prev = [d for d in desc if d.get("closed_at") and now - 4 * week <= d["closed_at"] < now - week]
        prev_avg = len(done_prev) / 3
        created7 = [d for d in desc if (d.get("created_at") or 0) >= now - week
                    and (d.get("created_at") or 0) > (p.get("baseline_at") or 0) + 60]
        age = now - (p.get("created_at") or now)
        last = _last_event(p["id"], desc)
        if open_ and age > week:
            if prev_avg >= 1 and len(done7) < 0.5 * prev_avg:
                add("slowing", p, None, f"w{int(now // week)}",
                    f"“{p['title']}” is slowing down",
                    f"{len(done7)} card(s) done in the last 7 days against {prev_avg:.1f}/week before; "
                    f"{len(open_)} still open.",
                    {"done_7d": len(done7), "weekly_before": round(prev_avg, 2), "open": len(open_)})
            elif last and last < now - week:
                add("slowing", p, None, "stalled",
                    f"“{p['title']}” has not moved for {int((now - last) // 86400)} days",
                    f"No event on the project or its {len(desc)} cards since "
                    f"{datetime.fromtimestamp(last, TZ):%d.%m.%Y}; {len(open_)} still open.",
                    {"last_event": last, "open": len(open_)})
        if len(created7) >= 5 and len(created7) >= 2 * max(len(done7), 1):
            add("racing", p, None, f"w{int(now // week)}",
                f"“{p['title']}” is racing ahead: new work outpaces completion",
                f"{len(created7)} card(s) created in 7 days for {len(done7)} done.",
                {"created_7d": len(created7), "done_7d": len(done7)})
        spend = _run_spend(p["id"], desc, now)
        if spend["day"] > 1.0 and spend["day"] > 3 * max(spend["avg_before"], 0.01):
            add("racing", p, None, f"cost-d{int(now // 86400)}",
                f"“{p['title']}”: agent spend spiked",
                f"${spend['day']:.2f} of agent runs in 24 h against ${spend['avg_before']:.2f}/day the week before.",
                spend)

    # 6. an Operate incident linked to the work
    for p in parents:
        r = rollup(p["id"]) or {}
        for inc in (r.get("signals") or {}).get("incidents", []):
            add("incident", p, board.get_card(inc["card_id"]), inc["id"],
                f"Incident #{inc['id']} “{inc.get('title') or ''}” touches “{p['title']}”",
                "An open Operate incident is linked to this work: reprioritise, or note that it is "
                "handled elsewhere.", {"incident": inc["id"], "card": inc["card_id"],
                                       "severity": inc.get("severity")})
    # one suggestion per fingerprint (a card can sit under two steered parents)
    seen, uniq = set(), []
    for s in out:
        if s["fingerprint"] not in seen:
            seen.add(s["fingerprint"])
            uniq.append(s)
    return uniq, notes


def _last_event(card_id: int, desc: list[dict]) -> float | None:
    ids = [card_id] + [d["id"] for d in desc]
    con = board._con()
    q = ",".join("?" * len(ids))
    r = con.execute(f"SELECT MAX(ts) m FROM card_events WHERE card_id IN ({q}) AND via != 'helm'",
                    ids).fetchone()
    con.close()
    return r["m"] if r and r["m"] else None


def _run_spend(card_id: int, desc: list[dict], now: float) -> dict:
    runs: dict[int, dict] = {}
    for c in [board.get_card(card_id) or {}] + desc:
        if not c:
            continue
        for lk in _raw_links(c["id"]):
            if lk["kind"] == "run" and str(lk["ref"]).isdigit():
                x = _run(int(lk["ref"]))
                if x:
                    runs[x["id"]] = x
            elif lk["kind"] == "agent" and str(lk["ref"]).isdigit():
                for x in _agent_runs(int(lk["ref"]), 200):
                    runs[x["id"]] = x
    day = sum(float(x.get("cost_usd") or 0) for x in runs.values() if (x.get("created_at") or 0) >= now - 86400)
    before = sum(float(x.get("cost_usd") or 0) for x in runs.values()
                 if now - 8 * 86400 <= (x.get("created_at") or 0) < now - 86400)
    return {"day": round(day, 4), "avg_before": round(before / 7, 4)}


def suggest(project: str, now: float | None = None) -> dict:
    """Run the detectors and reconcile with what is stored: new → open; still true →
    kept (evidence refreshed); no longer true → resolved; ignored/approved recently →
    not proposed again (snooze)."""
    now = time.time() if now is None else now
    found, notes = detect(project, now)
    con = _con()
    new, kept = [], 0
    fps = set()
    for s in found:
        fps.add(s["fingerprint"])
        prev = con.execute("SELECT * FROM helm_suggestions WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
                           (s["fingerprint"],)).fetchone()
        if prev and prev["status"] == "open":
            con.execute("UPDATE helm_suggestions SET title=?, detail=?, evidence=?, updated_at=? WHERE id=?",
                        (s["title"], s["detail"], json.dumps(s["evidence"]), now, prev["id"]))
            kept += 1
            continue
        if prev and prev["status"] in ("ignored", "approved") and (prev["decided_at"] or 0) > now - snooze_s():
            continue
        cur = con.execute(
            "INSERT INTO helm_suggestions(project, card_id, target_id, kind, fingerprint, title, detail,"
            " evidence, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (project, s["card_id"], s["target_id"], s["kind"], s["fingerprint"], s["title"],
             s["detail"], json.dumps(s["evidence"]), now, now))
        new.append({**s, "id": cur.lastrowid})
    resolved = 0
    for r in con.execute("SELECT id, fingerprint FROM helm_suggestions WHERE project=? AND status='open'",
                         (project,)).fetchall():
        if r["fingerprint"] not in fps:
            con.execute("UPDATE helm_suggestions SET status='resolved', updated_at=? WHERE id=?", (now, r["id"]))
            resolved += 1
    con.commit()
    con.close()
    return {"project": project, "new": new, "kept": kept, "resolved": resolved, "notes": notes}


def _sugg_out(r) -> dict:
    d = dict(r)
    try:
        d["evidence"] = json.loads(d.get("evidence") or "{}")
    except ValueError:
        d["evidence"] = {}
    return d


def list_suggestions(projects_: list[str], card_id: int | None = None,
                     status: str = "open") -> list[dict]:
    if not projects_:
        return []
    con = _con()
    q = ",".join("?" * len(projects_))
    sql = f"SELECT * FROM helm_suggestions WHERE project IN ({q}) AND status=?"
    args: list = [*projects_, status]
    if card_id is not None:
        ids = [card_id] + [d["id"] for d in board.descendants(card_id)]
        sql += f" AND card_id IN ({','.join('?' * len(ids))})"
        args += ids
    rows = [_sugg_out(r) for r in con.execute(sql + " ORDER BY created_at DESC", args)]
    con.close()
    return rows


def open_suggestion_count(card_id: int) -> int:
    ids = [card_id] + [d["id"] for d in board.descendants(card_id)]
    con = _con()
    r = con.execute(f"SELECT count(*) n FROM helm_suggestions WHERE status='open' AND card_id IN"
                    f" ({','.join('?' * len(ids))})", ids).fetchone()
    con.close()
    return r["n"]


def get_suggestion(sid: int) -> dict | None:
    con = _con()
    r = con.execute("SELECT * FROM helm_suggestions WHERE id=?", (sid,)).fetchone()
    con.close()
    return _sugg_out(r) if r else None


def approve(sid: int, user: str) -> dict:
    """A manager approves: a `reframe` card is created under the steering card (or at the
    top of the board for a project-level one), assigned to them, and the concerned card
    gets a comment. Helm never edits the work itself."""
    s = get_suggestion(sid)
    if s is None:
        raise KeyError(sid)
    if s["status"] != "open":
        raise ValueError(f"suggestion already {s['status']}")
    body = (f"{s['detail']}\n\nSuggested by Helm ({s['kind']}), approved by {user}."
            + (f"\nEvidence: {json.dumps(s['evidence'], ensure_ascii=False)[:600]}" if s["evidence"] else ""))
    card = board.add_card(f"Reframe: {s['title']}"[:200], body, tag="research", bucket="Backlog",
                          priority=1, user=user, origin={"via": "helm"}, project=s["project"],
                          parent_id=s["card_id"], kind="reframe",
                          assignee=user if "@" in user else "")
    if s["target_id"] and board.get_card(s["target_id"]):
        board.add_comment(s["target_id"], f"Helm reframe approved by {user}: {s['title']} → card #{card['id']}",
                          author="helm", origin={"via": "helm"})
    con = _con()
    con.execute("UPDATE helm_suggestions SET status='approved', decided_by=?, decided_at=?, updated_at=?,"
                " reframe_card_id=? WHERE id=?", (user, time.time(), time.time(), card["id"], sid))
    con.commit()
    con.close()
    return {**(get_suggestion(sid) or {}), "reframe_card": card}


def ignore(sid: int, user: str) -> dict:
    s = get_suggestion(sid)
    if s is None:
        raise KeyError(sid)
    if s["status"] != "open":
        raise ValueError(f"suggestion already {s['status']}")
    con = _con()
    con.execute("UPDATE helm_suggestions SET status='ignored', decided_by=?, decided_at=?, updated_at=?"
                " WHERE id=?", (user, time.time(), time.time(), sid))
    con.commit()
    con.close()
    return get_suggestion(sid) or {}


def rebaseline(card_id: int, user: str) -> dict | None:
    """The manager accepts the current scope: the baseline moves to now."""
    c = board.get_card(card_id)
    if c is None:
        return None
    con = board._con()
    con.execute("UPDATE cards SET baseline_at=?, updated_at=? WHERE id=?", (time.time(), time.time(), card_id))
    board._event(con, card_id, user, "scope accepted",
                 f"baseline = {len(board.children(card_id))} card(s)", {"via": "web"})
    con.commit()
    con.close()
    return board.get_card(card_id)


# ---- project creation (Nina's proposal, validated by a human) -------------------------
def create_project(user: str, project: str, title: str, intent: str = "", scope: str = "",
                   constraints: str = "", deadline: str = "", team: list[str] | None = None,
                   decisions: list[str] | None = None, children: list[dict] | None = None,
                   tag: str = "research") -> dict:
    """The project card + its children in one go (the decomposition the person edited and
    validated). The baseline is set AFTER the children: they are the agreed scope."""
    if not (title or "").strip():
        raise ValueError("title is required")
    if deadline and not re.match(r"^\d{4}-\d{2}-\d{2}$", deadline):
        raise ValueError("deadline: YYYY-MM-DD")
    desc = []
    if scope.strip():
        desc.append(f"Scope: {scope.strip()}")
    if team:
        desc.append("Team: " + ", ".join(t.strip() for t in team if t.strip()))
    origin = {"via": "nina"}
    p = board.add_card(title.strip()[:200], "\n".join(desc), tag=tag, bucket="Backlog", priority=1,
                       due=deadline or "", user=user, origin=origin, project=project, kind="project",
                       intent=intent, constraints=constraints, decisions=decisions or [],
                       assignee=user if "@" in user else "")
    made = []
    for ch in (children or [])[:40]:
        t = (ch.get("title") or "").strip()
        if not t:
            continue
        assignee = (ch.get("assignee") or "").strip()
        if assignee:
            try:
                assignee = board.validate_assignee(assignee)
            except ValueError:
                assignee = ""
        made.append(board.add_card(t[:200], (ch.get("description") or "").strip(),
                                   tag=(ch.get("tag") or tag), bucket="Backlog",
                                   priority=int(ch.get("priority", 2)), due=ch.get("due") or "",
                                   user=user, origin=origin, project=project, parent_id=p["id"],
                                   assignee=assignee))
    con = board._con()
    con.execute("UPDATE cards SET baseline_at=? WHERE id=?", (time.time(), p["id"]))
    con.commit()
    con.close()
    note = write_context_note(p["id"])
    refresh(p["id"])
    return {"card": board.get_card(p["id"]), "children": made, "note": note}


# ---- morning brief --------------------------------------------------------------------
_MOVE_ACTIONS = ("created", "moved", "closed", "reopened", "assigned", "progress", "child added")


def _scope_people(project: str, person: str, team: str) -> set[str] | None:
    if person:
        return {person.lower()}
    if team:
        return _team_members(team)
    return None


def morning_brief(project: str, person: str = "", team: str = "", since: float | None = None,
                  now: float | None = None, calendar_events: list | None = None) -> dict:
    """The day's brief for a person (their cards) or a team (its members' cards):
    cards that moved, blockers, approvals waiting, incidents, agents in error, recent
    decisions, and the agenda when a calendar source is configured. Read-only."""
    now = time.time() if now is None else now
    if since is None:
        wd = datetime.fromtimestamp(now, TZ).weekday()
        since = now - (72 if wd == 0 else 24) * 3600       # Monday: since Friday morning
    people = _scope_people(project, person, team)
    con = board._con()
    cards = [board._card_out(r) for r in con.execute(
        "SELECT * FROM cards WHERE project=? AND archived=0", (project,))]
    by_id = {c["id"]: c for c in cards}

    def mine(c: dict) -> bool:
        if people is None:
            return True
        if (c.get("assignee") or "").lower() in people:
            return True
        return any((a.get("assignee") or "").lower() in people and a.get("kind") == "project"
                   for a in board.ancestors(c["id"]))
    scoped = {cid for cid, c in by_id.items() if mine(c)}
    # decisions: those of the person's cards AND of every card above them (they apply)
    above = {a["id"] for cid in scoped for a in board.ancestors(cid)}
    watch = scoped | above
    q = ",".join("?" * len(watch)) or "NULL"
    events = [dict(r) for r in con.execute(
        f"SELECT card_id, ts, user, action, detail FROM card_events WHERE ts >= ? AND card_id IN ({q})"
        " ORDER BY ts", (since, *watch))] if watch else []
    con.close()
    moved: dict[int, list] = {}
    decisions = []
    for e in events:
        if e["action"] in _MOVE_ACTIONS and e["card_id"] in scoped:
            moved.setdefault(e["card_id"], []).append(e)
        if e["action"] == "decision recorded":
            decisions.append({"card_id": e["card_id"], "title": by_id[e["card_id"]]["title"],
                              "decision": e["detail"], "by": e["user"], "ts": e["ts"]})
    blocked, waiting, incidents = [], [], []
    for cid in sorted(scoped):
        c = by_id[cid]
        if c["bucket"] == "Done":
            continue
        sig = card_signals(c)
        st = leaf_state(c, sig)
        if st == "blocked":
            blocked.append({"card_id": cid, "title": c["title"],
                            "why": ", ".join([f"incident #{i['id']}" for i in sig["incidents"]]
                                             + ([f"{sig['runs_failed']} failed run(s)"] if sig["runs_failed"] else []))})
        elif st == "waiting":
            waiting.append({"card_id": cid, "title": c["title"],
                            "why": "in Review" if c["bucket"] == "Review" else "approval pending"})
        for i in sig["incidents"]:
            if i["id"] not in {x["id"] for x in incidents}:
                incidents.append({**i, "card_id": cid})
    errors, approvals = [], []
    try:
        import agents
        con = agents._con()
        for a in con.execute("SELECT * FROM agents WHERE project=? AND status != 'archived'", (project,)):
            a = agents._agent_out(a)
            if people is not None and (a["owner"] or "").lower() not in people:
                continue
            last = next((x for x in _agent_runs(a["id"], 10)
                         if x["status"] not in ("skipped", "cancelled", "queued", "running")), None)
            if last and last["status"] in ("failed", "timeout", "budget", "interrupted", "incomplete"):
                errors.append({"agent_id": a["id"], "name": a["name"], "owner": a["owner"]})
            if a["status"] == "pending" or a.get("pending_change"):
                approvals.append({"kind": "agent", "id": a["id"], "name": a["name"]})
        con.close()
    except Exception as e:  # noqa: BLE001 — Crew off or unreadable: the brief says nothing of it
        print(f"[helm] brief: agents unreadable: {e!r}", flush=True)
    sugg = list_suggestions([project])
    agenda = calendar_events if calendar_events is not None else _agenda(person, team, now)
    out = {"project": project, "person": person, "team": team, "since": since, "now": now,
           "moved": [{"card_id": cid, "title": by_id[cid]["title"], "bucket": by_id[cid]["bucket"],
                      "events": [f"{e['action']}: {e['detail']}" for e in evs][-4:]}
                     for cid, evs in moved.items()],
           "blocked": blocked, "waiting": waiting, "approvals": approvals,
           "suggestions": [{"id": s["id"], "title": s["title"], "kind": s["kind"]} for s in sugg],
           "incidents": incidents, "agents_in_error": errors, "decisions": decisions,
           "agenda": agenda}
    out["markdown"] = brief_markdown(out)
    return out


def _agenda(person: str, team: str, now: float) -> list[dict] | None:
    try:
        import helm_calendar
        src = helm_calendar.source_for(person or team)
        if src is None:
            return None
        day = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        return [e.as_dict() for e in src.events(day, day + timedelta(days=1))]
    except Exception as e:  # noqa: BLE001 — a calendar outage never kills the brief
        return [{"error": f"calendar unavailable: {type(e).__name__}"}]


def brief_markdown(b: dict) -> str:
    who = b["person"] or (f"team {b['team']}" if b["team"] else "the project")
    day = datetime.fromtimestamp(b["now"], TZ)
    L = [f"# Morning brief — {who} — {day:%a %d.%m.%Y}", f"Project `{b['project']}`.", ""]

    def sec(title, items, fmt):
        L.append(f"## {title}")
        L.extend([f"- {fmt(x)}" for x in items] if items else ["- none"])
        L.append("")
    if b.get("agenda") is not None:
        sec("Agenda", b["agenda"], lambda e: e.get("error") or
            f"{e['start_local']}{'–' + e['end_local'] if e.get('end_local') else ''} {e['title']}"
            + (f" ({e['location']})" if e.get("location") else ""))
    sec("Blocked", b["blocked"], lambda x: f"#{x['card_id']} {x['title']} — {x['why'] or 'blocked'}")
    sec("Waiting for an approval", b["waiting"] + b["approvals"],
        lambda x: f"#{x['card_id']} {x['title']} ({x['why']})" if "card_id" in x
        else f"agent {x['name']} needs approval")
    sec("Incidents", b["incidents"], lambda x: f"#{x['id']} {x.get('title') or ''} ({x.get('severity') or ''}) on card #{x['card_id']}")
    sec("Agents in error", b["agents_in_error"], lambda x: f"{x['name']} (owner {x['owner']})")
    sec("Cards that moved", b["moved"], lambda x: f"#{x['card_id']} {x['title']} → {x['bucket']}: "
        + "; ".join(x["events"]))
    sec("Recent decisions", b["decisions"], lambda x: f"#{x['card_id']} {x['title']}: {x['decision']} ({x['by']})")
    if b["suggestions"]:
        sec("Helm suggestions to review", b["suggestions"], lambda x: f"{x['kind']}: {x['title']}")
    return "\n".join(L).strip() + "\n"


# ---- periodic job ---------------------------------------------------------------------
def run_once(notify_new: bool = True) -> dict:
    """Refresh every steering card's roll-up and every project's suggestions."""
    import projects
    out = {}
    for p in projects.work_projects():
        slug = p["slug"]
        for c in _parents(slug):
            refresh(c["id"])
        res = suggest(slug)
        out[slug] = {"new": len(res["new"]), "resolved": res["resolved"], "notes": res["notes"]}
        if notify_new and res["new"]:
            try:
                import notify
                notify.send(f"Helm: {len(res['new'])} new suggestion(s) in {slug}",
                            "\n".join(f"• {s['title']}" for s in res["new"][:5]),
                            link="/?tab=helm", kind="info")
            except Exception:  # noqa: BLE001
                pass
    return out


async def loop(stop) -> None:
    """The API's background job (started in the lifespan when the feature is on)."""
    import asyncio
    import features
    while not stop.is_set():
        try:
            if features.enabled("helm"):
                await asyncio.to_thread(run_once)
        except Exception as e:  # noqa: BLE001
            print(f"[helm] periodic job failed: {e!r}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_s())
        except asyncio.TimeoutError:
            pass
