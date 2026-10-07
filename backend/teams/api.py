"""teams.api — routes of the `teams` feature.

* ``POST /api/teams/messages``    the bot's messaging endpoint (no cookie: the Bot Framework
                                  JWT is verified first, then the tenant, then the person)
* ``GET  /api/admin/teams``       configuration state, channel mapping, Graph permissions
* ``PUT|DELETE /api/admin/teams/channels``   map a channel / chat to a project (+ its level)
* ``GET  /api/admin/teams/manifest``          the Teams app manifest to upload (admin)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import audit
import auth
import iam
import teams
from teams import botauth, bot, store

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
        botauth.verify(request.headers.get("authorization"), activity)
    except botauth.Rejected as e:
        # not the journal: an unauthenticated caller must not be able to fill it
        print(f"[teams] rejected request: {str(e)[:200]}")
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
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


@router.get("/api/admin/teams")
def admin_state(_u: dict = Depends(_admin)) -> dict:
    return {"enabled": teams.enabled(), "missing": teams.configured(),
            "tenant": teams.tenant_id(), "app_id": teams.app_id(),
            "endpoint": f"{teams.public_url()}/api/teams/messages",
            "channels": store.channels(), "graph_permissions": GRAPH_PERMISSIONS,
            "bot": BOT_PERMISSIONS}


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
    store.map_channel(body.channel_id.strip(), body.project, lvl, body.name, u["email"])
    audit.log(u["email"], "teams.channel.map", body.channel_id[:120], f"{body.project} @ {body.level}")
    return {"channels": store.channels()}


@router.delete("/api/admin/teams/channels")
def admin_unmap(channel_id: str, u: dict = Depends(_admin)) -> dict:
    store.unmap_channel(channel_id)
    audit.log(u["email"], "teams.channel.unmap", channel_id[:120], "")
    return {"channels": store.channels()}


@router.get("/api/admin/teams/manifest")
def admin_manifest(_u: dict = Depends(_admin)) -> dict:
    """Teams app manifest (v1.17) for this instance: zip it with two icons and upload it in
    the Teams admin center (docs/enterprise/TEAMS.md)."""
    if not teams.app_id():
        raise HTTPException(409, "set SOKKAN_TEAMS_APP_ID first")
    host = (teams.public_url().split("://", 1)[-1] or "sokkan.example").split("/")[0]
    return {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
        "manifestVersion": "1.17", "version": "3.4.0", "id": teams.app_id(),
        "developer": {"name": "SOKKAN", "websiteUrl": f"https://{host}",
                      "privacyUrl": f"https://{host}", "termsOfUseUrl": f"https://{host}"},
        "name": {"short": "Nina (SOKKAN)", "full": "Nina — SOKKAN assistant"},
        "description": {"short": "Ask Nina about your SOKKAN projects.",
                        "full": "Project status, cards, agent approvals and decision capture, "
                                "answered as you, within your clearance."},
        "icons": {"color": "color.png", "outline": "outline.png"}, "accentColor": "#0E7C86",
        "bots": [{"botId": teams.app_id(), "scopes": ["personal", "team", "groupChat"],
                  "supportsFiles": False, "isNotificationOnly": False,
                  "commandLists": [{"scopes": ["team", "groupChat", "personal"], "commands": [
                      {"title": "status", "description": "Status of the linked project"},
                      {"title": "decision:", "description": "Note a decision in the project memory"},
                      {"title": "card:", "description": "Create a card"},
                      {"title": "run", "description": "Propose a run of an agent (approval)"},
                      {"title": "approvals", "description": "Pending approvals"}]}]}],
        "permissions": ["identity", "messageTeamMembers"], "validDomains": [host],
        "webApplicationInfo": {"id": teams.app_id(), "resource": f"api://{host}/{teams.app_id()}"},
    }
