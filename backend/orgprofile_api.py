#!/usr/bin/env python3
"""orgprofile_api.py — HTTP routes of the organisation's profile (backend/orgprofile.py).

    GET    /api/org/profile            the profile (an administrator sees the IBAN, others its end)
    PUT    /api/org/profile            change it (administrator; journaled, values never logged)
    GET    /api/org/profile/status     what a work knows / does not know yet (the onboarding step)
    GET    /api/org/profile/preview    the block a work receives (?ask=…&playbook=… ; administrator)
    GET    /api/org/logo               the logo (any member: documents and screens show it)
    PUT    /api/org/logo?name=x.png    the logo as the request body (administrator, ≤ 512 KB)
    DELETE /api/org/logo

The onboarding lot (« organisation » step of the first launch) calls these routes; its screen
is its own, the model and the checks are here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

import audit
import auth
import iam
import orgprofile


def router(require) -> APIRouter:
    r = APIRouter()

    def _admin(u: dict) -> bool:
        return iam.rank(u.get("role") or "") >= iam.rank("admin")

    @r.get("/api/org/profile")
    def profile(u: dict = Depends(auth.current_user)) -> dict:
        p = orgprofile.get(reveal=_admin(u))
        return {**p, "can_edit": _admin(u), "completeness": orgprofile.completeness()}

    @r.put("/api/org/profile")
    def profile_set(body: dict, u: dict = Depends(require("admin"))) -> dict:
        try:
            p = orgprofile.update(body or {}, u["email"])
        except orgprofile.ProfileError as e:
            raise HTTPException(400, {"code": f"{e.field}_{e.code}", "field": e.field,
                                      "message": str(e)})
        # which fields changed, never their values (an IBAN does not belong in a journal)
        fields = sorted(k for k in (body or {}) if k in orgprofile.TEXT_FIELDS
                        or k in ("name", "people", "signatories"))
        audit.log(u["email"], "org.profile.update", "organisation", ", ".join(fields)[:300])
        return {**p, "can_edit": True, "completeness": orgprofile.completeness()}

    @r.get("/api/org/profile/status")
    def status(_u: dict = Depends(auth.current_user)) -> dict:
        return orgprofile.completeness()

    @r.get("/api/org/profile/preview")
    def preview(ask: str = "", playbook: str = "", u: dict = Depends(require("admin"))) -> dict:
        txt = orgprofile.block(email=u["email"], display_name=iam.display_name(u["email"]),
                               ask=ask, playbook=playbook)
        return {"text": txt, "chars": len(txt), "tokens_est": (len(txt) + 3) // 4,
                "bank": orgprofile.needs_bank(ask, playbook)}

    @r.get("/api/org/logo")
    def logo(_u: dict = Depends(auth.current_user)):
        p = orgprofile.logo_path()
        if p is None:
            raise HTTPException(404, "no logo")
        ext = p.suffix.lstrip(".")
        return Response(p.read_bytes(), media_type=orgprofile.LOGO_EXT[ext], headers={
            "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff",
            # an SVG is an image here, never a document that runs a script
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    @r.put("/api/org/logo")
    async def logo_set(request: Request, name: str = "logo.png",
                       u: dict = Depends(require("admin"))) -> dict:
        data = await request.body()
        try:
            p = orgprofile.save_logo(data, name)
        except orgprofile.ProfileError as e:
            raise HTTPException(400, {"code": f"logo_{e.code}", "message": str(e)})
        audit.log(u["email"], "org.logo.update", "organisation", f"{p.suffix} · {len(data)} B")
        return {"ok": True}

    @r.delete("/api/org/logo")
    def logo_drop(u: dict = Depends(require("admin"))) -> dict:
        orgprofile.drop_logo()
        audit.log(u["email"], "org.logo.delete", "organisation", "")
        return {"ok": True}

    return r
