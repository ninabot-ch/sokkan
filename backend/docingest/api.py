"""api.py — /api/doc-ingest/* : sources, passes, and the review of batches.

Project-scoped like every memory route (projectgate: header x-sokkan-project or ?project=).
A developer of the project reads and reviews (as for the unit quarantine); a maintainer
starts and stops passes. A note above the reviewer's clearance does not exist for them
(it is only counted as hidden) and is never approved by their batch approval. 404 everywhere
when the feature is off. The allow-list itself is NOT editable here (config.py).
"""
from __future__ import annotations

import csv
import io
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import audit
import auth
import classification
import features
import iam

from . import batches, llmclient, scheduler, state
from . import config as cfgmod

router = APIRouter(prefix="/api/doc-ingest")


def _on() -> None:
    if not features.enabled("doc_ingest"):
        raise HTTPException(404, "feature disabled on this instance")


Dep = Depends(auth.current_user)
On = Depends(_on)


def _project(u: dict) -> str:
    return u.get("project") or "default"


def _need(u: dict, role: str) -> None:
    if iam.rank(u.get("role") or "") < iam.rank(role):
        label = {"dev": "developer", "admin": "maintainer"}.get(role, role)
        raise HTTPException(403, f"you need the {label} role in this project")


def _clearance() -> int | None:
    return classification.ctx_clearance()


def _sources(u: dict) -> list:
    try:
        return [s for s in cfgmod.load() if s.project == _project(u)]
    except cfgmod.ConfigError as e:
        raise HTTPException(500, f"doc-ingest configuration: {e}") from None


def _source(u: dict, sid: str):
    s = next((x for x in _sources(u) if x.id == sid), None)
    if s is None:
        raise HTTPException(404, "source not found")
    return s


def _batch(u: dict, bid: int) -> dict:
    b = batches.get(bid, _project(u))
    if b is None:
        raise HTTPException(404, "batch not found")
    return b


# ---- status, sources, passes --------------------------------------------------------------
@router.get("/status")
def status(u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    ep = llmclient.endpoint()
    return {"enabled": True, "model": {"configured": bool(ep["url"] and ep["model"]),
                                       "model": ep["model"], "not_ready": llmclient.ready()},
            "review_min_sample": batches.review_min_sample(),
            "pending_batches": len(batches.list_batches(_project(u), status="pending"))}


@router.get("/sources")
def sources_list(u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    sched = scheduler.status()
    out = []
    for s in _sources(u):
        d = s.public() | {"trust": batches.trust(s), **sched.get(s.id, {})}
        last = state.pass_list(source=s.id, limit=1)
        d["last_pass"] = last[0] if last else None
        out.append(d)
    return {"sources": out}


class RunIn(BaseModel):
    kind: str = "manual"


@router.post("/sources/{sid}/run", status_code=202)
def source_run(sid: str, body: RunIn | None = None, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    s = _source(u, sid)
    why = llmclient.ready()
    if why:
        raise HTTPException(409, why)
    kind = body.kind if body and body.kind in ("initial", "incremental", "manual") else "manual"
    try:
        out = scheduler.start_pass(s.id, kind, u["email"])
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    audit.log(u["email"], "doc_ingest.pass.start", s.id, kind)
    return out


@router.post("/sources/{sid}/stop")
def source_stop(sid: str, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "admin")
    s = _source(u, sid)
    stopped = scheduler.stop_pass(s.id, u["email"])
    audit.log(u["email"], "doc_ingest.pass.stop", s.id, "")
    return {"stopping": stopped}


@router.get("/passes")
def passes(source: str | None = None, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    return {"passes": state.pass_list(source=source, project=_project(u))}


@router.get("/passes/{pid}")
def pass_detail(pid: int, outcome: str | None = None, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    p = state.pass_get(pid)
    if p is None or p["project"] != _project(u):
        raise HTTPException(404, "pass not found")
    return {"pass": p, "events": state.events(pid, outcome)}


# ---- batches: review ------------------------------------------------------------------------
@router.get("/batches")
def batches_list(status: str | None = None, source: str | None = None, u: dict = Dep,
                 _f: None = On) -> dict:
    _need(u, "dev")
    return {"batches": batches.list_batches(_project(u), status, source)}


@router.get("/batches/{bid}")
def batch_stats(bid: int, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    _batch(u, bid)
    out = batches.stats(bid, _clearance())
    out["viewed_by_you"] = batches.viewed(bid, u["email"])
    out["review_min_sample"] = batches.review_min_sample()
    return out


@router.get("/batches/{bid}/sample")
def batch_sample(bid: int, n: int = batches.SAMPLE_DEFAULT, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    b = _batch(u, bid)
    notes = batches.sample(bid, u["email"], max(1, min(n, 50)), _clearance())
    batches.journal(u["email"], b, "sample", "", "", {"shown": [x["name"] for x in notes]})
    return {"notes": notes, "viewed_by_you": batches.viewed(bid, u["email"])}


@router.get("/batches/{bid}/notes")
def batch_notes(bid: int, status: str | None = None, flag: str | None = None, offset: int = 0,
                limit: int = 200, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    _batch(u, bid)
    return {"notes": batches.notes_of(bid, status, flag, _clearance(), max(0, offset),
                                      max(1, min(limit, 1000)))}


@router.get("/batches/{bid}/notes/{name}")
def batch_note(bid: int, name: str, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    _batch(u, bid)
    n = batches.note(bid, name, _clearance(), viewer=u["email"])
    if n is None:
        raise HTTPException(404, "note not found")
    return n


class Decision(BaseModel):
    reason: str = ""


def _decide(fn, *a, **kw) -> dict:
    import store_backend
    try:
        return fn(*a, **kw)
    except KeyError:
        raise HTTPException(404, "not found") from None
    except batches.ReviewError as e:
        raise HTTPException(409, str(e)) from None
    except store_backend.StoreRequired as e:
        raise HTTPException(409, {"code": e.code, "message": str(e)}) from None


@router.post("/batches/{bid}/approve")
def batch_approve(bid: int, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    _batch(u, bid)
    return _decide(batches.approve_batch, bid, u["email"], _clearance(), _project(u))


@router.post("/batches/{bid}/reject")
def batch_reject(bid: int, body: Decision | None = None, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    _batch(u, bid)
    return _decide(batches.reject_batch, bid, u["email"], (body.reason if body else ""),
                   _clearance(), _project(u))


@router.post("/batches/{bid}/notes/{name}/approve")
def note_approve(bid: int, name: str, u: dict = Dep, _f: None = On) -> dict:
    _need(u, "dev")
    _batch(u, bid)
    return _decide(batches.approve_note, bid, name, u["email"], _clearance(), _project(u))


@router.post("/batches/{bid}/notes/{name}/reject")
def note_reject(bid: int, name: str, body: Decision | None = None, u: dict = Dep,
                _f: None = On) -> dict:
    _need(u, "dev")
    _batch(u, bid)
    return _decide(batches.reject_note, bid, name, u["email"], (body.reason if body else ""),
                   _clearance(), _project(u))


# ---- the decision journal (a signal for whoever studies the review) ---------------------------
@router.get("/decisions")
def decisions(batch: int | None = None, format: str = "json", u: dict = Dep, _f: None = On):
    _need(u, "admin")
    rows = batches.decisions(_project(u), batch)
    if format != "csv":
        return {"decisions": rows}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "iso", "user", "source", "batch", "note", "action", "reason", "detail"])
    for r in reversed(rows):
        w.writerow([r["ts"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["ts"])), r["user"],
                    r["source"], r["batch_id"], r["note"], r["action"], r["reason"],
                    str(r["detail"])])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")
