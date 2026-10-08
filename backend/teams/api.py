"""teams.api — routes of the `teams` feature.

* ``POST /api/teams/messages``    the bot's messaging endpoint (no cookie: the Bot Framework
                                  JWT is verified first, then the tenant, then the person)
* ``GET  /api/admin/teams``       configuration state, channel mapping, Graph permissions
* ``PUT|DELETE /api/admin/teams/channels``   map a channel / chat to a project (+ its level)
* ``GET  /api/admin/teams/manifest``          the Teams app manifest to upload (admin)
* ``GET  /api/admin/teams/package``           the zip to upload (manifest + icons)
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import audit
import auth
import iam
import teams
from teams import botauth, bot, manifest, proactive, store

router = APIRouter()

GRAPH_PERMISSIONS = [
    {"permission": "Calendars.Read", "type": "Application",
     "why": "the brief reads the calendar of the person it is for (limit the mailboxes with an "
            "application access policy / RBAC for Applications)", "optional": True},
    {"permission": "Presence.Read.All", "type": "Application",
     "why": "presence in the brief (optional)", "optional": True},
]
BOT_PERMISSIONS = ["Bot Framework: messaging endpoint (no Graph permission needed to answer)"]


def _on() -> None:
    if not teams.enabled():
        raise HTTPException(404, "feature disabled on this instance")


def _admin(user: dict = Depends(auth.current_user)) -> dict:
    if iam.rank(user["role"]) < iam.rank("admin"):
        raise HTTPException(403, "role 'admin' required")
    return user


@router.post("/api/teams/messages")
async def messages(request: Request):
    _on()
    try:
        activity = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(activity, dict):
        raise HTTPException(400, "invalid activity")
    try:
        claims = botauth.verify(request.headers.get("authorization"), activity)
    except botauth.Rejected as e:
        # not the journal: an unauthenticated caller must not be able to fill it
        print(f"[teams] rejected request: {str(e)[:200]}")
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    try:
        store.set_meta("last_inbound", {
            "at": time.time(), "claims": sorted(claims), "type": activity.get("type"),
            "name": activity.get("name") or "", "service_url": activity.get("serviceUrl")})
        proactive.remember(activity)        # where to post this channel's approvals later
    except Exception as e:  # noqa: BLE001 — bookkeeping never blocks an answer
        print(f"[teams] could not record the conversation: {e!r}")
    import asyncio
    try:
        out = await asyncio.to_thread(bot.handle, activity)
    except Exception as e:  # noqa: BLE001 — never a stack trace to Microsoft
        print(f"[teams] activity failed: {e!r}")
        out = None
        if activity.get("type") == "invoke":
            out = {"statusCode": 200, "type": "application/vnd.microsoft.activity.message",
                   "value": "Nina could not do that — try again."}
    if out is None:
        return Response(status_code=200)
    return JSONResponse(out)


class ChannelIn(BaseModel):
    channel_id: str
    project: str
    level: str = "project"
    name: str = ""
    approvals: bool = True     # post the project's pending approvals in this channel


@router.get("/api/admin/teams")
def admin_state(_u: dict = Depends(_admin)) -> dict:
    return {"enabled": teams.enabled(), "missing": teams.configured(),
            "tenant": teams.tenant_id(), "app_id": teams.app_id(),
            "endpoint": f"{teams.public_url()}/api/teams/messages",
            "channels": store.channels(), "graph_permissions": GRAPH_PERMISSIONS,
            "bot": BOT_PERMISSIONS,
            # first live check of a real tenant: which claim names Microsoft sent (no values)
            "last_inbound": store.get_meta("last_inbound"),
            "proactive": proactive.state()}


@router.put("/api/admin/teams/channels")
def admin_map(body: ChannelIn, u: dict = Depends(_admin)) -> dict:
    import classification
    import projects
    if projects.get(body.project) is None:
        raise HTTPException(400, f"unknown project: {body.project}")
    if not body.channel_id.strip() or len(body.channel_id) > 400:
        raise HTTPException(400, "channel_id required")
    try:
        lvl = classification._parse_strict(body.level)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    store.map_channel(body.channel_id.strip(), body.project, lvl, body.name, u["email"],
                      body.approvals)
    audit.log(u["email"], "teams.channel.map", body.channel_id[:120], f"{body.project} @ {body.level}")
    return {"channels": store.channels()}


@router.delete("/api/admin/teams/channels")
def admin_unmap(channel_id: str, u: dict = Depends(_admin)) -> dict:
    store.unmap_channel(channel_id)
    audit.log(u["email"], "teams.channel.unmap", channel_id[:120], "")
    return {"channels": store.channels()}


@router.get("/api/admin/teams/manifest")
def admin_manifest(sso: bool = False, _u: dict = Depends(_admin)) -> dict:
    """Teams app manifest (v1.17) for this instance (docs/enterprise/TEAMS.md § 3).
    ``?sso=1`` adds webApplicationInfo (only if the Entra app exposes ``api://<host>/<id>``)."""
    try:
        return manifest.build(teams.app_id(), teams.public_url(), sso=sso)
    except manifest.ManifestError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/api/admin/teams/package")
def admin_package(sso: bool = False, _u: dict = Depends(_admin)) -> Response:
    """The app package to upload in the Teams admin center: manifest.json + the two icons."""
    try:
        m = manifest.build(teams.app_id(), teams.public_url(), sso=sso)
    except manifest.ManifestError as e:
        raise HTTPException(409, str(e)) from e
    return Response(manifest.package(m), media_type="application/zip",
                    headers={"content-disposition": 'attachment; filename="sokkan-teams-app.zip"'})
