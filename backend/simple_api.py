#!/usr/bin/env python3
"""simple_api.py — HTTP routes of the simple profile (feature `simple_profile`).

    GET  /api/me/prefs                      profile / language / theme of the caller
    PUT  /api/me/prefs                      change them (profile: unless SOKKAN_PROFILE_LOCKED)
    PUT  /api/admin/prefs/{email}           an instance admin sets somebody's profile

    GET  /api/simple/home                   greeting, task templates, recent works, gauge
    GET  /api/simple/works                  every work of the caller, with its deliverables
    POST /api/simple/start                  {ask, playbook?, documents[], lang} → a session
    GET  /api/simple/sessions/{sid}         one work: title, documents, deliverables
    POST /api/simple/sessions/{sid}/message {text, documents[]} a further message
    GET  /api/simple/sessions/{sid}/deliverables
    POST /api/simple/sessions/{sid}/deliverables            {title, text} keep an answer
    GET  /api/simple/sessions/{sid}/deliverables/{name}     preview (text)
    GET  /api/simple/sessions/{sid}/deliverables/{name}/download?format=md|docx
    POST /api/simple/sessions/{sid}/deliverables/{name}/validate

Everything under /api/simple answers 404 when the feature is off. As for the documents,
these paths carry no project context from the middleware: a list or a start is about the
project the cockpit selected, a session route about THAT session's project, with the
caller's role there (none = 404). Driving a work (message, save, validate) is for the
person who opened it, or a project admin.

The thread itself is the existing session WebSocket (`/api/agent/ws/{sid}?view=simple`,
app.py), which translates events with `plain.View` built by `make_view` below.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

import agentchat
import audit
import auth
import board
import budgets
import documents
import features
import iam
import plain
import playbooks
import projectgate
import simple
import uiprefs


def make_view(sid: str) -> plain.View:
    """The plain-language translator of one connection to session ``sid``."""
    project = board.get_session_project(sid) or "default"
    try:
        deliv = simple.deliverable_dir(project, sid)
    except ValueError:
        deliv = None

    def lookup(doc_id: str):
        d = documents.get(doc_id)
        return d if d and d["project"] == project else None

    return plain.View(docs_dir=documents.docs_dir(project), deliv_dir=deliv, doc_lookup=lookup,
                      on_write=lambda name, tool_id: simple.note_write(sid, name, tool_id),
                      attached=len(documents.of_session(sid)))


def edited_input(session, permission_id: str, draft_text) -> dict | None:
    """The person corrected the draft before allowing its save: the pending Write's own input
    with its content replaced. Anything else (another tool, an unknown request, no text, a
    text too large) → None = the input the agent asked for, unchanged. The path is never the
    browser's to choose; a confined session re-checks the result as any edited input."""
    if not isinstance(draft_text, str) or not draft_text.strip() \
            or len(draft_text.encode("utf-8")) > simple.MAX_DELIVERABLE:
        return None
    for ev in reversed(session.events):
        if ev.get("type") == "permission" and ev.get("id") == permission_id:
            inp = ev.get("input")
            if ev.get("tool") == "Write" and isinstance(inp, dict) and isinstance(inp.get("content"), str):
                return {**inp, "content": draft_text}
            return None
    return None


class PrefsIn(BaseModel):
    profile: str | None = None
    lang: str | None = None
    theme: str | None = None


class StartIn(BaseModel):
    ask: str = ""
    playbook: str = ""
    documents: list[str] = []
    lang: str = "fr"


class MessageIn(BaseModel):
    text: str
    documents: list[str] = []


class SaveIn(BaseModel):
    title: str = ""
    text: str


def router(*, bg, preseed, shown_name) -> APIRouter:
    """``bg(coro)`` runs a turn in the background (app._bg), ``preseed(query, session_id=)``
    is the deterministic memory recall of a spawn (app._memory_preseed), ``shown_name(request,
    user)`` the person's display name (app._shown_name)."""
    r = APIRouter()

    def _feature() -> None:
        if not features.enabled("simple_profile"):
            raise HTTPException(404, "feature disabled on this instance")

    def _in_project(request: Request, slug: str, min_role: str = "viewer") -> dict:
        pu = projectgate.project_user(auth.instance_user(request), slug)
        if pu is None:
            raise HTTPException(404, "not found")
        if iam.rank(pu["role"]) < iam.rank(min_role):
            raise HTTPException(403, f"role {min_role!r} required in this project")
        return pu

    def _session(request: Request, sid: str, drive: bool = False) -> tuple[dict, str]:
        """→ (caller in the session's project, project). 404 for anything not theirs to see."""
        project = board.get_session_project(sid)
        if project is None:
            raise HTTPException(404, "not found")
        pu = _in_project(request, project, "dev" if drive else "viewer")
        if projectgate.above_clearance(pu, "session", sid):
            raise HTTPException(404, "not found")
        if drive:
            owner = board.get_session_owner(sid)
            if owner and owner != (pu.get("email") or "").lower() \
                    and iam.rank(pu["role"]) < iam.rank("admin"):
                raise HTTPException(403, "only the person who opened this work drives it")
        return pu, project

    def _docs(ids: list[str], project: str, pu: dict) -> list[dict]:
        out = []
        for i in dict.fromkeys(ids or []):
            d = documents.get(i)
            if d is None or d["project"] != project or not documents.visible(d, pu.get("clearance")):
                raise HTTPException(404, f"document not found: {i[:32]}")
            if not documents.attachable(d):
                raise HTTPException(409, {"code": "classified", "message":
                                          f"« {d['name']} » is above the default level: it cannot "
                                          "go to an agent session"})
            out.append(d)
        return out

    def _state(sid: str) -> str:
        a = agentchat.peek(sid)
        if a is None:
            return "idle"
        if a._perms or a._questions:
            return "waiting"
        return "working" if a._busy else "idle"

    def _row(s: dict, work: dict | None, with_deliverables: bool = False) -> dict:
        sid = s["session_id"]
        a = agentchat.peek(sid)
        line = plain.last_line(a.events) if a is not None else ""
        if line and work:
            simple.note_last_line(sid, line)
        dl = simple.list_deliverables(s.get("project") or "default", sid)
        out = {"sid": sid, "title": s.get("title") or "", "state": _state(sid),
               "last_line": line or (work or {}).get("last_line") or "",
               "created_at": s.get("created_at"),
               "updated_at": max([x["updated_at"] for x in dl]
                                 + [(work or {}).get("last_at") or 0, s.get("created_at") or 0]),
               "deliverables": len(dl),
               "validated": bool(dl) and all(x["validated"] for x in dl),
               "to_validate": sum(1 for x in dl if not x["validated"])}
        if with_deliverables:
            out["items"] = dl
        return out

    def _mine(project: str, email: str) -> list[dict]:
        return [s for s in board.list_sessions()
                if (s.get("project") or "default") == project and s.get("kind") == "sdk"
                and (s.get("owner") or "") == email]

    # ---- preferences -----------------------------------------------------------------------
    @r.get("/api/me/prefs")
    def prefs_get(user: dict = Depends(auth.current_user)) -> dict:
        return uiprefs.get(user["email"])

    @r.put("/api/me/prefs")
    def prefs_set(body: PrefsIn, request: Request, _f=Depends(_feature)) -> dict:
        user = auth.instance_user(request)
        if body.profile is not None and uiprefs.locked() \
                and iam.rank(user.get("role") or "") < iam.rank("admin"):
            raise HTTPException(403, "the profile is set by an administrator on this instance")
        try:
            out = uiprefs.set_prefs(user["email"], profile=body.profile, lang=body.lang,
                                    theme=body.theme)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if body.profile is not None:
            audit.log(user["email"], "profile.set", user["email"], body.profile, project="")
        return out

    @r.put("/api/admin/prefs/{email}")
    def prefs_admin(email: str, body: PrefsIn, request: Request, _f=Depends(_feature)) -> dict:
        admin = auth.instance_user(request)
        if iam.rank(admin.get("role") or "") < iam.rank("admin"):
            raise HTTPException(403, "role 'admin' required")
        if body.profile is None:
            raise HTTPException(400, "profile: simple | technical")
        try:
            out = uiprefs.set_prefs(email, profile=body.profile)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.log(admin["email"], "profile.set", email.lower().strip(), body.profile, project="")
        return out

    # ---- home ------------------------------------------------------------------------------
    @r.get("/api/simple/home")
    def home(request: Request, lang: str = "fr", _f=Depends(_feature)) -> dict:
        slug = projectgate.requested_project(request)
        pu = _in_project(request, slug)
        email = (pu.get("email") or "").lower()
        name = (shown_name(request, pu) or "").strip()
        first = "" if "@" in name else name.split(" ")[0]
        mine = _mine(slug, email)
        works = simple.works_by_sid([s["session_id"] for s in mine[:40]])
        recents = [_row(s, works.get(s["session_id"])) for s in mine[:40]]
        recents.sort(key=lambda x: x["updated_at"] or 0, reverse=True)
        try:
            import assistant
            nina = features.enabled("assistant") and assistant.configured()
        except Exception:  # noqa: BLE001
            nina = False
        return {"first_name": first, "project": slug,
                "can_start": iam.rank(pu["role"]) >= iam.rank("dev"),
                "playbooks": playbooks.catalog("simple", lang),
                "recents": recents[:6], "works_total": len(mine),
                "usage": simple.usage_gauge(slug), "nina": bool(nina),
                "limits": documents.limits() if documents.enabled() else None,
                "prefs": uiprefs.get(email)}

    @r.get("/api/simple/works")
    def works(request: Request, _f=Depends(_feature)) -> dict:
        slug = projectgate.requested_project(request)
        pu = _in_project(request, slug)
        mine = _mine(slug, (pu.get("email") or "").lower())[:100]
        w = simple.works_by_sid([s["session_id"] for s in mine])
        rows = [_row(s, w.get(s["session_id"]), with_deliverables=True) for s in mine]
        rows.sort(key=lambda x: x["updated_at"] or 0, reverse=True)
        return {"works": rows}

    # ---- start a work ----------------------------------------------------------------------
    @r.post("/api/simple/start")
    async def start(body: StartIn, request: Request, _f=Depends(_feature)) -> dict:
        slug = projectgate.requested_project(request)
        pu = _in_project(request, slug, "dev")
        email = (pu.get("email") or "").lower()
        lang = body.lang if body.lang in ("fr", "en") else "fr"
        pb = None
        if body.playbook:
            pb = playbooks.get(body.playbook)
            if not pb or pb.get("audience") != "simple":
                raise HTTPException(400, "unknown task template")
        ask = (body.ask or "").strip()
        label = pb["i18n"][lang]["label"] if pb else ""
        if not ask and not pb:
            raise HTTPException(400, {"code": "empty", "message": "say what you want to obtain"})
        docs = _docs(body.documents, slug, pu)
        if not ask:
            if not docs:
                raise HTTPException(400, {"code": "empty", "message":
                                          "say what you want to obtain, or attach a document"})
            ask = label
        sid = agentchat.new_sid()
        title = simple.title_for(ask if ask != label else "", label)
        s = board.add_sdk_session(sid, "docs", title=title, prompt=ask, project=slug, owner=email)
        simple.add_work(sid, slug, email, ask, body.playbook, lang)
        for d in docs:
            documents.attach(sid, d["id"], by=email)
        session = agentchat.get_or_create(sid, user=email)
        bstate, bmsg = budgets.check(slug)
        if bstate != "ok":
            session._emit({"type": "error", "message": bmsg})
        mission = ""
        if pb:
            mission = (playbooks.render(body.playbook, ask, project=slug) or ("", ""))[0]
        recall = preseed(f"{title} {ask}".strip(), session_id=sid)
        session._emit({"type": "user_echo", "text": ask,
                       "documents": [{"id": d["id"], "name": d["name"]} for d in docs]})
        bg(session.handle_user(simple.brief(ask=ask, lang=lang, project=slug, sid=sid,
                                            mission=mission, docs=docs, recall=recall)))
        audit.log(email, "session.spawn", sid,
                  f"{s['title']} (simple{', ' + body.playbook if body.playbook else ''})"
                  f" · {len(docs)} document(s)", project=slug)
        return {"session_id": sid, "title": s["title"]}

    # ---- one work --------------------------------------------------------------------------
    @r.get("/api/simple/sessions/{sid}")
    def work_get(sid: str, request: Request, _f=Depends(_feature)) -> dict:
        pu, project = _session(request, sid)
        s = next((x for x in board.list_sessions() if x["session_id"] == sid), None) or {}
        owner = (s.get("owner") or "")
        me = (pu.get("email") or "").lower()
        return {"sid": sid, "title": s.get("title") or "", "project": project, "state": _state(sid),
                "can_drive": iam.rank(pu["role"]) >= iam.rank("dev")
                and (not owner or owner == me or iam.rank(pu["role"]) >= iam.rank("admin")),
                "documents": [documents.public(d) for d in documents.of_session(sid)
                              if documents.visible(d, pu.get("clearance"))],
                "deliverables": simple.list_deliverables(project, sid)}

    @r.post("/api/simple/sessions/{sid}/message")
    async def work_message(sid: str, body: MessageIn, request: Request, _f=Depends(_feature)) -> dict:
        pu, project = _session(request, sid, drive=True)
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(400, {"code": "empty", "message": "empty message"})
        email = (pu.get("email") or "").lower()
        already = {d["id"] for d in documents.of_session(sid)}
        docs = [d for d in _docs(body.documents, project, pu) if d["id"] not in already]
        for d in docs:
            documents.attach(sid, d["id"], by=email)
        session = agentchat.get_or_create(sid, user=email)
        if session._busy:
            raise HTTPException(409, {"code": "busy", "message": "the agent is still working"})
        session._emit({"type": "user_echo", "text": text,
                       "documents": [{"id": d["id"], "name": d["name"]} for d in docs]})
        bg(session.handle_user(simple.follow_up(text, docs)))
        audit.log(email, "session.send", sid, f"{len(text)} chars · {len(docs)} document(s)",
                  project=project)
        return {"ok": True}

    # ---- deliverables ----------------------------------------------------------------------
    @r.get("/api/simple/sessions/{sid}/deliverables")
    def deliv_list(sid: str, request: Request, _f=Depends(_feature)) -> dict:
        _pu, project = _session(request, sid)
        return {"deliverables": simple.list_deliverables(project, sid)}

    @r.post("/api/simple/sessions/{sid}/deliverables")
    def deliv_save(sid: str, body: SaveIn, request: Request, _f=Depends(_feature)) -> dict:
        pu, project = _session(request, sid, drive=True)
        if not (body.text or "").strip():
            raise HTTPException(400, {"code": "empty", "message": "nothing to save"})
        try:
            name = simple.save_text(project, sid, body.title, body.text)
        except ValueError as e:
            raise HTTPException(413, str(e))
        audit.log(pu.get("email") or "", "deliverable.save", sid, name, project=project)
        return {"name": name, "deliverables": simple.list_deliverables(project, sid)}

    def _one(project: str, sid: str, name: str) -> dict:
        d = next((x for x in simple.list_deliverables(project, sid) if x["name"] == name), None)
        if d is None:
            raise HTTPException(404, "not found")
        return d

    @r.get("/api/simple/sessions/{sid}/deliverables/{name}")
    def deliv_get(sid: str, name: str, request: Request, _f=Depends(_feature)) -> dict:
        _pu, project = _session(request, sid)
        d = _one(project, sid, name)
        text = ""
        if d["previewable"]:
            try:
                text = simple.read_deliverable(project, sid, name)
            except (FileNotFoundError, ValueError):
                raise HTTPException(404, "not found")
        return {**d, "text": text}

    @r.get("/api/simple/sessions/{sid}/deliverables/{name}/download")
    def deliv_download(sid: str, name: str, request: Request, format: str = "md",  # noqa: A002
                       _f=Depends(_feature)):
        pu, project = _session(request, sid)
        d = _one(project, sid, name)
        try:
            p = simple.deliverable_path(project, sid, name)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "not found")
        stem = name.rsplit(".", 1)[0]
        if format == "docx":
            if d["ext"] not in ("md", "markdown", "txt"):
                raise HTTPException(400, "Word export is for text deliverables")
            data = simple.markdown_to_docx(simple.read_deliverable(project, sid, name))
            out_name, mime = f"{stem}.docx", documents.MIME["docx"]
        else:
            data = p.read_bytes()
            out_name = name
            mime = "text/markdown; charset=utf-8" if d["ext"] in ("md", "markdown") else \
                "application/octet-stream"
        audit.log(pu.get("email") or "", "deliverable.download", sid, out_name, project=project)
        ascii_name = out_name.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
        return Response(data, media_type=mime, headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; "
                                   f"filename*=UTF-8''{quote(out_name)}",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @r.post("/api/simple/sessions/{sid}/deliverables/{name}/validate")
    def deliv_validate(sid: str, name: str, request: Request, _f=Depends(_feature)) -> dict:
        pu, project = _session(request, sid, drive=True)
        try:
            v = simple.validate(project, sid, name, pu.get("email") or "")
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "not found")
        audit.log(pu.get("email") or "", "deliverable.validate", sid,
                  f"{name} · sha256 {v['sha256'][:16]}", project=project)
        return {"ok": True, "deliverables": simple.list_deliverables(project, sid)}

    return r
