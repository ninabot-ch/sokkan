#!/usr/bin/env python3
"""claudetier_api.py — routes of « Write with Claude » (feature `claude_tier`, backend/claudetier.py).

    GET  /api/internal/claude-grant?project=&model=   the router, and only it (shared secret,
                                                      never through the reverse proxy)
    GET  /api/claude/gate?model=&lang=                may the current project write with Claude
    GET  /api/claude/settings                         admin: key (masked), models, default
    PUT  /api/claude/settings  {default_model}        admin: the model offered first
    PUT  /api/claude/key       {key}                  admin: the organisation's Anthropic key
    DELETE /api/claude/key                            admin
    PUT  /api/projects/{slug}/level {level, reason}   project admin: the project's classification

The key entered here is used by the Claude tier ONLY: unlike Setup › Model keys on a cloud
instance, it never becomes the engine of every session (on an appliance that would send
every request of the organisation to Anthropic).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import audit
import auth
import claudetier
import iam
import modelkeys
import projectgate

router = APIRouter()


class SettingsIn(BaseModel):
    default_model: str


class KeyIn(BaseModel):
    key: str


class LevelIn(BaseModel):
    level: str | int
    reason: str = ""


def _feature() -> None:
    if not claudetier.enabled():
        raise HTTPException(404, "feature disabled on this instance")


def _admin(request: Request) -> dict:
    _feature()
    u = auth.instance_user(request)
    if iam.rank(u.get("role") or "") < iam.rank("admin"):
        raise HTTPException(403, "role 'admin' required")
    return u


@router.get(claudetier.GRANT_PATH)
def grant(request: Request, project: str = "", model: str = ""):
    # the router talks to the api on the compose network; a request that came through the
    # reverse proxy (TLS / web) carries a forwarding header and is not the router
    fwd = any(request.headers.get(h) for h in ("x-forwarded-for", "x-forwarded-host", "x-real-ip",
                                                "forwarded"))
    status, body = claudetier.grant(project, model, request.headers.get("x-sokkan-router-secret") or "",
                                    fwd)
    return JSONResponse(body, status_code=status, headers={"cache-control": "no-store"})


@router.get("/api/claude/gate")
def gate(request: Request, model: str = "", lang: str = "fr") -> dict:
    _feature()
    slug = projectgate.requested_project(request)
    if projectgate.project_user(auth.instance_user(request), slug) is None:
        raise HTTPException(404, "not found")
    return claudetier.gate(slug, model, "fr" if lang == "fr" else "en")


def _settings() -> dict:
    rec = modelkeys.record("instance", "anthropic")
    return {"key": modelkeys.public("instance", "anthropic", rec) if rec else None,
            "models": [{"id": m, "label": claudetier.label(m),
                        "available": m in claudetier.available_models()} for m in claudetier.models()],
            "default_model": claudetier.default_model(),
            "max_level": claudetier.max_level(),
            "appliance": claudetier.appliance(), "router": bool(claudetier.router_secret()),
            "destination": claudetier.DESTINATION}


@router.get("/api/claude/settings")
def settings(request: Request) -> dict:
    _admin(request)
    return _settings()


@router.put("/api/claude/settings")
def settings_set(body: SettingsIn, request: Request) -> dict:
    u = _admin(request)
    try:
        m = claudetier.set_default_model(body.default_model.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "claude.default_model", "instance:anthropic", m)
    return _settings()


@router.put("/api/claude/key")
def key_set(body: KeyIn, request: Request) -> dict:
    u = _admin(request)
    prev = (modelkeys.record("instance", "anthropic") or {}).get("default_model")
    try:
        rec = modelkeys.set_key("instance", "anthropic", body.key, u["email"])
    except modelkeys.KeyError_ as e:
        raise HTTPException(400, str(e))
    if prev:
        modelkeys._update("instance", "anthropic", default_model=prev)
    audit.log(u["email"], "byok.set", "instance:anthropic", f"key {rec['masked']} (claude tier)")
    test = modelkeys.test_key("instance", "anthropic")
    audit.log(u["email"], "byok.test", "instance:anthropic", test["detail"])
    return {**_settings(), "test": test}


@router.delete("/api/claude/key")
def key_delete(request: Request) -> dict:
    u = _admin(request)
    gone = modelkeys.delete_key("instance", "anthropic")
    if gone:
        audit.log(u["email"], "byok.delete", "instance:anthropic", "claude tier")
    return _settings()


@router.put("/api/projects/{slug}/level")
def project_level(slug: str, body: LevelIn, request: Request) -> dict:
    """The project's classification. Lowering it takes a reason (it can open the project to
    Claude); both directions are journaled."""
    _feature()
    from core import levels
    import projects
    u = auth.instance_user(request)
    pu = projectgate.project_user(u, slug)
    if pu is None:
        raise HTTPException(404, "not found")
    if iam.rank(pu["role"]) < iam.rank("admin"):
        raise HTTPException(403, "role 'admin' required in this project")
    new = levels.parse(body.level)
    if new is None:
        raise HTTPException(400, "level: public | team | project | confidential | restricted")
    cur = claudetier.project_level(slug)
    reason = (body.reason or "").strip()
    if new < cur and len(reason) < 3:
        raise HTTPException(400, {"code": "reason", "message": "lowering a classification takes a reason"})
    p = projects.set_level(slug, new)
    audit.log(u["email"], "project.level", slug,
              f"{levels.ident(cur)} → {levels.ident(new)}" + (f" — {reason[:300]}" if reason else ""),
              project=slug)
    return {"project": slug, "level": levels.ident(p["level"]), "label": levels.label(p["level"]),
            "claude": claudetier.gate(slug)}
