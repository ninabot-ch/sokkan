"""classification_api.py — SOKKAN 3.4 routes of the `classification` feature.

* ``GET  /api/classification``                 the scale (customer labels) + my clearance here
* ``POST /api/memory/note/{name}/level``       reclassify a note (lowering: cleared
                                                maintainer/admin + reason, journaled)
* ``POST /api/board/card/{id}/level``          same for a card
* ``GET  /api/classification/audit``           audited recall of the project (project admins;
                                                ``?format=csv`` exports)
* ``GET|PUT|DELETE /api/admin/classification``  instance admin: SSO group → level mapping and
                                                the level of each project role

Project-scoped routes go through projectgate (selected project, object project, clearance);
everything 404s while the feature is off, except the description (``enabled: false``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import audit
import auth
import board
import classification
import iam
import projectgate
import projects

router = APIRouter()


def _require(min_role: str):
    def dep(user: dict = Depends(auth.current_user)) -> dict:
        if iam.rank(user["role"]) < iam.rank(min_role):
            raise HTTPException(403, f"role {min_role!r} required (you are {user['role']!r})")
        return user
    return dep


def _on() -> None:
    if not classification.enabled():
        raise HTTPException(404, "feature disabled on this instance")


def _ctx() -> dict:
    pu = projectgate.current()
    if pu is None:
        raise HTTPException(400, "no project context")
    return pu


@router.get("/api/classification")
def describe(_u: dict = Depends(auth.current_user)) -> dict:
    out = classification.describe()
    pu = projectgate.current()
    if pu is not None and out["enabled"]:
        c = pu.get("clearance")
        from core import levels
        out["project"] = pu["project"]
        out["clearance"] = None if c is None else levels.ident(c)
        out["can_audit"] = pu.get("project_role") == "admin"
    return out


class LevelIn(BaseModel):
    level: str
    reason: str = ""


@router.post("/api/memory/note/{name}/level")
def note_level(name: str, body: LevelIn, _u: dict = Depends(_require("dev"))) -> dict:
    _on()
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid name")
    pu = _ctx()
    try:
        return classification.set_note_level(classification.ctx_user(), pu["project"], name,
                                             body.level, body.reason)
    except classification.Refused as e:
        raise HTTPException(e.status, e.detail) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/api/board/card/{card_id}/level")
def card_level(card_id: int, body: LevelIn, _u: dict = Depends(_require("dev"))) -> dict:
    _on()
    card = board.get_card(card_id)
    if card is None:
        raise HTTPException(404, "card not found")
    try:
        return classification.set_card_level(classification.ctx_user(), card, body.level,
                                             body.reason)
    except classification.Refused as e:
        raise HTTPException(e.status, e.detail) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/classification/audit")
def access_audit(actor: str = "", note: str = "", session: str = "", days: int = 30,
                 limit: int = 1000, format: str = "json",
                 u: dict = Depends(auth.current_user)):
    """Who obtained which note of the selected project, through which path. Project admins
    only (an instance admin adds themself to the project first — journaled)."""
    _on()
    pu = _ctx()
    if pu.get("project_role") != "admin":
        raise HTTPException(403, "the access log is for the project's admins")
    stats: dict = {}
    rows = classification.access_log(pu["project"], reader_clearance=pu.get("clearance"),
                                     actor=actor, note=note, session=session,
                                     since_days=days, limit=limit, stats=stats)
    hidden = int(stats.get("hidden") or 0)
    audit.log(u["email"], "classification.audit.read", pu["project"],
              f"{len(rows)} rows" + (" (csv)" if format == "csv" else ""))
    if format == "csv":
        return PlainTextResponse(classification.access_csv(rows), media_type="text/csv",
                                 headers={"content-disposition":
                                          f'attachment; filename="access-{pu["project"]}.csv"',
                                          "x-sokkan-hidden-rows": str(hidden)})
    from core import levels
    return {"project": pu["project"], "entries": rows, "hidden_above_clearance": hidden,
            "clearance": None if pu.get("clearance") is None else levels.ident(pu["clearance"])}


# ---- instance administration ----------------------------------------------------------
class GroupLevelIn(BaseModel):
    team: str                 # IdP group name, or sso:<g> | local:<t> | user:<email>
    level: str
    project: str = "*"


class RoleLevelIn(BaseModel):
    role: str
    level: str


@router.get("/api/admin/classification")
def admin_get(_u: dict = Depends(_require("admin"))) -> dict:
    return {**classification.describe(), "groups": classification.group_map(),
            "roles": classification.role_levels(), "teams": projects.list_teams(),
            "projects": [p["slug"] for p in projects.list_projects()]}


@router.put("/api/admin/classification/groups")
def admin_set_group(body: GroupLevelIn, u: dict = Depends(_require("admin"))) -> dict:
    try:
        rows = classification.set_group_level(body.team, body.level, body.project, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.log(u["email"], "classification.group.set", body.team,
              f"{body.level} in {body.project}")
    return {"groups": rows}


@router.delete("/api/admin/classification/groups")
def admin_del_group(team: str, project: str = "*", u: dict = Depends(_require("admin"))) -> dict:
    rows = classification.delete_group_level(team, project)
    audit.log(u["email"], "classification.group.delete", team, project)
    return {"groups": rows}


@router.put("/api/admin/classification/roles")
def admin_set_role(body: RoleLevelIn, u: dict = Depends(_require("admin"))) -> dict:
    try:
        roles = classification.set_role_level(body.role, body.level, u["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.log(u["email"], "classification.role.set", body.role, body.level)
    return {"roles": roles}
