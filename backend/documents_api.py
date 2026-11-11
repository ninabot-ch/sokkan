#!/usr/bin/env python3
"""documents_api.py — HTTP routes of the document drop (feature `documents`, 404 when off).

    GET    /api/documents                 the selected project's documents      (viewer)
    GET    /api/documents/limits          accepted types and sizes              (anyone signed in)
    PUT    /api/documents?name=…          upload — the raw file is the body     (dev)
             [&session=<sid>]             … and attach it to that session
             [&modified=<epoch ms>]       the file's own date (age shown next to a source)
    GET    /api/documents/{id}            one document                          (viewer)
    GET    /api/documents/{id}/content    download                              (viewer)
    GET    /api/documents/{id}/text       its text copy (preview)               (viewer)
    POST   /api/documents/{id}/attach     {session_id} attach to a session      (dev, session owner)
    DELETE /api/documents/{id}            remove (who added it, or a project admin)

None of these paths is a project route for the middleware (projectgate): each route decides
with the caller's role in the document's own project (an upload / a list: the project the
cockpit selected, header ``x-sokkan-project``). No role there, a document of another
project or above the caller's clearance: 404. The body is a raw stream on purpose — no
multipart parser, and the size ceiling is enforced while the bytes arrive.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import audit
import auth
import board
import documents
import features
import iam
import projectgate

router = APIRouter()


def _feature() -> None:
    if not features.enabled("documents"):
        raise HTTPException(404, "feature disabled on this instance")


def _in_project(request: Request, slug: str, min_role: str = "viewer") -> dict:
    """The caller as the project ``slug`` sees them, or 404 / 403."""
    pu = projectgate.project_user(auth.instance_user(request), slug)
    if pu is None:
        raise HTTPException(404, "not found")
    if iam.rank(pu["role"]) < iam.rank(min_role):
        raise HTTPException(403, f"role {min_role!r} required in this project")
    return pu


def _doc_for(request: Request, doc_id: str, min_role: str = "viewer") -> tuple[dict, dict]:
    d = documents.get(doc_id)
    if d is None:
        raise HTTPException(404, "not found")
    pu = _in_project(request, d["project"], min_role)
    if not documents.visible(d, pu.get("clearance")):
        raise HTTPException(404, "not found")
    return d, pu


def _refuse(e: documents.DocumentError, user: str, project: str, name: str) -> None:
    audit.log(user, "document.refused", detail=f"{e.code}: {name[:120]!r} — {e.detail}"[:400],
              project=project)
    raise HTTPException(e.status, {"code": e.code, "message": e.detail})


def session_for_attach(pu: dict, session_id: str, project: str) -> None:
    """A document goes to a session of the same project, opened by the caller (a project
    admin may attach to anybody's)."""
    if board.get_session_project(session_id) != project:
        raise HTTPException(404, "session not found")
    owner = board.get_session_owner(session_id)
    if owner and owner != (pu.get("email") or "").lower() and iam.rank(pu["role"]) < iam.rank("admin"):
        raise HTTPException(403, "only the person who opened the session attaches documents to it")
    if projectgate.above_clearance(pu, "session", session_id):
        raise HTTPException(404, "session not found")


@router.get("/api/documents/limits")
def doc_limits(_u: dict = Depends(auth.current_user), _f=Depends(_feature)) -> dict:
    return documents.limits()


@router.get("/api/documents")
def doc_list(request: Request, _f=Depends(_feature)) -> dict:
    slug = projectgate.requested_project(request)
    pu = _in_project(request, slug)
    uses = documents.session_counts(slug)
    return {"project": slug, "limits": documents.limits(),
            "documents": [documents.public(d, uses.get(d["id"], 0))
                          for d in documents.list_project(slug, pu.get("clearance"))]}


@router.put("/api/documents")
async def doc_upload(request: Request, name: str, session: str = "", modified: float = 0,
                     _f=Depends(_feature)) -> dict:
    slug = projectgate.requested_project(request)
    pu = _in_project(request, slug, "dev")
    user = pu.get("email") or ""
    if session:
        session_for_attach(pu, session, slug)
    limit = documents.max_bytes()
    try:
        documents.safe_name(name)          # type refused before a byte is stored
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise documents.DocumentError(
                413, "too_large", f"file larger than {limit // (1024 * 1024)} MB")
    except documents.DocumentError as e:
        _refuse(e, user, slug, name)
    tmp = documents.incoming_path(slug)
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:           # the declared length is not trusted
                    raise documents.DocumentError(
                        413, "too_large", f"file larger than {limit // (1024 * 1024)} MB")
                f.write(chunk)
        doc = documents.add_file(slug, name, tmp, owner=user,
                                 modified_at=(modified / 1000.0) if modified else None)
    except documents.DocumentError as e:
        tmp.unlink(missing_ok=True)
        _refuse(e, user, slug, name)
    except BaseException:
        tmp.unlink(missing_ok=True)       # client went away mid-upload: nothing is kept
        raise
    if not documents.attachable(doc) and session:
        session = ""
    if session:
        documents.attach(session, doc["id"], by=user)
    audit.log(user, "document.upload", doc["id"],
              f"{doc['name']} · {doc['size']} bytes · sha256 {doc['sha256'][:16]}"
              + (f" · session {session}" if session else ""), project=slug)
    return {**documents.public(doc), "attached_to": session or None}


@router.get("/api/documents/{doc_id}")
def doc_get(doc_id: str, request: Request, _f=Depends(_feature)) -> dict:
    d, _pu = _doc_for(request, doc_id)
    return documents.public(d, documents.session_counts(d["project"]).get(d["id"], 0))


@router.get("/api/documents/{doc_id}/content")
def doc_content(doc_id: str, request: Request, _f=Depends(_feature)):
    d, pu = _doc_for(request, doc_id)
    try:
        p = documents.path_of(d)
    except documents.DocumentError:
        raise HTTPException(404, "not found")
    if not p.is_file():
        raise HTTPException(404, "not found")
    audit.log(pu.get("email") or "", "document.download", d["id"], d["name"], project=d["project"])
    ascii_name = d["name"].encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    return FileResponse(p, media_type=documents.MIME[d["ext"]], headers={
        # always a download, never rendered by the browser from this origin
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; "
                               f"filename*=UTF-8''{quote(d['name'])}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
        "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.get("/api/documents/{doc_id}/text")
def doc_text(doc_id: str, request: Request, limit: int = 20000, _f=Depends(_feature)) -> dict:
    d, _pu = _doc_for(request, doc_id)
    t = documents.text_path_of(d)
    text = ""
    if t is not None and t.is_file():
        with open(t, encoding="utf-8", errors="replace") as f:
            text = f.read(max(0, min(int(limit), 200000)))
    return {"id": d["id"], "name": d["name"], "state": d["text_state"], "chars": d["text_chars"],
            "text": text, "truncated": d["text_chars"] > len(text)}


class AttachIn(BaseModel):
    session_id: str


@router.post("/api/documents/{doc_id}/attach")
def doc_attach(doc_id: str, body: AttachIn, request: Request, _f=Depends(_feature)) -> dict:
    d, pu = _doc_for(request, doc_id, "dev")
    session_for_attach(pu, body.session_id, d["project"])
    if not documents.attachable(d):
        raise HTTPException(409, {"code": "classified", "message":
                                  "a document above the default level cannot go to an agent session"})
    documents.attach(body.session_id, d["id"], by=pu.get("email") or "")
    audit.log(pu.get("email") or "", "document.attach", d["id"],
              f"{d['name']} → session {body.session_id}", project=d["project"])
    return {"ok": True}


@router.delete("/api/documents/{doc_id}")
def doc_delete(doc_id: str, request: Request, _f=Depends(_feature)) -> dict:
    d, pu = _doc_for(request, doc_id, "dev")
    me = (pu.get("email") or "").lower()
    if d["owner"] != me and iam.rank(pu["role"]) < iam.rank("admin"):
        raise HTTPException(403, "only the person who added the document, or a project admin, removes it")
    documents.delete(d["id"])
    audit.log(me, "document.delete", d["id"], d["name"], project=d["project"])
    return {"ok": True}
