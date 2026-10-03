#!/usr/bin/env python3
"""corthexis.py — the CortHeXis tab: the project memory, visible and repairable.

Wraps the memory engine (``memory/core/review.py`` and ``repair.py``) for SOKKAN:

* **graph** — notes, ``[[links]]``, broken links as ghosts, a 2-D projection of the
  notes' meaning (mean embedding of their passages), ages and review flags;
* **review** — run every hour in the backend process, which is where the chat sessions
  are started: the chain checks (the memory server answers, started exactly the way a
  session starts it; embedding servers; recall hook) see what a session sees;
  score history, problems open for more than 7 days, mean time to fix;
* **repairs** — a click creates a *proposal* (the full diff of every file it touches);
  nothing is written until someone with the ``dev`` role approves it. Approvals,
  refusals and conflicts go to the audit journal; a proposal left pending pings the
  notification channels like a session waiting for a permission. Judgement cases open a
  « Memory curation » session pre-loaded with the findings, whose edits go through the
  session's own permission buttons;
* **alerts** — through ``notify``: one digest a day when the findings changed, and right
  away when a new critical problem appears (at most once per cooldown).

Configuration (``CORTHEXIS_*`` or ``SOKKAN_*``): ``REVIEW_EVERY_S`` (3600, 0 = off),
``REVIEW_DIGEST_AT`` (08:20), ``REVIEW_TZ``, ``REVIEW_CRIT_COOLDOWN_H`` (6), the
``REVIEW_*`` thresholds of ``ReviewConfig``, ``DEAD_PATH_ROOTS`` / ``DEAD_PATH_PREFIXES``,
``REVIEW_EXPECT_HOOK`` / ``REVIEW_HOOK_PATTERN``, ``REVIEW_CHAIN`` (1).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
from core import repair as rp  # noqa: E402
from core import review as rv  # noqa: E402
from core.config import env, env_bool, env_int  # noqa: E402
from core.dates import age_days  # noqa: E402

import audit  # noqa: E402
import auth  # noqa: E402
import iam  # noqa: E402
import notify  # noqa: E402
import playbooks  # noqa: E402

DATA_DIR = Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))
_HERE = Path(__file__).resolve().parent
MEM_SERVER = _HERE.parent / "memory" / "memory_search_server.py"
PUBLIC_URL = (os.environ.get("SOKKAN_PUBLIC_URL", "http://localhost:3009")).rstrip("/")
PENDING_PING_S = float(env("REVIEW_PENDING_PING_S", "600") or 600)

_lock = threading.RLock()
_state: dict = {"report": None, "graph": None, "graph_key": None, "running": False}
# set by app.py: spawns an SDK chat session → dict with session_id
spawn_hook = None
# set by app.py (2.x SQLite path): incremental reindex after a repair
reindex_hook = None


# ----------------------------------------------------------------------------- wiring

def memory_dir() -> Path:
    return Path(env("MEMORY_DIR", "~/.sokkan/memory") or "~/.sokkan/memory").expanduser()


def _pg() -> bool:
    import store_backend
    return store_backend.enabled()


def _store():
    import store_backend
    return store_backend.get_store()


def source():
    if _pg():
        return rv.PgSource(_store())
    import memorykb
    return rv.SqliteSource(memorykb.MEM_DB)


_history = {"obj": None, "pg": None}


def history():
    pg = _pg()
    if _history["obj"] is None or _history["pg"] != pg:
        _history["obj"] = rv.PgHistory(_store()) if pg else \
            rv.SqliteHistory(DATA_DIR / "corthexis-review.db")
        _history["pg"] = pg
    return _history["obj"]


def review_config() -> rv.ReviewConfig:
    # the 2.x writer (memory_write) names files "<name>.md": accepted until the corpus
    # moves to the 3.0 store, whose indexer normalises it
    return rv.ReviewConfig.from_env(memory_dir=memory_dir(), accept_kebab_files=not _pg())


def chain_config() -> rv.ChainConfig:
    """The chain as a SOKKAN chat session sees it (agentchat.MCP_SERVERS)."""
    cfg = rv.ChainConfig.from_env(
        server_name="sokkan-memory",
        launch={"command": os.environ.get("SOKKAN_PYTHON", sys.executable),
                "args": [str(MEM_SERVER)]},
        handshake_timeout=float(env("REVIEW_HANDSHAKE_TIMEOUT", "60") or 60))
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
    cwd = Path(os.environ.get("SOKKAN_AGENT_CWD") or (
        "/workspace" if os.path.isdir("/workspace") else os.getcwd()))
    if not cfg.hook_settings:
        cfg.hook_settings = [claude_dir / "settings.json", cwd / ".claude" / "settings.json"]
    if not cfg.embed_urls and _pg():
        try:
            from core import embed
            d = embed.describe()
            if d.get("backend") == "llamacpp":
                cfg.embed_urls = list(d.get("urls") or [])
                cfg.rerank_url = d.get("rerank_url")
        except Exception:  # noqa: BLE001 — no embedding configured: nothing to probe
            pass
    return cfg


# ----------------------------------------------------------------------------- review

def run(*, chain: bool | None = None, alert: bool = True) -> dict:
    """One review pass: record it, maybe alert. Never raises (a failed pass is logged)."""
    chain = env_bool("REVIEW_CHAIN", True) if chain is None else chain
    with _lock:
        if _state["running"]:
            return _state["report"] or {}
        _state["running"] = True
    try:
        findings = rv.chain_checks(chain_config()) if chain else []
        report = rv.run_review(review_config(), source(), chain=findings)
        try:
            history().record(report)
        except Exception as e:  # noqa: BLE001
            print(f"[corthexis] history not recorded: {e}", file=sys.stderr)
        with _lock:
            _state["report"] = report
            _state["graph"] = None
        if alert:
            maybe_alert(report)
        return report
    finally:
        with _lock:
            _state["running"] = False


def current() -> dict:
    rep = _state["report"]
    if rep is None:
        try:
            rep = history().last_report()
        except Exception:  # noqa: BLE001
            rep = None
        if rep is None:
            rep = run(chain=False, alert=False)
        _state["report"] = rep
    return rep


def maybe_alert(report: dict) -> str | None:
    if not notify.enabled():
        return None
    h = history()
    kind = rv.alert_decision(report, h, rv.AlertPolicy.from_env())
    if not kind:
        return None
    title, body = rv.format_digest(report, kind=kind)
    try:
        sent = notify.send(title, body, f"{PUBLIC_URL}/?tab=corthexis", "memory")
    except Exception:  # noqa: BLE001
        return None
    if any(v == "ok" for v in sent.values()):
        rv.mark_alerted(report, h, kind)
        return kind
    return None


def loop() -> None:
    """Background reviewer (started by app.py). First pass shortly after boot."""
    every = env_int("REVIEW_EVERY_S", 3600)
    if every <= 0:
        return
    time.sleep(float(env("REVIEW_FIRST_DELAY_S", "45") or 45))
    while True:
        try:
            run()
        except Exception as e:  # noqa: BLE001
            print(f"[corthexis] review failed: {e}", file=sys.stderr)
        # wake up for the digest slot even when the period does not land on it
        pol = rv.AlertPolicy.from_env()
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            now = now.astimezone(ZoneInfo(pol.tz))
        except Exception:  # noqa: BLE001
            pass
        slot = now.replace(hour=pol.digest_hour, minute=pol.digest_minute, second=5,
                           microsecond=0)
        if slot <= now:
            slot += datetime.timedelta(days=1)
        time.sleep(max(30.0, min(every, (slot - now).total_seconds())))


def start() -> None:
    threading.Thread(target=loop, daemon=True, name="corthexis-review").start()


# ----------------------------------------------------------------------------- graph

def _pca2d(vecs: dict[str, list[float]]) -> dict[str, tuple[float, float]]:
    import numpy as np
    if len(vecs) < 3:
        return {k: (0.0, 0.0) for k in vecs}
    names = list(vecs)
    M = np.asarray([vecs[k] for k in names], dtype=np.float32)
    M = M - M.mean(axis=0)
    _u, _s, vt = np.linalg.svd(M, full_matrices=False)
    P = M @ vt[:2].T
    for c in range(2):
        col = P[:, c]
        rng = float(col.max() - col.min()) or 1.0
        P[:, c] = (col - col.min()) / rng * 2 - 1
    return {k: (round(float(P[i, 0]), 4), round(float(P[i, 1]), 4)) for i, k in enumerate(names)}


def _corpus_key(notes: list[rv.CorpusNote], report: dict) -> str:
    h = hashlib.sha1()
    for n in notes:
        h.update(f"{n.path.name}:{n.mtime}:{n.words}\n".encode())
    h.update((report or {}).get("signature", "").encode())
    h.update((report or {}).get("at", "").encode())
    return h.hexdigest()[:12]


def graph() -> dict:
    notes = rv.load_corpus(memory_dir())
    report = current()
    key = _corpus_key(notes, report)
    with _lock:
        if _state["graph"] and _state["graph_key"] == key:
            return _state["graph"]
    resolve = rv.Resolver(notes)
    src = source()
    indexed, cents, err = {}, {}, None
    try:
        indexed = src.indexed()
        cents = src.centroids()
    except Exception as e:  # noqa: BLE001
        err = str(e)[:200]
    names = {n.name for n in notes}
    proj = _pca2d({k: v for k, v in cents.items() if k in names})
    flags = report.get("flags") or {}
    sev = {f["id"]: f["severity"] for f in report.get("findings", [])}
    edges, ghosts, inbound = [], {}, {}
    for n in notes:
        seen = set()
        for t in n.links:
            r = resolve(t)
            if r and r.name != n.name and r.name not in seen:
                seen.add(r.name)
                edges.append({"s": n.name, "t": r.name})
                inbound[r.name] = inbound.get(r.name, 0) + 1
            elif not r:
                gid = f"ghost:{t}"
                ghosts.setdefault(gid, {"id": gid, "label": t, "ghost": True, "refs": 0})
                ghosts[gid]["refs"] += 1
                edges.append({"s": n.name, "t": gid, "broken": True})
    nodes = []
    for n in notes:
        ix = indexed.get(n.name)
        modified = (ix.modified if ix else None) or n.parsed.modified
        f_ids = flags.get(n.name, [])
        level = ("crit" if any(sev.get(i) == "crit" for i in f_ids) else
                 "warn" if any(sev.get(i) == "warn" for i in f_ids) else
                 "info" if f_ids else None)
        sx, sy = proj.get(n.name, (None, None))
        nodes.append({
            "id": n.name, "file": n.path.name, "type": n.type, "desc": n.description,
            "words": n.words, "priority": bool(n.parsed.priority),
            "modified": (modified or "")[:10] or None, "age": age_days(modified),
            "date_source": (ix.modified_source if ix else None)
            or ("frontmatter" if n.parsed.modified else "unknown"),
            "in": inbound.get(n.name, 0), "out": len([t for t in n.links if resolve(t)]),
            "flags": f_ids, "level": level, "sx": sx, "sy": sy, "indexed": ix is not None,
            "chunks": ix.chunks if ix else 0,
        })
    g = {
        "version": key, "at": report.get("at"), "source": src.label, "error": err,
        "stats": {"notes": len(notes), "words": sum(n.words for n in notes),
                  "links": len([e for e in edges if not e.get("broken")]),
                  "broken": len([e for e in edges if e.get("broken")]),
                  "chunks": sum(v.chunks for v in indexed.values()) if indexed else None,
                  "types": {t: sum(1 for n in notes if n.type == t)
                            for t in sorted({n.type for n in notes})}},
        "nodes": nodes, "ghosts": list(ghosts.values()), "edges": edges,
    }
    with _lock:
        _state["graph"], _state["graph_key"] = g, key
    return g


def note(name: str) -> dict:
    notes = rv.load_corpus(memory_dir())
    resolve = rv.Resolver(notes)
    n = resolve(name)
    if not n:
        raise HTTPException(404, "unknown note")
    inbound = sorted({m.name for m in notes if m.name != n.name
                      and any((r := resolve(t)) is not None and r.name == n.name
                              for t in m.links)})
    outbound = []
    for t in n.links:
        r = resolve(t)
        outbound.append({"target": t, "resolved": r.name if r else None})
    ix = None
    try:
        ix = source().indexed().get(n.name)
    except Exception:  # noqa: BLE001
        pass
    modified = (ix.modified if ix else None) or n.parsed.modified
    report = current()
    flags = []
    for f in report.get("findings", []):
        if n.name not in (f.get("notes") or []):
            continue
        items = [i for i in f.get("items") or [] if i.get("note") == n.name
                 or i.get("other") == n.name]
        flags.append({"id": f["id"], "severity": f["severity"], "category": f["category"],
                      "title": f["title"], "remedy": f["remedy"], "action": f.get("action"),
                      "judgement": f.get("judgement"), "items": items})
    return {
        "id": n.name, "file": n.path.name, "type": n.type, "desc": n.description,
        "words": n.words, "priority": bool(n.parsed.priority),
        "modified": (modified or "")[:10] or None, "age": age_days(modified),
        "date_source": (ix.modified_source if ix else None)
        or ("frontmatter" if n.parsed.modified else "unknown"),
        "chunks": ix.chunks if ix else None, "indexed": ix is not None,
        "warnings": [w for w in n.parsed.warnings if w != "empty description"],
        "body": n.body, "in": inbound, "out": outbound, "flags": flags,
    }


def overview() -> dict:
    rep = current()
    h = history()
    try:
        series, summary = h.series(), h.summary()
    except Exception:  # noqa: BLE001
        series, summary = [], {}
    pending = [p for p in proposals() if p["status"] == "pending"]
    return {"report": rep, "history": series, "summary": summary, "pending": len(pending),
            "running": _state["running"], "notify": notify.enabled(),
            "digest_at": h.get("digest_at") if hasattr(h, "get") else None}


# ----------------------------------------------------------------------------- proposals

PROPOSALS = DATA_DIR / "corthexis-proposals.json"
BACKUPS = DATA_DIR / "corthexis-backups"
_plock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(PROPOSALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    keep = dict(sorted(d.items(), key=lambda kv: kv[1].get("created_at", 0))[-200:])
    tmp = PROPOSALS.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(PROPOSALS)


def proposals() -> list[dict]:
    with _plock:
        d = _load()
    return sorted(d.values(), key=lambda p: -p.get("created_at", 0))


def get_proposal(pid: str) -> dict:
    p = _load().get(pid)
    if not p:
        raise HTTPException(404, "unknown proposal")
    return p


class ProposalIn(BaseModel):
    kind: str                 # relink | merge | rename | close
    note: str = ""            # rename / close; source note of a relink (optional)
    target: str = ""          # relink: the broken [[target]]
    new_target: str = ""      # relink: the note it should lead to
    keep: str = ""            # merge
    drop: str = ""            # merge
    new_name: str = ""        # rename (default: the convention)
    reason: str = ""          # close


def propose(body: ProposalIn, user: str) -> dict:
    mem = memory_dir()
    try:
        if body.kind == "relink":
            if not (body.target and body.new_target):
                raise rp.RepairError("relink needs the broken target and the note to point to")
            p = rp.propose_relink(mem, body.target, body.new_target,
                                  [body.note] if body.note else None)
        elif body.kind == "merge":
            p = rp.propose_merge(mem, body.keep, body.drop)
        elif body.kind == "rename":
            p = rp.propose_rename(mem, body.note, body.new_name or None)
        elif body.kind == "close":
            p = rp.propose_close(mem, body.note, body.reason)
        else:
            raise rp.RepairError(f"unknown repair {body.kind!r}")
    except rp.RepairError as e:
        raise HTTPException(400, str(e)) from e
    rec = {**p.to_dict(), "status": "pending", "created_by": user, "created_at": time.time()}
    with _plock:
        d = _load()
        d[p.id] = rec
        _save(d)
    audit.log(user, "memory.repair.propose", p.id, p.title)
    _arm_ping(p.id, p.title)
    return rec


def _arm_ping(pid: str, title: str) -> None:
    """Like a session permission: a proposal nobody answers pings the channels once."""
    if not notify.hitl_enabled():
        return

    def ping() -> None:
        try:
            if get_proposal(pid)["status"] == "pending":
                notify.send("SOKKAN — action required",
                            f"A memory repair is waiting for your approval: {title}",
                            f"{PUBLIC_URL}/?tab=corthexis&proposal={pid}", "hitl")
        except Exception:  # noqa: BLE001
            pass
    t = threading.Timer(PENDING_PING_S, ping)
    t.daemon = True
    t.start()


def decide(pid: str, approve: bool, user: str) -> dict:
    with _plock:
        d = _load()
        rec = d.get(pid)
        if not rec:
            raise HTTPException(404, "unknown proposal")
        if rec["status"] != "pending":
            raise HTTPException(409, f"proposal already {rec['status']}")
        rec.update(decided_by=user, decided_at=time.time())
        if not approve:
            rec["status"] = "refused"
            _save(d)
            audit.log(user, "memory.repair.refuse", pid, rec["title"])
            return rec
        try:
            written = rp.apply(memory_dir(), rp.Proposal.from_dict(rec), backup_dir=BACKUPS)
        except rp.Conflict as e:
            rec.update(status="conflict", error=str(e))
            _save(d)
            audit.log(user, "memory.repair.conflict", pid, f"{rec['title']} — {e}")
            raise HTTPException(409, f"{e} — the note was edited meanwhile; propose again") from e
        except (OSError, rp.RepairError) as e:
            rec.update(status="failed", error=str(e))
            _save(d)
            audit.log(user, "memory.repair.failed", pid, f"{rec['title']} — {e}")
            raise HTTPException(500, f"repair failed: {e}") from e
        rec.update(status="applied", written=written)
        _save(d)
    audit.log(user, "memory.repair.apply", pid, f"{rec['title']} ({len(written)} file(s))")
    threading.Thread(target=_after_repair, daemon=True, name="corthexis-after").start()
    return rec


def _after_repair() -> None:
    """Reindex what changed, then review again so the tab shows the result."""
    try:
        if _pg():
            from core import embed
            from core.indexer import IndexConfig, Indexer
            Indexer(_store(), embed.get(), IndexConfig.from_env(memory_dir=memory_dir()),
                    log=lambda m: None).run()
        elif reindex_hook:
            reindex_hook()
    except Exception as e:  # noqa: BLE001
        print(f"[corthexis] reindex after repair failed: {e}", file=sys.stderr)
    run(chain=False, alert=False)


# ----------------------------------------------------------------------------- curation

def curation_subject(finding_ids: list[str], notes: list[str]) -> str:
    rep = current()
    lines = []
    for f in rep.get("findings", []):
        if finding_ids and f["id"] not in finding_ids:
            continue
        if not finding_ids and not f.get("judgement"):
            continue
        sel = [n for n in f["notes"] if not notes or n in notes]
        if notes and not sel:
            continue
        lines.append(f"## {f['title']} ({f['severity']})\n{f['detail']}\nRemedy: {f['remedy']}")
        for it in (f.get("items") or [])[:25]:
            if notes and it.get("note") not in notes:
                continue
            extra = ", ".join(f"{k}: {v}" for k, v in it.items() if k != "note" and v not in
                              (None, "", []))
            lines.append(f"- {it.get('note', '')}" + (f" — {extra}" if extra else ""))
    if not lines:
        raise HTTPException(400, "no finding to curate")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- API

router = APIRouter(prefix="/api/corthexis")


def _require(min_role: str):
    def dep(user: dict = Depends(auth.current_user)) -> dict:
        if iam.rank(user["role"]) < iam.rank(min_role):
            raise HTTPException(403, f"role {min_role!r} required (you are {user['role']!r})")
        return user
    return dep


@router.get("/graph")
def api_graph(since: str = "", _u: dict = Depends(_require("viewer"))) -> dict:
    g = graph()
    if since and since == g["version"]:
        return {"version": g["version"], "unchanged": True}
    return g


@router.get("/note/{name}")
def api_note(name: str, _u: dict = Depends(_require("viewer"))) -> dict:
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid name")
    return note(name)


@router.get("/review")
def api_review(_u: dict = Depends(_require("viewer"))) -> dict:
    return overview()


@router.post("/review/run")
def api_review_run(u: dict = Depends(_require("dev"))) -> dict:
    rep = run(alert=False)
    audit.log(u["email"], "memory.review", "", f"score {rep.get('score')}")
    return overview()


@router.get("/proposals")
def api_proposals(_u: dict = Depends(_require("viewer"))) -> list[dict]:
    return proposals()


@router.post("/proposals")
def api_propose(body: ProposalIn, u: dict = Depends(_require("dev"))) -> dict:
    return propose(body, u["email"])


@router.post("/proposals/{pid}/approve")
def api_approve(pid: str, u: dict = Depends(_require("dev"))) -> dict:
    return decide(pid, True, u["email"])


@router.post("/proposals/{pid}/refuse")
def api_refuse(pid: str, u: dict = Depends(_require("dev"))) -> dict:
    return decide(pid, False, u["email"])


class CurationIn(BaseModel):
    finding_ids: list[str] = []
    notes: list[str] = []


@router.post("/curation")
def api_curation(body: CurationIn, u: dict = Depends(_require("dev"))) -> dict:
    if spawn_hook is None:
        raise HTTPException(503, "sessions are not available on this instance")
    subject = curation_subject(body.finding_ids, body.notes)
    prompt, tag = playbooks.render("curation", subject)
    s = spawn_hook(tag, prompt, title="Memory curation", user=u["email"])
    audit.log(u["email"], "memory.curation", s.get("session_id", ""),
              ",".join(body.finding_ids) or "judgement findings")
    return s
