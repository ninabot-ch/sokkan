#!/usr/bin/env python3
"""helm_api.py — the HTTP routes of Helm (3.3), installed by app.py.

`/api/helm/*` is NOT a project-prefixed route for projectgate: the Helm view spans the
projects a person steers, so every route checks it here — `helm.can_steer(user, project)`
for the management view (project maintainer/admin, or an instance admin who belongs to
the project), project membership (dev+) for creating a project from Nina's proposal and
for one's own brief. A project the person cannot steer answers 404, never a hint.
Every route 404s when the `helm` feature is off.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

import agent_templates
import audit
import board
import features
import helm
from calendars import ics as helm_calendar
import projectgate
import projects


class ProposalChild(BaseModel):
    title: str
    description: str = ""
    assignee: str = ""
    tag: str = ""
    priority: int = 2
    due: str = ""


class ProjectProposal(BaseModel):
    title: str
    intent: str = ""
    scope: str = ""
    constraints: str = ""
    deadline: str = ""
    team: list[str] = []
    decisions: list[str] = []
    children: list[ProposalChild] = []
    tag: str = "research"
    project: str = ""


class CalendarBody(BaseModel):
    kind: str = "ics"
    secret: str


def install(app, current_user, require) -> None:
    def feature_helm() -> None:
        if not features.enabled("helm"):
            raise HTTPException(404, "feature disabled on this instance")

    def _steer_card(user: dict, card_id: int) -> dict:
        c = board.get_card(card_id)
        if c is None or not helm.can_steer(user, c.get("project") or "default"):
            raise HTTPException(404, "card not found")
        return c

    def _view_card(user: dict, card_id: int) -> dict:
        """Read routes: can_view (= can_steer, plus project members on the Captains demo)."""
        c = board.get_card(card_id)
        if c is None or not helm.can_view(user, c.get("project") or "default"):
            raise HTTPException(404, "card not found")
        return c

    @app.get("/api/helm/deck")
    def helm_deck(project: str = "", team: str = "", person: str = "",
                  u: dict = Depends(current_user), _f: None = Depends(feature_helm)) -> dict:
        if project and not helm.can_view(u, project):
            raise HTTPException(404, f"no project '{project}' for you")
        return helm.deck(u, project=project, team=team, person=person.strip().lower())

    @app.get("/api/helm/filters")
    def helm_filters(u: dict = Depends(current_user), _f: None = Depends(feature_helm)) -> dict:
        return helm.filters(u)

    @app.get("/api/helm/access")
    def helm_access(u: dict = Depends(current_user)) -> dict:
        """UI hint: does this person get the Helm tab (they steer at least one project)."""
        on = features.enabled("helm")
        import demo_captains
        demo = on and demo_captains.enabled()
        steers = helm.steerable_projects(u) if on else []
        # 3.2.2 Captains demo: a member reads Helm without steering (read_only = no button)
        reads = helm.viewable_projects(u) if demo else steers
        return {"enabled": on, "steers": steers, "reads": reads,
                "read_only": bool(demo and reads and not steers)}

    @app.get("/api/helm/cards/{card_id}")
    def helm_card(card_id: int, u: dict = Depends(current_user),
                  _f: None = Depends(feature_helm)) -> dict:
        _view_card(u, card_id)
        return helm.detail(card_id)

    @app.get("/api/helm/cards/{card_id}/activity")
    def helm_activity(card_id: int, u: dict = Depends(current_user),
                      _f: None = Depends(feature_helm)) -> list[dict]:
        _view_card(u, card_id)
        return helm.activity(card_id)

    @app.get("/api/helm/cards/{card_id}/costs")
    def helm_costs(card_id: int, u: dict = Depends(current_user),
                   _f: None = Depends(feature_helm)) -> dict:
        _view_card(u, card_id)
        return helm.costs(card_id)

    @app.post("/api/helm/cards/{card_id}/suggestions/refresh")
    def helm_refresh(card_id: int, u: dict = Depends(current_user),
                     _f: None = Depends(feature_helm)) -> dict:
        """On demand: run the detectors of the card's project now."""
        c = _steer_card(u, card_id)
        res = helm.suggest(c["project"])
        helm.refresh(card_id)
        return {"new": len(res["new"]), "resolved": res["resolved"], "notes": res["notes"],
                "suggestions": helm.list_suggestions([c["project"]], card_id=card_id)}

    @app.post("/api/helm/cards/{card_id}/baseline")
    def helm_baseline(card_id: int, u: dict = Depends(current_user),
                      _f: None = Depends(feature_helm)) -> dict:
        card = _steer_card(u, card_id)
        c = helm.rebaseline(card_id, u["email"])
        audit.log(u["email"], "helm.scope.accept", f"card #{card_id}", "re-baselined",
                  project=card["project"])
        return c

    def _sugg(u: dict, sid: int) -> dict:
        s = helm.get_suggestion(sid)
        if s is None or not helm.can_steer(u, s["project"]):
            raise HTTPException(404, "suggestion not found")
        return s

    @app.post("/api/helm/suggestions/{sid}/approve")
    def helm_approve(sid: int, u: dict = Depends(current_user),
                     _f: None = Depends(feature_helm)) -> dict:
        s = _sugg(u, sid)
        try:
            out = helm.approve(sid, u["email"])
        except ValueError as e:
            raise HTTPException(409, str(e))
        audit.log(u["email"], "helm.suggestion.approve", f"suggestion #{sid}",
                  f"{s['kind']}: {s['title'][:120]} → card #{out['reframe_card']['id']}",
                  project=s["project"])
        return out

    @app.post("/api/helm/suggestions/{sid}/ignore")
    def helm_ignore(sid: int, u: dict = Depends(current_user),
                    _f: None = Depends(feature_helm)) -> dict:
        s = _sugg(u, sid)
        try:
            out = helm.ignore(sid, u["email"])
        except ValueError as e:
            raise HTTPException(409, str(e))
        audit.log(u["email"], "helm.suggestion.ignore", f"suggestion #{sid}",
                  f"{s['kind']}: {s['title'][:120]}", project=s["project"])
        return out

    @app.post("/api/helm/projects")
    def helm_create_project(body: ProjectProposal, request: Request,
                            u: dict = Depends(current_user),
                            _f: None = Depends(feature_helm)) -> dict:
        """Nina's proposal, edited and validated by the person: the project card and its
        children. Needs dev+ in the project (any developer creates cards on the board)."""
        slug = body.project or projectgate.requested_project(request)
        role = projects.effective_role(u, slug)
        if role is None:
            raise HTTPException(404, f"no project '{slug}' for you")
        if role not in ("dev", "maintainer", "admin"):
            raise HTTPException(403, "creating a project needs the developer role in it")
        try:
            out = helm.create_project(u["email"], slug, body.title, intent=body.intent,
                                      scope=body.scope, constraints=body.constraints,
                                      deadline=body.deadline, team=body.team,
                                      decisions=body.decisions,
                                      children=[c.model_dump() for c in body.children],
                                      tag=body.tag if body.tag in board.TAGS else "research")
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.log(u["email"], "helm.project.create", f"card #{out['card']['id']}",
                  f"{body.title[:120]} · {len(out['children'])} card(s) · from Nina", project=slug)
        return out

    @app.get("/api/helm/brief")
    def helm_brief(request: Request, project: str = "", person: str = "", team: str = "",
                   u: dict = Depends(current_user), _f: None = Depends(feature_helm)) -> dict:
        """Preview of a morning brief. One's own brief: any member of the project; someone
        else's or a team's: the project's managers."""
        slug = project or projectgate.requested_project(request)
        if projects.effective_role(u, slug) is None:
            raise HTTPException(404, f"no project '{slug}' for you")
        person = (person or "").strip().lower()
        if (team or (person and person != u["email"])) and not helm.can_steer(u, slug):
            raise HTTPException(403, "only the project's managers read someone else's brief")
        return helm.morning_brief(slug, person=person or ("" if team else u["email"]), team=team)

    @app.get("/api/helm/calendar")
    def helm_calendar_get(u: dict = Depends(current_user), _f: None = Depends(feature_helm)) -> dict:
        return {"config": helm_calendar.get_source_config(u["email"]), "kinds": list(helm_calendar.KINDS)}

    @app.put("/api/helm/calendar")
    def helm_calendar_put(body: CalendarBody, u: dict = Depends(require("dev")),
                          _f: None = Depends(feature_helm)) -> dict:
        import vault
        if body.secret not in vault.names():
            raise HTTPException(400, f"no vault secret named {body.secret} — an admin adds it in Setup › Secrets")
        try:
            out = helm_calendar.set_source(u["email"], body.kind, body.secret)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.log(u["email"], "helm.calendar.set", u["email"], f"{body.kind} · secret {body.secret}")
        return out

    @app.delete("/api/helm/calendar")
    def helm_calendar_delete(u: dict = Depends(current_user), _f: None = Depends(feature_helm)) -> dict:
        helm_calendar.clear_source(u["email"])
        return {"ok": True}

    # ---- Crew templates (agents ready to use) ----
    # (own prefix: /api/agents/{id} would swallow /api/agents/templates)
    @app.get("/api/agent-templates")
    def agent_templates_list(_u: dict = Depends(current_user)) -> list[dict]:
        return agent_templates.catalog(features.enabled)

    @app.get("/api/agent-templates/{tid}")
    def agent_template(tid: str, person: str = "", team: str = "",
                       _u: dict = Depends(current_user)) -> dict:
        t = agent_templates.TEMPLATES.get(tid)
        if t is None or (t.get("feature") and not features.enabled(t["feature"])):
            raise HTTPException(404, "unknown template")
        return {"id": tid, "fields": agent_templates.render(tid, person=person, team=team)}
