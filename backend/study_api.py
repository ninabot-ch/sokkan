"""study_api.py — routes of the `study_mode` feature (backend/study.py, study_export.py).

For everyone signed in:
* ``GET  /api/study/me``                     the study here, my pseudonym, my consent, the
                                              ceilings (``enabled: false`` when the feature is off)
* ``POST /api/study/consent``                give my consent (``content_scenarios``: the
                                              scenarios whose content I agree to share)
* ``PUT  /api/study/consent/content``        change the scenarios whose content I share
* ``DELETE /api/study/consent``              withdraw
* ``GET  /api/study/sessions``               my study sessions (to tag them, to answer)
* ``GET|PUT /api/sessions/{sid}/study``      tag of a session: scenario, reference time
* ``POST /api/sessions/{sid}/study/feedback`` the two answers at the end of a task
* ``PUT  /api/board/card/{id}/study``        tag a card (its sessions inherit it)

Instance admins:
* ``GET  /api/admin/study``                  state, participants (the local correspondence
                                              table), consents, export tokens, past exports
* ``POST /api/admin/study/refresh``          ask the gateway again now
* ``POST|DELETE /api/admin/study/tokens``    create / revoke a read-only export token

Researcher (or an admin):
* ``GET /api/study/export``                  the package — ``Authorization: Bearer skx_…``
                                              (an export token opens this route and nothing
                                              else) or an admin session; ``?format=zip|json``,
                                              ``since`` / ``until``.

Everything answers 404 while the feature is off, except ``/api/study/me``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

import audit
import auth
import hitl_journal
import iam
import study
import study_export

router = APIRouter()
EXPORT_PATH = "/api/study/export"     # own authentication (token or admin): see app._AUTH_FREE


def _on() -> None:
    if not study.enabled():
        raise HTTPException(404, "feature disabled on this instance")


def _err(e: study.StudyError):
    raise HTTPException(e.status, e.detail) from e


def _admin(u: dict = Depends(auth.current_user)) -> dict:
    if iam.rank(u.get("instance_role") or u["role"]) < iam.rank("admin"):
        raise HTTPException(403, "role 'admin' required")
    return u


def _public_state(st: dict) -> dict:
    return {k: st[k] for k in ("enabled", "active", "study_id", "expires_at", "expired",
                               "days_left", "audit_required", "notice", "limits", "used",
                               "source", "stale")}


@router.get("/api/study/me")
def me(u: dict = Depends(auth.current_user)) -> dict:
    if not study.enabled():
        return {"enabled": False, "active": False}
    st = study.state()
    out = {**_public_state(st), "scenarios": list(study.SCENARIOS),
           "consent_version": study.CONSENT_VERSION, "pseudonym": "", "consent": None,
           "pending_feedback": 0, "blocked": False, "mine": None}
    if st["study_id"]:
        out["pseudonym"] = study.pseudonym_of(u["email"]) if st["active"] else \
            study.pseudonym_of(u["email"], create=False)
        out["consent"] = study.consent_of(u["email"], st["study_id"])
        out["pending_feedback"] = study.pending_feedback(u["email"])
        out["blocked"] = study.gate(u["email"]) is not None
        out["mine"] = study.my_usage(u["email"])
    return out


class ConsentIn(BaseModel):
    content_scenarios: list[str] = []


@router.post("/api/study/consent")
def consent_give(body: ConsentIn, u: dict = Depends(auth.current_user), _f=Depends(_on)) -> dict:
    try:
        c = study.give_consent(u["email"], body.content_scenarios)
    except study.StudyError as e:
        _err(e)
    # the journal keeps the fact under the pseudonym: the action journal is exported to no one,
    # but it must not become a second correspondence table
    audit.log("", "study.consent.give", c["study_id"],
              f"{study.pseudonym_of(u['email'])} v{c['version']}, content: "
              f"{','.join(c['content_scenarios']) or 'none'}", project="")
    return c


@router.put("/api/study/consent/content")
def consent_content(body: ConsentIn, u: dict = Depends(auth.current_user),
                    _f=Depends(_on)) -> dict:
    try:
        c = study.set_content_scenarios(u["email"], body.content_scenarios)
    except study.StudyError as e:
        _err(e)
    audit.log("", "study.consent.content", c["study_id"],
              f"{study.pseudonym_of(u['email'])}: {','.join(c['content_scenarios']) or 'none'}",
              project="")
    return c


@router.delete("/api/study/consent")
def consent_withdraw(u: dict = Depends(auth.current_user), _f=Depends(_on)) -> dict:
    c = study.withdraw(u["email"])
    audit.log("", "study.consent.withdraw", c["study_id"],
              study.pseudonym_of(u["email"], create=False), project="")
    return c


@router.get("/api/study/sessions")
def my_sessions(u: dict = Depends(auth.current_user), _f=Depends(_on)) -> dict:
    rows = study.my_sessions(u["email"])
    titles: dict[str, str] = {}
    try:
        import board
        titles = {s["session_id"]: s.get("title") or s.get("tag") or ""
                  for s in board.list_sessions()}
    except Exception:  # noqa: BLE001
        pass
    for r in rows:
        r["title"] = titles.get(r["session_id"], "")   # the person's own sessions, to them only
    return {"sessions": rows, "scenarios": list(study.SCENARIOS)}


def _session_owner(sid: str) -> str:
    import board
    if board.get_session_project(sid) is None:
        raise HTTPException(404, "session not found")
    return board.get_session_owner(sid)


def _may_tag(u: dict, owner: str) -> None:
    if owner and owner == (u["email"] or "").lower():
        return
    if iam.rank(u["role"]) >= iam.rank("admin"):     # admin / maintainer of the project
        return
    raise HTTPException(403, "only the session's owner or an admin of the project tags it")


class TagIn(BaseModel):
    scenario: str | None = None
    reference_minutes: float | None = None


@router.get("/api/sessions/{sid}/study")
def session_tag(sid: str, u: dict = Depends(auth.current_user), _f=Depends(_on)) -> dict:
    _session_owner(sid)
    t = study.tag_of(sid)
    if t is None:
        return {"tagged": False}
    mine = t["pseudonym"] == study.pseudonym_of(u["email"], create=False)
    return {"tagged": True, "study_id": t["study_id"], "participant": t["pseudonym"],
            "scenario": t["scenario"], "reference_minutes": t["reference_minutes"],
            "closed_at": t["closed_at"], "mine": mine}


@router.put("/api/sessions/{sid}/study")
def session_tag_set(sid: str, body: TagIn, u: dict = Depends(auth.current_user),
                    _f=Depends(_on)) -> dict:
    owner = _session_owner(sid)
    _may_tag(u, owner)
    if not owner:
        raise HTTPException(400, "this session has no owner to attribute the work to")
    if not study.consent_of(owner)["given"]:
        raise HTTPException(409, "the session's owner has not consented to the study")
    try:
        t = study.tag_session(sid, owner, scenario=body.scenario,
                              reference_minutes=body.reference_minutes)
    except study.StudyError as e:
        _err(e)
    audit.log("", "study.tag", f"session:{sid}",
              f"{t['pseudonym']} {t['scenario'] or '—'} ref {t['reference_minutes']}")
    return {"tagged": True, "study_id": t["study_id"], "participant": t["pseudonym"],
            "scenario": t["scenario"], "reference_minutes": t["reference_minutes"]}


class FeedbackIn(BaseModel):
    useful: int
    verify_minutes: float


@router.post("/api/sessions/{sid}/study/feedback")
def session_feedback(sid: str, body: FeedbackIn, u: dict = Depends(auth.current_user),
                     _f=Depends(_on)) -> dict:
    _session_owner(sid)
    try:
        f = study.give_feedback(sid, u["email"], body.useful, body.verify_minutes)
    except study.StudyError as e:
        _err(e)
    return {"useful": f["useful"], "verify_minutes": f["verify_minutes"],
            "at": f["created_at"]}


@router.put("/api/board/card/{card_id}/study")
def card_tag_set(card_id: int, body: TagIn, u: dict = Depends(auth.current_user),
                 _f=Depends(_on)) -> dict:
    import board
    if board.get_card(card_id) is None:
        raise HTTPException(404, "card not found")
    if iam.rank(u["role"]) < iam.rank("dev"):
        raise HTTPException(403, "role 'dev' required")
    try:
        t = study.tag_card(card_id, scenario=body.scenario,
                           reference_minutes=body.reference_minutes)
    except study.StudyError as e:
        _err(e)
    return {"study_id": t["study_id"], "scenario": t["scenario"],
            "reference_minutes": t["reference_minutes"]}


# ---- instance administration ------------------------------------------------------------
@router.get("/api/admin/study")
def admin_get(u: dict = Depends(_admin), _f=Depends(_on)) -> dict:
    st = study.state()
    cons = {c["pseudonym"]: c for c in study.consents(st["study_id"])} if st["study_id"] else {}
    people = []
    for r in study.correspondence():
        c = cons.get(r["pseudonym"])
        people.append({"pseudonym": r["pseudonym"], "email": r["email"],
                       "consent": "none" if c is None else
                       ("withdrawn" if c["withdrawn_at"] is not None else "given"),
                       "consent_at": None if c is None else c["given_at"],
                       "content_scenarios": [] if c is None or c["withdrawn_at"] is not None
                       else c["content_scenarios"]})
    audit.log(u["email"], "study.correspondence.read", st["study_id"], f"{len(people)} people",
              project="")
    return {**_public_state(st), "scenarios": list(study.SCENARIOS), "participants": people,
            "tokens": study.list_tokens(), "exports": study.exports(),
            "auth_mode": auth.MODE,
            "single_identity": study.single_identity()}


@router.post("/api/admin/study/refresh")
def admin_refresh(_u: dict = Depends(_admin), _f=Depends(_on)) -> dict:
    return _public_state(study.state(refresh=True))


class TokenIn(BaseModel):
    label: str = ""
    days: int | None = 30


@router.post("/api/admin/study/tokens")
def admin_token_create(body: TokenIn, u: dict = Depends(_admin), _f=Depends(_on)) -> dict:
    t = study.create_token(body.label, body.days)
    audit.log(u["email"], "study.token.create", f"token:{t['id']}", t["label"], project="")
    return t                                   # the only time the token itself is returned


@router.delete("/api/admin/study/tokens/{token_id}")
def admin_token_revoke(token_id: int, u: dict = Depends(_admin), _f=Depends(_on)) -> dict:
    if not study.revoke_token(token_id):
        raise HTTPException(404, "no such live token")
    audit.log(u["email"], "study.token.revoke", f"token:{token_id}", "", project="")
    return {"ok": True}


# ---- the researcher's package -----------------------------------------------------------
@router.get(EXPORT_PATH)
def export(request: Request, format: str = "zip", since: str = "", until: str = "",
           _f=Depends(_on)):
    """Own authentication: a read-only export token (limited to this route) or an admin."""
    authz = request.headers.get("authorization") or ""
    by = ""
    if authz.lower().startswith("bearer "):
        tok = study.check_token(authz[7:].strip())
        if tok is None:
            raise HTTPException(401, "invalid, expired or revoked export token")
        by = f"token:{tok['id']}"
    else:
        u = auth.instance_user(request)        # raises 401 / 403 itself
        if iam.rank(u["role"]) < iam.rank("admin"):
            raise HTTPException(403, "an export token or the role 'admin' is required")
        by = u["email"]
    try:
        t0, t1 = hitl_journal.parse_when(since), hitl_journal.parse_when(until)
    except ValueError:
        raise HTTPException(400, "since / until: an ISO date (2027-01-12) or epoch seconds")
    try:
        pkg = study_export.build(since=t0, until=t1, by=by)
    except study.StudyError as e:
        _err(e)
    except study_export.ExportLeak as e:
        audit.log(by if "@" in by else "", "study.export.refused", "", str(e), project="")
        raise HTTPException(500, str(e)) from e
    if format == "json":
        return pkg
    name = f"sokkan-study-{pkg['manifest']['study_id']}.zip"
    return Response(study_export.to_zip(pkg), media_type="application/zip",
                    headers={"content-disposition": f'attachment; filename="{name}"'})
