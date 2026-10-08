#!/usr/bin/env python3
"""uiroutes.py — HTTP routes of three 3.2 cockpit features, kept out of app.py:

* `shared_review` — /api/shares…        (sharing.py)
* `byok_admin`    — /api/admin/model-keys… (modelkeys.py)
* `connect_ai`    — /api/connect-ai…     (connectai.py)

Every route is gated server side by its feature (404 when off) and by role. None of these
paths is a project route for the middleware (projectgate): `current_user` gives the
INSTANCE identity, and each route decides with the role in the object's project.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import audit
import auth
import connectai
import features
import iam
import llm
import modelkeys
import notify
import projectgate
import projects
import sharing

router = APIRouter()


def _feature(fid: str):
    features.BY_ID[fid]

    def dep() -> None:
        if not features.enabled(fid):
            raise HTTPException(404, "feature disabled on this instance")
    return dep


def _admin(user: dict = Depends(auth.current_user)) -> dict:
    """Instance admin (these routes carry no project context: the instance role)."""
    if iam.rank(user.get("role") or "") < iam.rank("admin"):
        raise HTTPException(403, "role 'admin' required")
    return user


def _share_err(e: sharing.ShareError):
    raise HTTPException(e.status, e.detail)


# =========================================================================================
# shared_review
# =========================================================================================
_shares = _feature("shared_review")


class ShareIn(BaseModel):
    kind: str                      # session | preview
    target: str                    # session id | preview URL
    principal_kind: str = "user"   # user | team
    principal: str
    access: str = "read"           # read | write
    expires_in_s: int | None = None
    approver: bool = False         # « ask X to validate » (write share of a session)
    note: str = ""
    title: str = ""
    path: str = ""                 # preview: path shown
    env: str = ""                  # preview: dev-server env name


@router.post("/api/shares")
def share_create(body: ShareIn, u: dict = Depends(auth.current_user), _f=Depends(_shares)) -> dict:
    try:
        s = sharing.create(u, body.kind, body.target, body.principal_kind, body.principal,
                           body.access, body.expires_in_s, body.approver, body.note, body.title,
                           {"path": body.path, "env": body.env} if body.kind == "preview" else None)
    except sharing.ShareError as e:
        _share_err(e)
    audit.log(u["email"], "share.create", f"{s['kind']}:{s['target']}",
              f"#{s['id']} project={s['project']} to {s['principal_kind']}:{s['principal']} "
              f"{s['access']}{' approver' if s['approver'] else ''}"
              f"{' expires' if s['expires_at'] else ''}")
    if s["approver"]:
        _notify_approver(s, u["email"])
    return s


@router.get("/api/shares")
def share_list(kind: str, target: str, u: dict = Depends(auth.current_user),
               _f=Depends(_shares)) -> list[dict]:
    try:
        return sharing.list_for_object(u, kind, target)
    except sharing.ShareError as e:
        _share_err(e)


@router.get("/api/shares/inbox")
def share_inbox(u: dict = Depends(auth.current_user), _f=Depends(_shares)) -> list[dict]:
    return sharing.inbox(u)


@router.get("/api/shares/people")
def share_people(kind: str, target: str, u: dict = Depends(auth.current_user),
                 _f=Depends(_shares)) -> list[dict]:
    """Who the object can be shared with: people and teams of ITS project, with their role
    (`write_ok` = may get write). Only for those who may share it (dev+)."""
    slug = sharing.object_project(kind, target)
    if slug is None or not projects.can(u, slug, "dev"):
        raise HTTPException(404, "not found")
    me = (u.get("email") or "").lower()
    out: dict[tuple[str, str], dict] = {}
    p = projects.get(slug) or {}
    if p.get("access_source") == "instance":
        for x in iam.list_users():
            role = projects.INSTANCE_TO_PROJECT.get(x["role"] or "")
            if role and x["email"] != me:
                out[("user", x["email"])] = {"kind": "user", "id": x["email"], "role": role}
    # effective members: direct grants AND members through a team grant (e.g. Alice via
    # `sso:radio-devs`), each with their effective role (best of grants + teams)
    emails: set[str] = set()
    for g in projects.list_grants(slug):
        if g["principal_kind"] == "user":
            emails.add(g["principal"])
        elif g["principal_kind"] == "team":
            out[("team", g["principal"])] = {"kind": "team", "id": g["principal"],
                                             "role": sharing._team_role(g["principal"], slug)}
            emails.update(projects.team_members(g["principal"]))
    for e in emails - {me}:
        role = sharing.role_of(e, slug)
        if role:
            out[("user", e)] = {"kind": "user", "id": e, "role": role}
    rows = sorted(out.values(), key=lambda r: (r["kind"], r["id"]))
    for r in rows:
        r["write_ok"] = projects.prank(r["role"]) >= projects.prank("dev")
    return rows


@router.get("/api/shares/{share_id}")
def share_open(share_id: int, u: dict = Depends(auth.current_user), _f=Depends(_shares)) -> dict:
    try:
        v = sharing.recipient_share(u, share_id)
    except sharing.ShareError as e:
        _share_err(e)
    audit.log(u["email"], "share.view", f"{v['kind']}:{v['target']}", f"#{share_id}")
    return v


@router.delete("/api/shares/{share_id}")
def share_revoke(share_id: int, u: dict = Depends(auth.current_user), _f=Depends(_shares)) -> dict:
    try:
        s = sharing.revoke(u, share_id)
    except sharing.ShareError as e:
        _share_err(e)
    audit.log(u["email"], "share.revoke", f"{s['kind']}:{s['target']}",
              f"#{share_id} was {s['principal_kind']}:{s['principal']} {s['access']}")
    return s


class ApproverIn(BaseModel):
    approver: bool


@router.post("/api/shares/{share_id}/approver")
def share_approver(share_id: int, body: ApproverIn, u: dict = Depends(auth.current_user),
                   _f=Depends(_shares)) -> dict:
    try:
        s = sharing.set_approver(u, share_id, body.approver)
    except sharing.ShareError as e:
        _share_err(e)
    audit.log(u["email"], "share.approver", f"{s['kind']}:{s['target']}",
              f"#{share_id} {s['principal_kind']}:{s['principal']} "
              f"{'asked to validate' if s['approver'] else 'no longer validates'}")
    if s["approver"]:
        _notify_approver(s, u["email"])
    return s


class DecideIn(BaseModel):
    permission_id: str
    decision: str                  # allow | deny
    message: str = ""


@router.post("/api/shares/{share_id}/decide")
def share_decide(share_id: int, body: DecideIn, u: dict = Depends(auth.current_user),
                 _f=Depends(_shares)) -> dict:
    if body.decision not in ("allow", "deny"):
        raise HTTPException(400, "decision: allow | deny")
    try:
        r = sharing.decide(u, share_id, body.permission_id, body.decision == "allow",
                           body.message)
    except sharing.ShareError as e:
        _share_err(e)
    audit.log(u["email"], f"share.hitl.{r['decision']}", f"session:{r['session_id']}",
              f"#{share_id} delegated approval: {r['tool']} — {r['title']}"[:500])
    return r


@router.get("/api/shares/{share_id}/shot")
def share_shot(share_id: int, w: int = 1440, h: int = 900, u: dict = Depends(auth.current_user),
               _f=Depends(_shares)):
    """Screenshot of a shared preview — for the recipient, whatever their role (a read share
    to a viewer), never another URL than the shared one."""
    if not features.enabled("preview"):
        raise HTTPException(404, "feature disabled on this instance")
    try:
        v = sharing.recipient_share(u, share_id)
    except sharing.ShareError as e:
        _share_err(e)
    if v["kind"] != "preview":
        raise HTTPException(400, "not a preview share")
    import preview
    try:
        path = preview.screenshot(v["target"], max(320, min(w, 2560)), max(240, min(h, 2000)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"screenshot failed: {e}")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


def _notify_approver(s: dict, by: str) -> None:
    try:
        notify.send("SOKKAN — validation requested",
                    f"{by} asks {s['principal']} to validate the session « {s['title'] or s['target']} »",
                    notify.session_link(s["target"]), "hitl")
    except Exception:  # noqa: BLE001 — a notification never fails the action
        pass


# =========================================================================================
# byok_admin
# =========================================================================================
_byok = _feature("byok_admin")


class ModelKeyIn(BaseModel):
    key: str
    scope: str = "instance"        # instance | project:<slug> (planned)
    base_url: str = ""
    use_for_sessions: bool = True  # Anthropic: sessions use this key (llm.json reference)
    test: bool = False


@router.get("/api/admin/model-keys")
def model_keys(_u: dict = Depends(_admin), _f=Depends(_byok)) -> dict:
    st = llm.status()
    return {"keys": modelkeys.list_keys(),
            "providers": [{"id": k, "label": v["label"], "hint": v["hint"],
                           "sessions": v["sessions"], "testable": bool(v["test_url"])}
                          for k, v in modelkeys.PROVIDERS.items()],
            "scopes": list(modelkeys.SCOPES_LIVE), "per_project": False,
            "gateway": modelkeys.gateway_public(),
            "sessions": {"mode": st.get("mode"), "key_ref": st.get("key_ref"),
                         "operator_managed": st.get("operator_managed")}}


@router.put("/api/admin/model-keys/{provider}")
def model_key_set(provider: str, body: ModelKeyIn, u: dict = Depends(_admin),
                  _f=Depends(_byok)) -> dict:
    try:
        rec = modelkeys.set_key(body.scope, provider, body.key, u["email"], body.base_url)
    except modelkeys.KeyError_ as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "byok.set", f"{rec['scope']}:{provider}", f"key {rec['masked']}")
    out: dict = {"key": rec}
    if body.test:
        out["test"] = modelkeys.test_key(rec["scope"], provider)
        audit.log(u["email"], "byok.test", f"{rec['scope']}:{provider}", out["test"]["detail"])
    if provider == "anthropic" and body.use_for_sessions:
        if llm.status().get("operator_managed"):
            out["sessions"] = "managed inference: sessions keep the operator's gateway"
        else:
            llm.save({"mode": "byok", "key_ref": modelkeys.ref(rec["scope"], provider),
                      "key_field": "anthropic_api_key"})
            out["sessions"] = "sessions use this key"
            audit.log(u["email"], "llm.config", "byok:api_key", "key from Model keys")
    out["gateway"] = modelkeys.push_gateway(rec["scope"], provider)
    if out["gateway"]["pushed"]:
        audit.log(u["email"], "byok.gateway.push", f"{rec['scope']}:{provider}",
                  modelkeys.gateway_public()["client"])
    out["key"] = modelkeys.public(rec["scope"], provider, modelkeys.record(rec["scope"], provider))
    return out


@router.delete("/api/admin/model-keys/{provider}")
def model_key_delete(provider: str, scope: str = "instance", u: dict = Depends(_admin),
                     _f=Depends(_byok)) -> dict:
    return _drop_key(scope, provider, u, "key deleted in Model keys")


def _drop_key(scope: str, provider: str, u: dict, why: str) -> dict:
    """Delete an instance model key — from Setup › Engines or the Model keys route alike:
    sessions lose it (llm.json reference cleared), the gateway forgets it (Anthropic)."""
    try:
        gone = modelkeys.delete_key(scope, provider)
    except modelkeys.KeyError_ as e:
        raise HTTPException(400, str(e))
    if not gone:
        raise HTTPException(404, "no key for that provider")
    audit.log(u["email"], "byok.delete", f"{scope}:{provider}")
    ref = modelkeys.ref(scope, provider)
    try:
        import json as _json
        raw = _json.loads(llm.CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if raw.get("key_ref") == ref:
        llm.save({})                       # sessions no longer have that key
        audit.log(u["email"], "llm.config", "none", why)
    gw = modelkeys.push_gateway(scope, provider, delete=True) if provider == "anthropic" else \
        {"pushed": False, "detail": "not pushed"}
    return {"ok": True, "gateway": gw}


@router.post("/api/admin/model-keys/{provider}/test")
def model_key_test(provider: str, scope: str = "instance", u: dict = Depends(_admin),
                   _f=Depends(_byok)) -> dict:
    try:
        res = modelkeys.test_key(scope, provider)
    except modelkeys.KeyError_ as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "byok.test", f"{scope}:{provider}", res["detail"])
    return res


# =========================================================================================
# connect_ai
# =========================================================================================
_connect = _feature("connect_ai")


def _ce(e: connectai.ConnectError):
    raise HTTPException(e.status, e.detail)


@router.get("/api/connect-ai")
def connect_view(request: Request, u: dict = Depends(auth.current_user),
                 _f=Depends(_connect)) -> dict:
    slug = projectgate.requested_project(request)
    prole = projects.effective_role(u, slug)
    return connectai.view(u, slug if prole else None,
                          iam.rank(u.get("role") or "") >= iam.rank("admin"), prole)


class EngineIn(BaseModel):
    auth: str = "key"              # key | login | none
    key: str = ""
    base_url: str = ""
    model: str = ""
    small_model: str = ""


@router.put("/api/connect-ai/engines/{eid}")
def connect_engine(eid: str, body: EngineIn, u: dict = Depends(_admin),
                   _f=Depends(_connect)) -> dict:
    try:
        connectai.connect(eid, u["email"], body.auth, body.key, body.base_url, body.model,
                          body.small_model)
    except connectai.ConnectError as e:
        _ce(e)
    except modelkeys.KeyError_ as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "connect_ai.connect", eid,
              f"auth={body.auth} model={body.model.strip()} base={body.base_url.strip()}"[:400])
    prov = connectai._provider_for(eid, body.auth)
    if body.key.strip() and prov == "anthropic":
        # one key, one behaviour: posed here or in Model keys, it reaches the gateway alike
        gw = modelkeys.push_gateway("instance", prov)
        if gw["pushed"]:
            audit.log(u["email"], "byok.gateway.push", f"instance:{prov}",
                      modelkeys.gateway_public()["client"])
    return connectai.view(u, None, True, None)


@router.delete("/api/connect-ai/engines/{eid}")
def disconnect_engine(eid: str, u: dict = Depends(_admin), _f=Depends(_connect)) -> dict:
    if eid not in connectai.BY_ID:
        raise HTTPException(404, "unknown engine")
    was_default = connectai._load().get("default") == eid
    connectai.disconnect(eid)
    if was_default:
        llm.save({})
    audit.log(u["email"], "connect_ai.disconnect", eid)
    return connectai.view(u, None, True, None)


def _engine_provider(eid: str, provider: str | None) -> str:
    if eid not in connectai.BY_ID:
        raise HTTPException(404, "unknown engine")
    provs = connectai.engine_providers(eid)
    if provider is None:
        have = [p for p in provs if modelkeys.record("instance", p)]
        provider = have[0] if have else provs[0]
    if provider not in provs:
        raise HTTPException(400, f"provider: one of {', '.join(provs)}")
    return provider


@router.post("/api/connect-ai/engines/{eid}/test")
def engine_key_test(eid: str, provider: str | None = None, u: dict = Depends(_admin),
                    _f=Depends(_connect)) -> dict:
    """3.2.2 Setup › Engines — test the instance key of this engine (same as Model keys)."""
    prov = _engine_provider(eid, provider)
    try:
        res = modelkeys.test_key("instance", prov)
    except modelkeys.KeyError_ as e:
        raise HTTPException(400, str(e))
    audit.log(u["email"], "byok.test", f"instance:{prov}", res["detail"])
    return res


@router.delete("/api/connect-ai/engines/{eid}/key")
def engine_key_delete(eid: str, provider: str | None = None, u: dict = Depends(_admin),
                      _f=Depends(_connect)) -> dict:
    """3.2.2 Setup › Engines — remove the instance key of this engine: the key is erased
    (the same record Model keys shows), the engines using it are disconnected."""
    prov = _engine_provider(eid, provider)
    ref = modelkeys.ref("instance", prov)
    users = [k for k, c in (connectai._load().get("connections") or {}).items()
             if c.get("key_ref") == ref]
    out = _drop_key("instance", prov, u, f"key removed in Setup › Engines ({eid})")
    for other in users:
        was_default = connectai._load().get("default") == other
        connectai.disconnect(other)
        if was_default:
            llm.save({})
        audit.log(u["email"], "connect_ai.disconnect", other, "its key was removed")
    return {**out, "view": connectai.view(u, None, True, None)}


class DefaultIn(BaseModel):
    engine: str


@router.post("/api/connect-ai/default")
def connect_default(body: DefaultIn, u: dict = Depends(_admin), _f=Depends(_connect)) -> dict:
    if llm.status().get("operator_managed"):
        raise HTTPException(403, "this instance uses managed inference (operated by NINABOT)")
    try:
        connectai.set_default(body.engine)
    except connectai.ConnectError as e:
        _ce(e)
    audit.log(u["email"], "connect_ai.default", body.engine)
    return connectai.view(u, None, True, None)


class PolicyIn(BaseModel):
    allowed: list[str] = []
    zones: dict[str, str] = {}
    allowed_zones: list[str] = []
    tiers: list[str] = []
    per_project: bool = False


@router.put("/api/connect-ai/policy")
def connect_policy(body: PolicyIn, u: dict = Depends(_admin), _f=Depends(_connect)) -> dict:
    if connectai.mode() != "governed":
        raise HTTPException(400, "the engine policy exists in governed mode only")
    try:
        p = connectai.set_policy(body.allowed, body.zones, body.allowed_zones, body.tiers,
                                 body.per_project)
    except connectai.ConnectError as e:
        _ce(e)
    audit.log(u["email"], "connect_ai.policy", "instance",
              f"allowed={','.join(p['allowed'])} zones={','.join(p['allowed_zones'])} "
              f"tiers={','.join(p['tiers'])} per_project={p['per_project']}")
    return connectai.view(u, None, True, None)


class ProjectEngineIn(BaseModel):
    engine: str | None = None


@router.put("/api/connect-ai/project/{slug}")
def connect_project(slug: str, body: ProjectEngineIn, u: dict = Depends(auth.current_user),
                    _f=Depends(_connect)) -> dict:
    role = projects.effective_role(u, slug)
    if role is None:
        raise HTTPException(404, f"no project '{slug}' for you")
    if projects.prank(role) < projects.prank("maintainer"):
        raise HTTPException(403, "choosing the project's engine needs the maintainer role")
    try:
        connectai.set_project_engine(slug, body.engine or None)
    except connectai.ConnectError as e:
        _ce(e)
    audit.log(u["email"], "connect_ai.project", slug, body.engine or "(instance default)")
    return {"ok": True, "project": slug, "engine": body.engine or None}


@router.get("/api/connect-ai/crew-engines")
def connect_crew_engines(_u: dict = Depends(auth.current_user)) -> list[dict]:
    return connectai.crew_engines()
