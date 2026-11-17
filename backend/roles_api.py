#!/usr/bin/env python3
"""roles_api.py — HTTP routes of the job kits of SOKKAN Atelier (4.0, `backend/roles.py`).

    GET  /api/simple/roles?lang=            the catalog, the caller's jobs, what they may set up
    POST /api/simple/roles/{rid}/setup      {project: "" | slug, lang} — a project for the job:
                                            "" = a NEW project (instance admin, multi-project on),
                                            a slug = the job in that project (its admin); in both
                                            cases the starter notes are written into the project's
                                            memory (never over a note that exists), the project is
                                            classified at least at the job's level, and the job is
                                            remembered as the project's own (its templates first)
    GET  /api/simple/roles/people           instance admin: every account with its jobs
                                            (set with PUT /api/admin/prefs/{email} {"roles": […]})

The person's own jobs are a preference: ``PUT /api/me/prefs {"roles": […]}`` (simple_api).
Everything here answers 404 when SOKKAN Atelier (feature ``simple_profile``) is off.
"""
from __future__ import annotations

import re
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import audit
import auth
import features
import iam
import projectgate
import projects
import roles
import simple
import uiprefs


class SetupIn(BaseModel):
    project: str = ""
    lang: str = "fr"


def _slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "poste"


def router(*, on_project_created=None) -> APIRouter:
    """``on_project_created()`` = what the app does after a project is created (its memory
    indexer, app._sync_project_indexers)."""
    r = APIRouter()

    def _feature() -> None:
        if not features.enabled("simple_profile"):
            raise HTTPException(404, "feature disabled on this instance")

    def _admin(user: dict) -> bool:
        return iam.rank(user.get("role") or "") >= iam.rank("admin")

    @r.get("/api/simple/roles")
    def catalog(request: Request, lang: str = "fr", _f=Depends(_feature)) -> dict:
        user = auth.instance_user(request)
        lang = lang if lang in roles.LANGS else "fr"
        try:
            cat = roles.catalog(lang)
        except roles.CatalogError as e:
            raise HTTPException(500, f"job kits: {e}")
        prefs = uiprefs.get(user["email"])
        slug = projectgate.requested_project(request)
        pu = projectgate.project_user(user, slug)
        return {"roles": cat, "mine": prefs.get("roles"), "project": slug,
                "project_role": simple.project_role(slug),
                "can_create_project": _admin(user) and projects.multi_project(),
                "can_setup_here": bool(pu) and iam.rank(pu["role"]) >= iam.rank("admin")}

    @r.post("/api/simple/roles/{rid}/setup")
    def setup(rid: str, body: SetupIn, request: Request, _f=Depends(_feature)) -> dict:
        user = auth.instance_user(request)
        email = (user.get("email") or "").lower()
        job = roles.get(rid)
        if job is None:
            raise HTTPException(404, "unknown job")
        lang = body.lang if body.lang in roles.LANGS else "fr"
        name = job["i18n"][lang]["project"]
        created = False
        if not body.project:
            if not _admin(user):
                raise HTTPException(403, {"code": "admin", "message": (
                    "Seule la personne qui administre SOKKAN crée un projet." if lang == "fr"
                    else "Only the person who administers SOKKAN creates a project.")})
            if not projects.multi_project():
                raise HTTPException(409, {"code": "single_project", "message": (
                    "Cette instance n'a qu'un projet : installez le poste dans celui-ci." if lang == "fr"
                    else "This instance has one project: set the job up in it.")})
            base = _slug(name)
            slug, n = base, 2
            while projects.get(slug) is not None:
                slug, n = f"{base}-{n}", n + 1
            projects.create(slug, name, created_by=email,
                            description=job["i18n"][lang]["description"])
            projects.grant(slug, "user", email, "admin", created_by=email)
            audit.log(email, "project.create", slug, f"job {rid}")
            created = True
            if on_project_created:
                try:
                    on_project_created()
                except Exception:  # noqa: BLE001 — the indexer catches up on its own
                    pass
        else:
            slug = body.project
            if not projects.valid_slug(slug) or projects.get(slug) is None and slug != "default":
                raise HTTPException(404, "not found")
            pu = projectgate.project_user(user, slug)
            if pu is None:
                raise HTTPException(404, "not found")
            if iam.rank(pu["role"]) < iam.rank("admin"):
                raise HTTPException(403, {"code": "project_admin", "message": (
                    "Il faut administrer ce projet pour y installer un poste." if lang == "fr"
                    else "Setting a job up in this project takes its administrator.")})
        # the job's level is a floor: a confidential job never lands in a team project
        want = roles.level_rank(job["level"])
        cur = projects.get(slug) or {}
        level = cur.get("level") if cur.get("level") is not None else 2
        if slug != "default" and want > int(level):
            projects.set_level(slug, want)
            audit.log(email, "project.level", slug, f"{level} → {want} (job {rid})", project=slug)
            level = want
        import store_backend
        written = roles.seed_notes(rid, store_backend.memory_dir_for(slug), lang)
        simple.bind_project_role(slug, rid, by=email)
        mine = uiprefs.get(email).get("roles") or []
        if rid not in mine:
            uiprefs.set_prefs(email, roles=roles.valid(mine + [rid]))
        audit.log(email, "roles.setup", slug, f"{rid} · {len(written)} note(s)", project=slug)
        return {"project": slug, "name": (projects.get(slug) or {}).get("name") or name,
                "created": created, "notes": written, "level": roles.LEVELS[int(level)]
                if 0 <= int(level) < len(roles.LEVELS) else "project"}

    @r.get("/api/simple/roles/people")
    def people(request: Request, _f=Depends(_feature)) -> dict:
        user = auth.instance_user(request)
        if not _admin(user):
            raise HTTPException(403, "role 'admin' required")
        out: dict[str, dict] = {}
        for u in iam.list_users():
            e = (u.get("email") or "").lower().strip()
            if e:
                out[e] = {"email": e, "name": u.get("name") or ""}
        try:
            import localaccounts
            for a in localaccounts.accounts():
                e = (a.get("email") or "").lower().strip()
                if e and e not in out:
                    out[e] = {"email": e, "name": a.get("name") if a.get("name") != e else ""}
        except Exception:  # noqa: BLE001 — no local accounts on this instance
            pass
        for e, p in out.items():
            p["roles"] = uiprefs.get(e).get("roles")
        return {"people": sorted(out.values(), key=lambda x: (x["name"] or x["email"]).lower())}

    return r
