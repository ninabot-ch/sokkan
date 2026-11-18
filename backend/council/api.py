#!/usr/bin/env python3
"""council/api.py — routes of the launch council (feature `council`, 404 when off).

    GET  /api/council/config               budget defaults, the nine roles, may this project use Claude
    GET  /api/council                      the selected project's councils                  (viewer)
    POST /api/council                      open a council: brief, documents, budget, Claude choice   (dev)
    GET  /api/council/{id}?after=N         the council, its roles, documents, journal (poll with `after`)
    POST /api/council/{id}/documents       {name, text} or {doc_id} (a project document)     (dev, state brief)
    POST /api/council/{id}/start           the three rounds run in the background             (dev)
    POST /api/council/{id}/say             {text} — the CEO writes, at any time              (dev)
    POST /api/council/{id}/stop            the CEO stops it                                   (dev)
    POST /api/council/{id}/budget          raise the ceiling after stopped_budget and resume where it stopped (owner/admin)
    POST /api/council/{id}/resume          resume the round of an interrupted council (restart)  (owner/admin)
    POST /api/council/{id}/accept          accept the card: board cards, agent DRAFTS, positions (owner/admin)
    POST /api/council/{id}/refuse          {reason} — the DAF writes a new synthesis (twice at most)
    GET  /api/council/{id}/export?format=json|md   replay / file
    GET  /api/council/{id}/positions       the open human positions

The project of a council is the one it was opened in; the caller's role IN THAT PROJECT decides.
A council above the caller's clearance does not exist for them (404).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

import audit
import auth
import features
import iam
import projectgate

from . import export, models, roles, service, store

router = APIRouter()


def _feature() -> None:
    if not features.enabled("council"):
        raise HTTPException(404, "feature disabled on this instance")


def _pu(request: Request, slug: str, min_role: str = "viewer") -> dict:
    pu = projectgate.project_user(auth.instance_user(request), slug)
    if pu is None:
        raise HTTPException(404, "not found")
    if iam.rank(pu["role"]) < iam.rank(min_role):
        raise HTTPException(403, f"role {min_role!r} required in this project")
    return pu


def _council(request: Request, cid: str, min_role: str = "viewer") -> tuple[dict, dict]:
    c = store.get(cid)
    if c is None:
        raise HTTPException(404, "not found")
    pu = _pu(request, c["project"], min_role)
    clr = pu.get("clearance")
    if clr is not None and c["level"] > clr:
        raise HTTPException(404, "not found")
    return c, pu


def _owner_or_admin(c: dict, pu: dict) -> None:
    if (pu.get("email") or "").lower() != (c["owner"] or "").lower() and iam.rank(pu["role"]) < iam.rank("admin"):
        raise HTTPException(403, "only the person who opened the council, or a project admin, decides")


def _fail(e: service.CouncilError):
    body = {"detail": str(e)}
    if e.code:
        body["code"] = e.code
    return JSONResponse(body, status_code=e.status)


def public(c: dict, with_card: bool = True) -> dict:
    d = {k: v for k, v in c.items() if k not in ("pii_map", "stop_requested")}
    d["speaking"] = c["speaking"] if c["state"] in store.LIVE else []
    d["budget"] = {"max_tokens": c["max_tokens"], "max_cost_chf": c["max_cost_chf"],
                   "tokens": c["tokens_in"] + c["tokens_out"], "cost_chf": round(c["cost_chf"], 4), "calls": c["calls"]}
    if not with_card:
        d.pop("card", None)
    return d


class OpenIn(BaseModel):
    title: str = ""
    brief: str
    lang: str = "fr"
    claude: bool = False
    claude_scope: str = "daf"
    claude_model: str = ""
    pii_ack: bool = False
    max_tokens: int | None = None
    max_cost_chf: float | None = None
    documents: list[dict] = Field(default_factory=list)      # [{name, text}]
    doc_ids: list[str] = Field(default_factory=list)         # documents of the project (feature `documents`)


class DocIn(BaseModel):
    name: str = ""
    text: str = ""
    doc_id: str = ""


class SayIn(BaseModel):
    text: str


class RefuseIn(BaseModel):
    reason: str


def documents_on() -> bool:
    try:
        import documents
        return bool(documents.enabled())
    except Exception:  # noqa: BLE001
        return False


def _project_docs(pu: dict, slug: str, ids: list[str]) -> list[dict]:
    import claudepii
    import documents
    if ids and not documents.enabled():
        raise HTTPException(409, {"code": "documents_off", "message": "project documents are not offered on this instance "
                                  "(feature `documents`): paste the text instead"})
    out = []
    for i in ids[:20]:
        d = documents.get(i)
        if d is None or d["project"] != slug or not documents.visible(d, pu.get("clearance")):
            raise HTTPException(404, f"document not found: {i}")
        out.append({"name": d["name"], "text": claudepii.doc_text(d)[:200_000], "source_doc_id": d["id"]})
    return out


@router.get("/api/council/config")
def config(request: Request, lang: str = "fr", _f=Depends(_feature)) -> dict:
    slug = projectgate.requested_project(request)
    _pu(request, slug)
    lg = lang if lang in ("fr", "en") else "fr"
    g = models.claude_gate(slug, "", lg)
    import claudetier
    from core import levels
    lvl = claudetier.project_level(slug)
    return {"roles": roles.public(lang if lang in roles.LANGS else "fr"), "defaults": service.defaults(),
            "claude": {"allowed": g["allowed"], "code": g["code"], "reason": g["reason"], "model": g["model"]},
            "limits": {"parallel": service.MAX_PARALLEL, "max_refusals": service.MAX_REFUSALS},
            # the project's classification, settable from the room (PUT /api/projects/{slug}/level,
            # whether or not Claude is offered) and whether the project's documents can be attached
            "project": {"slug": slug, "level": levels.ident(lvl), "label": claudetier.level_label(lvl, lg)},
            "documents": documents_on()}


@router.get("/api/council")
def listing(request: Request, _f=Depends(_feature)) -> dict:
    slug = projectgate.requested_project(request)
    pu = _pu(request, slug)
    clr = pu.get("clearance")
    return {"project": slug, "councils": [public(c, with_card=False) for c in store.list_project(slug)
                                          if clr is None or c["level"] <= clr]}


@router.post("/api/council")
def open_council(body: OpenIn, request: Request, _f=Depends(_feature)):
    slug = projectgate.requested_project(request)
    pu = _pu(request, slug, "dev")
    lang = body.lang if body.lang in roles.LANGS else "fr"
    docs = [{"name": str(d.get("name") or "document"), "text": str(d.get("text") or "")} for d in body.documents]
    docs += _project_docs(pu, slug, body.doc_ids)
    try:
        c = service.create(project=slug, owner=pu["email"], title=body.title, brief=body.brief, lang=lang,
                           claude=body.claude, claude_scope=body.claude_scope, claude_model=body.claude_model,
                           pii_ack=body.pii_ack, max_tokens=body.max_tokens, max_cost_chf=body.max_cost_chf,
                           documents=docs)
    except service.CouncilError as e:
        if e.code == "claude_pii":
            counts = service.pii_counts(slug, pu["email"], body.brief, docs)
            return JSONResponse({"detail": str(e), "code": e.code, "found": counts}, status_code=409)
        return _fail(e)
    audit.log(pu["email"], "council.open", c["id"], f"claude={body.claude}", project=slug)
    return public(c)


@router.get("/api/council/{cid}")
def one(cid: str, request: Request, after: int = 0, lang: str = "", _f=Depends(_feature)) -> dict:
    _council(request, cid)
    service.reconcile(cid)             # a worker lost to a restart: interrupted, never left « live »
    c, _pu_ = _council(request, cid)
    lg = lang if lang in roles.LANGS else c["lang"]
    return {"council": public(c), "roles": roles.public(lg), "documents": store.documents(cid, with_text=False),
            "journal": store.turns(cid, after)}


@router.post("/api/council/{cid}/documents")
def add_document(cid: str, body: DocIn, request: Request, _f=Depends(_feature)):
    c, pu = _council(request, cid, "dev")
    if c["state"] != "brief":
        raise HTTPException(409, "documents are attached before the council starts")
    if c["claude"]:
        raise HTTPException(409, "documents cannot be added to a council that uses Claude (the personal-data check is made at opening)")
    if body.doc_id:
        d = _project_docs(pu, c["project"], [body.doc_id])[0]
    else:
        d = {"name": body.name or "document", "text": body.text, "source_doc_id": ""}
    if not d["text"].strip():
        raise HTTPException(400, "empty document")
    if len(store.documents(cid, with_text=False)) >= 20:
        raise HTTPException(409, "20 documents at most")
    return store.add_document(cid, d["name"][:120], d["text"], d.get("source_doc_id", ""))


@router.post("/api/council/{cid}/start")
def start(cid: str, request: Request, _f=Depends(_feature)):
    c, pu = _council(request, cid, "dev")
    _owner_or_admin(c, pu)
    try:
        service.start_async(cid)
    except service.CouncilError as e:
        return _fail(e)
    audit.log(pu["email"], "council.start", cid, project=c["project"])
    return public(store.get(cid))


@router.post("/api/council/{cid}/say")
def say(cid: str, body: SayIn, request: Request, _f=Depends(_feature)):
    c, pu = _council(request, cid, "dev")
    _owner_or_admin(c, pu)
    try:
        return service.say(cid, body.text, pu["email"])
    except service.CouncilError as e:
        return _fail(e)


@router.post("/api/council/{cid}/stop")
def stop(cid: str, request: Request, _f=Depends(_feature)):
    c, pu = _council(request, cid, "dev")
    _owner_or_admin(c, pu)
    try:
        r = service.stop(cid)
    except service.CouncilError as e:
        return _fail(e)
    audit.log(pu["email"], "council.stop", cid, project=c["project"])
    return public(r)


@router.post("/api/council/{cid}/resume")
def resume(cid: str, request: Request, _f=Depends(_feature)):
    c, pu = _council(request, cid, "dev")
    _owner_or_admin(c, pu)
    try:
        r = service.resume(cid)
    except service.CouncilError as e:
        return _fail(e)
    audit.log(pu["email"], "council.resume", cid, f"step={r['resume_step']}", project=c["project"])
    return public(r)


class BudgetIn(BaseModel):
    max_tokens: int | None = None
    max_cost_chf: float | None = None


@router.post("/api/council/{cid}/budget")
def raise_budget(cid: str, body: BudgetIn, request: Request, _f=Depends(_feature)):
    c, pu = _council(request, cid, "dev")
    _owner_or_admin(c, pu)
    try:
        r = service.raise_budget(cid, body.max_tokens, body.max_cost_chf, pu["email"])
    except service.CouncilError as e:
        return _fail(e)
    audit.log(pu["email"], "council.budget_raise", cid,
              f"tokens {c['max_tokens']}->{r['max_tokens']} chf {c['max_cost_chf']}->{r['max_cost_chf']}", project=c["project"])
    return public(r)


@router.post("/api/council/{cid}/accept")
def accept(cid: str, request: Request, _f=Depends(_feature)):
    c, pu = _council(request, cid, "dev")
    _owner_or_admin(c, pu)
    try:
        r = service.accept(cid, pu)
    except service.CouncilError as e:
        return _fail(e)
    a = r["accepted"]
    audit.log(pu["email"], "council.accept", cid,
              f"cards={len(a['cards'])} agent_drafts={len(a['agents'])}", project=c["project"])
    return public(r)


@router.post("/api/council/{cid}/refuse")
def refuse(cid: str, body: RefuseIn, request: Request, _f=Depends(_feature)):
    c, pu = _council(request, cid, "dev")
    _owner_or_admin(c, pu)
    try:
        r = service.refuse(cid, body.reason)
    except service.CouncilError as e:
        return _fail(e)
    audit.log(pu["email"], "council.refuse", cid, f"refusals={r['refusals']}", project=c["project"])
    return public(r)


@router.get("/api/council/{cid}/positions")
def positions(cid: str, request: Request, _f=Depends(_feature)) -> dict:
    c, _pu_ = _council(request, cid)
    return {"positions": c["positions"]}


@router.get("/api/council/{cid}/export")
def export_council(cid: str, request: Request, format: str = "json", _f=Depends(_feature)):
    c, pu = _council(request, cid)
    audit.log(pu["email"], "council.export", cid, format, project=c["project"])
    if format == "md":
        return PlainTextResponse(export.markdown(cid), media_type="text/markdown; charset=utf-8",
                                 headers={"content-disposition": f'attachment; filename="{cid}.md"'})
    if format != "json":
        raise HTTPException(400, "format: json or md")
    return JSONResponse(export.snapshot(cid), headers={"content-disposition": f'attachment; filename="{cid}.json"'})
