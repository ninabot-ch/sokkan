"""connectors.api — /api/connectors/* : the instance settings (admin), each person's
connections (connect, consent, revoke) and the approval cards (approve / reject).

404 everywhere when the feature is off. A person only ever sees and decides their OWN cards
and connections; an instance administrator configures providers and may revoke anyone's
connection (journaled), never read it or decide in their place."""
from __future__ import annotations

import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

import audit
import auth
import iam

from . import ConnectorError, enabled, service, store

router = APIRouter(prefix="/api/connectors")


def _on() -> None:
    if not enabled():
        raise HTTPException(404, "feature disabled on this instance")


Dep = Depends(auth.current_user)
On = Depends(_on)


def _err(e: ConnectorError) -> HTTPException:
    return HTTPException(e.status, {"code": e.code, "message": e.message})


def _admin(u: dict) -> bool:
    return iam.rank(u.get("instance_role") or u.get("role") or "") >= iam.rank("admin")


def _ctx(u: dict) -> service.Ctx:
    return service.Ctx(u["email"], "", u.get("project") or "default")


def _redirect_uri(request: Request) -> str:
    base = (os.environ.get("SOKKAN_PUBLIC_URL") or str(request.base_url)).rstrip("/")
    return base + "/api/connectors/graph/callback"


@router.get("")
def overview(u: dict = Dep, _=On):
    try:
        st = service.status(_ctx(u))
    except ConnectorError as e:
        raise _err(e) from None
    out = {"me": st, "accounts": [{k: a[k] for k in ("provider", "login", "consented_at", "last_used_at")}
                                  for a in store.accounts_of(u["email"])],
           "is_admin": _admin(u)}
    if _admin(u):
        out["settings"] = service.public_settings()
    return out


class SettingsIn(BaseModel):
    providers: dict | None = None
    allow_send: bool | None = None
    scopes: dict | None = None
    graph_sites: dict | None = None
    graph_client_secret: str | None = None


@router.put("/settings")
def put_settings(body: SettingsIn, u: dict = Dep, _=On):
    if not _admin(u):
        raise HTTPException(403, "instance administrators only")
    try:
        service.save_settings(body.model_dump(exclude_none=True), u["email"])
    except ConnectorError as e:
        raise _err(e) from None
    return service.public_settings()


class LoginIn(BaseModel):
    login: str = ""
    password: str = ""
    url: str = ""


@router.post("/{provider}/connect")
def connect(provider: str, body: LoginIn, u: dict = Dep, _=On):
    try:
        return service.connect_login(u["email"], provider, body.login, body.password, body.url)
    except ConnectorError as e:
        raise _err(e) from None


@router.get("/graph/start")
def graph_start(request: Request, u: dict = Dep, _=On):
    from . import graph
    s = service.settings()
    if not s["providers"]["graph"].get("enabled"):
        raise HTTPException(409, {"code": "disabled", "message": "Microsoft 365 n'est pas activé."})
    try:
        return {"url": graph.authorize_url(s, u["email"], _redirect_uri(request))}
    except ConnectorError as e:
        raise _err(e) from None


@router.get("/graph/callback")
def graph_callback(request: Request, state: str = "", code: str = "", error: str = "",
                   u: dict = Dep, _=On):
    from . import graph
    if error or not code:
        return RedirectResponse("/?" + urlencode({"connectors": "refused"}), 302)
    try:
        who = graph.finish(service.settings(), state, code, _redirect_uri(request), u["email"])
    except ConnectorError as e:
        if e.code == "mismatch":
            audit.log(u["email"], "connectors.connect.mismatch", "graph", "")
        return RedirectResponse("/?" + urlencode({"connectors": "error", "code": e.code}), 302)
    audit.log(who, "connectors.connect", "graph", (store.account(who, "graph") or {}).get("login", ""))
    return RedirectResponse("/?" + urlencode({"connectors": "connected"}), 302)


@router.delete("/{provider}")
def revoke(provider: str, u: dict = Dep, _=On):
    return {"revoked": service.revoke(u["email"], provider)}


@router.delete("/accounts/{email}/{provider}")
def admin_revoke(email: str, provider: str, u: dict = Dep, _=On):
    if not _admin(u):
        raise HTTPException(403, "instance administrators only")
    return {"revoked": service.revoke(email, provider, by=u["email"])}


@router.get("/actions")
def actions(status: str = "", u: dict = Dep, _=On):
    return {"actions": store.actions_of(u["email"], status)}


class DecisionIn(BaseModel):
    reason: str = ""


@router.post("/actions/{aid}/approve")
def approve(aid: str, u: dict = Dep, _=On):
    try:
        return service.decide(aid, u["email"], True)
    except ConnectorError as e:
        raise _err(e) from None


@router.post("/actions/{aid}/reject")
def reject(aid: str, body: DecisionIn | None = None, u: dict = Dep, _=On):
    try:
        return service.decide(aid, u["email"], False, (body.reason if body else "")[:500])
    except ConnectorError as e:
        raise _err(e) from None
