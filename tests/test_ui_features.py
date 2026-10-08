"""SOKKAN 3.2 — `shared_review`, `byok_admin`, `connect_ai`, through the real request path
(middleware → projectgate → route); only the identity is chosen (`auth.resolve_email`).

People:
  * bob@x    — instance dev (= dev of `default`), owner of the default session;
  * dave@x   — instance dev;
  * carol@x  — instance viewer;
  * alice@x  — unknown to the instance, dev of `radio` through the SSO group `radio-devs`;
  * eve@x    — unknown to the instance, in `radio-devs` too;
  * admin@x  — instance admin.
"""
import json
import time

import pytest

DEFAULT_SID = "d" * 32
RADIO_SID = "r" * 32
SECRET_KEY = "sk-ant-api03-THIS-IS-THE-SECRET-abcd1234WXYZ"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import audit
    import auth
    import board
    import iam
    import llm
    import projects
    import sharing
    import vault

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    for f in ("MULTI_PROJECT", "SHARED_REVIEW", "BYOK_ADMIN", "CONNECT_AI"):
        monkeypatch.setenv(f"SOKKAN_FEATURE_{f}", "1")
    for v in ("SOKKAN_CONNECT_AI_MODE", "SOKKAN_ROUTER_WELCOME_URL", "SOKKAN_GATEWAY_URL",
              "SOKKAN_GATEWAY_ADMIN_TOKEN", "SOKKAN_GATEWAY_CLIENT", "SOKKAN_INFER_BASE_URL",
              "SOKKAN_INFER_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
              "SOKKAN_EDITION"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    for e, r in (("bob@x", "dev"), ("dave@x", "dev"), ("carol@x", "viewer"), ("admin@x", "admin")):
        iam.upsert_user(e, r)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(sharing, "DB", tmp_path / "shares.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    projects.create("radio", "Radio player", created_by="admin@x")
    projects.sync_sso_groups("alice@x", ["radio-devs"])
    projects.sync_sso_groups("eve@x", ["radio-devs"])
    projects.grant("radio", "team", "sso:radio-devs", "dev")
    board.add_sdk_session(DEFAULT_SID, "backend", title="default plan", project="default")
    board.add_sdk_session(RADIO_SID, "backend", title="radio work", project="radio")

    who = {"email": "bob@x"}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    c = TestClient(a.app)

    def as_(email, project=None):
        who["email"] = email
        c.headers.pop("x-sokkan-project", None)
        if project:
            c.headers["x-sokkan-project"] = project
        return c

    return {"as": as_, "tmp": tmp_path}


def _share(c, **kw):
    body = {"kind": "session", "target": DEFAULT_SID, "principal_kind": "user",
            "principal": "dave@x", "access": "read", **kw}
    return c.post("/api/shares", json=body)


def _audit(action):
    import audit
    return [e for e in audit.recent(500) if e["action"] == action]


# ================================ shared_review ==========================================

def test_share_a_session_read_shows_in_the_recipient_inbox_and_is_logged(world):
    r = _share(world["as"]("bob@x"))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    inbox = world["as"]("dave@x").get("/api/shares/inbox").json()
    assert [(s["id"], s["created_by"], s["effective_access"]) for s in inbox] == \
        [(sid, "bob@x", "read")]
    assert world["as"]("dave@x").get(f"/api/shares/{sid}").status_code == 200
    assert _audit("share.create") and _audit("share.view")
    # the owner lists the shares of their session
    assert [s["id"] for s in world["as"]("bob@x").get(
        "/api/shares", params={"kind": "session", "target": DEFAULT_SID}).json()] == [sid]


def test_a_viewer_never_gets_write(world):
    r = _share(world["as"]("bob@x"), principal="carol@x", access="write")
    assert r.status_code == 400 and "viewer never gets write" in r.json()["detail"]
    assert _share(world["as"]("bob@x"), principal="carol@x").status_code == 200
    # and a viewer cannot share
    assert _share(world["as"]("carol@x"), principal="bob@x").status_code == 403


def test_write_is_capped_by_the_current_role(world):
    import iam
    sid = _share(world["as"]("bob@x"), access="write", approver=True).json()["id"]
    v = world["as"]("dave@x").get(f"/api/shares/{sid}").json()
    assert v["effective_access"] == "write" and v["approver"] is True
    iam.upsert_user("dave@x", "viewer")
    v = world["as"]("dave@x").get(f"/api/shares/{sid}").json()
    assert v["effective_access"] == "read" and v["approver"] is False


def test_non_member_sees_nothing(world):
    """No leak: a person outside the object's project can neither be a recipient nor see,
    list, open or screenshot anything of it — the answers are the not-found ones."""
    r = _share(world["as"]("bob@x"), principal="alice@x")
    assert r.status_code == 400 and "no role in project" in r.json()["detail"]
    sid = _share(world["as"]("bob@x")).json()["id"]
    alice = world["as"]("alice@x", "radio")
    assert alice.get("/api/shares/inbox").json() == []
    assert alice.get(f"/api/shares/{sid}").status_code == 404
    assert alice.get(f"/api/shares/{sid}/shot").status_code == 404
    assert alice.delete(f"/api/shares/{sid}").status_code == 404
    assert alice.post(f"/api/shares/{sid}/decide",
                      json={"permission_id": "x", "decision": "allow"}).status_code == 404
    for path in ("/api/shares", "/api/shares/people"):
        r = alice.get(path, params={"kind": "session", "target": DEFAULT_SID})
        assert r.status_code == 404 and "default plan" not in r.text and "dave" not in r.text
    # sharing a session of a project one is not in: not found, never forbidden
    assert _share(alice, principal="eve@x").status_code == 404
    # and the other way round: bob cannot reach radio's session
    assert _share(world["as"]("bob@x"), target=RADIO_SID).status_code == 404


def test_team_share_reaches_its_members_only_while_active(world, monkeypatch):
    c = world["as"]("alice@x", "radio")
    r = c.post("/api/shares", json={"kind": "session", "target": RADIO_SID,
                                    "principal_kind": "team", "principal": "sso:radio-devs",
                                    "access": "write", "expires_in_s": 3600})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert [s["id"] for s in world["as"]("eve@x").get("/api/shares/inbox").json()] == [sid]
    assert world["as"]("bob@x").get("/api/shares/inbox").json() == []
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 7200)
    assert world["as"]("eve@x").get("/api/shares/inbox").json() == []


def test_a_recipient_who_leaves_the_project_sees_nothing(world):
    import projects
    projects.grant("radio", "user", "eve@x", "viewer")
    sid = world["as"]("alice@x", "radio").post("/api/shares", json={
        "kind": "session", "target": RADIO_SID, "principal": "eve@x"}).json()["id"]
    assert [s["id"] for s in world["as"]("eve@x").get("/api/shares/inbox").json()] == [sid]
    projects.revoke("radio", "team", "sso:radio-devs")
    projects.revoke("radio", "user", "eve@x")
    assert world["as"]("eve@x").get("/api/shares/inbox").json() == []
    assert world["as"]("eve@x").get(f"/api/shares/{sid}").status_code == 404


def test_revoke_is_logged_and_removes_the_share(world):
    sid = _share(world["as"]("bob@x")).json()["id"]
    assert world["as"]("dave@x").delete(f"/api/shares/{sid}").status_code == 404  # not his
    assert world["as"]("bob@x").delete(f"/api/shares/{sid}").json()["revoked_at"]
    assert world["as"]("dave@x").get("/api/shares/inbox").json() == []
    assert world["as"]("dave@x").get(f"/api/shares/{sid}").status_code == 404
    assert _audit("share.revoke")


class _FakeSession:
    def __init__(self):
        self.events = [{"type": "permission", "id": "p1", "tool": "Bash",
                        "title": "Run: rm -rf build", "input": {}}]
        self._perms = {"p1": object()}
        self.decisions = []

    def resolve_permission(self, pid, decision):
        self.decisions.append((pid, decision))
        self._perms.pop(pid, None)


def test_delegated_hitl_approval(world, monkeypatch):
    import agentchat
    fake = _FakeSession()
    monkeypatch.setattr(agentchat, "peek", lambda sid: fake if sid == DEFAULT_SID else None)
    bob = world["as"]("bob@x")
    read_id = _share(bob).json()["id"]
    # approval needs a write share
    assert bob.post(f"/api/shares/{read_id}/approver", json={"approver": True}).status_code == 400
    sid = _share(bob, access="write").json()["id"]
    assert bob.post(f"/api/shares/{sid}/approver", json={"approver": True}).json()["approver"]
    inbox = world["as"]("dave@x").get("/api/shares/inbox").json()
    mine = next(s for s in inbox if s["id"] == sid)
    assert mine["pending"] == [{"id": "p1", "tool": "Bash", "title": "Run: rm -rf build"}]
    # the read share's recipient cannot decide through it
    assert world["as"]("dave@x").post(f"/api/shares/{read_id}/decide", json={
        "permission_id": "p1", "decision": "allow"}).status_code == 403
    r = world["as"]("dave@x").post(f"/api/shares/{sid}/decide",
                                   json={"permission_id": "p1", "decision": "deny"})
    assert r.status_code == 200 and fake.decisions[0][1]["decision"] == "deny"
    assert "dave@x" in fake.decisions[0][1]["message"]
    assert _audit("share.hitl.deny") and _audit("share.approver")
    # already decided
    assert world["as"]("dave@x").post(f"/api/shares/{sid}/decide", json={
        "permission_id": "p1", "decision": "allow"}).status_code == 404


def test_preview_share_to_a_viewer_serves_only_the_shared_url(world, monkeypatch):
    import preview
    png = world["tmp"] / "shot.png"
    png.write_bytes(b"\x89PNG fake")
    asked = []
    monkeypatch.setattr(preview, "screenshot", lambda url, w, h: (asked.append(url), png)[1])
    r = world["as"]("bob@x").post("/api/shares", json={
        "kind": "preview", "target": "http://localhost:3000/pricing", "principal": "carol@x",
        "title": "pricing page", "path": "/pricing", "env": "web"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["project"] == "default" and r.json()["preview"]["path"] == "/pricing"
    carol = world["as"]("carol@x")
    assert carol.get(f"/api/shares/{sid}/shot").status_code == 200
    assert asked == ["http://localhost:3000/pricing"]
    assert world["as"]("dave@x").get(f"/api/shares/{sid}/shot").status_code == 404
    # carol, a viewer, still has no general screenshot route
    assert world["as"]("carol@x").get("/api/preview/shot", params={"url": "http://localhost:3000"}).status_code == 403


def test_people_picker_lists_members_with_write_eligibility(world):
    rows = world["as"]("bob@x").get("/api/shares/people",
                                    params={"kind": "session", "target": DEFAULT_SID}).json()
    got = {(r["id"], r["write_ok"]) for r in rows}
    assert ("carol@x", False) in got and ("dave@x", True) in got
    assert not any(r["id"] in ("bob@x", "alice@x", "sso:radio-devs") for r in rows)


def test_people_picker_includes_team_members_with_effective_role(world):
    import projects
    projects.sync_sso_groups("vic@x", ["radio-readers"])
    projects.grant("radio", "team", "sso:radio-readers", "viewer")
    projects.grant("radio", "user", "dave@x", "viewer")
    projects.sync_sso_groups("dave@x", ["radio-devs"])   # direct viewer + dev via team
    rows = world["as"]("eve@x").get("/api/shares/people",
                                    params={"kind": "session", "target": RADIO_SID}).json()
    got = {(r["kind"], r["id"]): (r["role"], r["write_ok"]) for r in rows}
    assert got[("user", "alice@x")] == ("dev", True)          # member via sso:radio-devs only
    assert got[("user", "vic@x")] == ("viewer", False)        # viewer: never write
    assert got[("user", "dave@x")] == ("dev", True)           # best of grant and team
    assert got[("team", "sso:radio-devs")] == ("dev", True)
    assert got[("team", "sso:radio-readers")] == ("viewer", False)
    assert ("user", "eve@x") not in got                        # never oneself
    r = world["as"]("eve@x").post("/api/shares", json={
        "kind": "session", "target": RADIO_SID, "principal_kind": "user",
        "principal": "alice@x", "access": "write"})
    assert r.status_code == 200, r.text


def test_shared_review_off_means_404(world, monkeypatch):
    monkeypatch.setenv("SOKKAN_FEATURE_SHARED_REVIEW", "0")
    assert world["as"]("bob@x").get("/api/shares/inbox").status_code == 404
    monkeypatch.setenv("SOKKAN_FEATURE_SHARED_REVIEW", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "0")   # requires multi_project
    assert world["as"]("bob@x").get("/api/shares/inbox").status_code == 404


# ================================ byok_admin ==============================================

class _Resp:
    def __init__(self, status):
        self.status_code = status


class _FakeHttp:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers, None))
        return _Resp(self.status)

    def put(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, headers, json))
        return _Resp(self.status)

    def delete(self, url, headers=None, timeout=None):
        self.calls.append(("DELETE", url, headers, None))
        return _Resp(204)


def test_byok_key_is_encrypted_masked_used_by_sessions_and_never_logged(world, monkeypatch):
    import llm
    import modelkeys
    http = _FakeHttp(200)
    monkeypatch.setattr(modelkeys, "_http", http)
    def adm_():
        return world["as"]("admin@x")
    r = adm_().put("/api/admin/model-keys/anthropic", json={"key": SECRET_KEY, "test": True})
    assert r.status_code == 200, r.text
    assert SECRET_KEY not in r.text and r.json()["key"]["masked"] == "…WXYZ"
    assert r.json()["test"]["ok"] is True
    assert http.calls[0][1] == "https://api.anthropic.com/v1/models"
    listing = adm_().get("/api/admin/model-keys").json()
    assert listing["keys"][0]["masked"] == "…WXYZ" and listing["keys"][0]["set_by"] == "admin@x"
    assert SECRET_KEY not in json.dumps(listing)
    # encrypted at rest, and llm.json only holds a reference
    tmp = world["tmp"]
    assert SECRET_KEY not in (tmp / "modelkeys.json").read_text()
    assert SECRET_KEY not in (tmp / "llm.json").read_text()
    assert llm.session_env("x@y")["ANTHROPIC_API_KEY"] == SECRET_KEY
    assert llm.status()["byok_kind"] == "api_key"
    import audit
    assert all(SECRET_KEY not in json.dumps(e) for e in audit.recent(500))
    # delete: sessions lose it
    assert adm_().delete("/api/admin/model-keys/anthropic").status_code == 200
    assert "ANTHROPIC_API_KEY" not in llm.session_env("x@y")


def test_byok_pushes_to_the_gateway_when_configured(world, monkeypatch):
    import modelkeys
    http = _FakeHttp(200)
    monkeypatch.setattr(modelkeys, "_http", http)
    monkeypatch.setenv("SOKKAN_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("SOKKAN_GATEWAY_ADMIN_TOKEN", "adm-tok")
    monkeypatch.setenv("SOKKAN_GATEWAY_CLIENT", "rts-poc")
    def adm_():
        return world["as"]("admin@x")
    r = adm_().put("/api/admin/model-keys/anthropic", json={"key": SECRET_KEY})
    assert r.json()["gateway"]["pushed"] is True and r.json()["key"]["pushed_at"]
    method, url, headers, body = http.calls[-1]
    assert (method, url, body) == ("PUT", "https://gw.example/admin/tenant/rts-poc/byok",
                                   {"key": SECRET_KEY})
    assert headers["authorization"] == "Bearer adm-tok"
    adm_().delete("/api/admin/model-keys/anthropic")
    assert http.calls[-1][:2] == ("DELETE", "https://gw.example/admin/tenant/rts-poc/byok/anthropic")


def test_byok_rejected_key_and_gateway_error_are_reported_without_the_key(world, monkeypatch):
    import modelkeys
    monkeypatch.setattr(modelkeys, "_http", _FakeHttp(401))
    monkeypatch.setenv("SOKKAN_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("SOKKAN_GATEWAY_ADMIN_TOKEN", "adm-tok")
    monkeypatch.setenv("SOKKAN_GATEWAY_CLIENT", "rts-poc")
    r = world["as"]("admin@x").put("/api/admin/model-keys/anthropic",
                                   json={"key": SECRET_KEY, "test": True})
    assert r.json()["test"] == {"ok": False, "detail": "rejected (HTTP 401)"}
    assert r.json()["key"]["push_error"] == "gateway answered HTTP 401"
    assert SECRET_KEY not in r.text


def test_byok_admin_only_instance_scope_and_feature_gate(world, monkeypatch):
    assert world["as"]("bob@x").get("/api/admin/model-keys").status_code == 403
    def adm_():
        return world["as"]("admin@x")
    r = adm_().put("/api/admin/model-keys/anthropic", json={"key": SECRET_KEY, "scope": "project:radio"})
    assert r.status_code == 400 and "planned" in r.json()["detail"]
    assert adm_().put("/api/admin/model-keys/nope", json={"key": SECRET_KEY}).status_code == 400
    monkeypatch.setenv("SOKKAN_FEATURE_BYOK_ADMIN", "0")
    assert adm_().get("/api/admin/model-keys").status_code == 404


# ================================ connect_ai ==============================================

def test_personal_mode_lists_every_engine_router_preselected(world, monkeypatch):
    monkeypatch.setenv("SOKKAN_ROUTER_WELCOME_URL", "https://router.example/welcome")
    v = world["as"]("bob@x").get("/api/connect-ai").json()
    assert v["mode"] == "personal"
    ids = [e["id"] for e in v["engines"]]
    assert ids == ["sokkan_router", "claude", "openai", "gemini", "openrouter", "ollama",
                   "magnitude"]
    assert [e["id"] for e in v["engines"] if e["preselected"]] == ["sokkan_router"]
    assert v["welcome_url"] == "https://router.example/welcome"
    assert "terms" in v["login_note"]
    assert v["can_admin"] is False


def test_connect_engine_default_sessions_and_crew(world):
    import connectai
    import llm
    def adm_():
        return world["as"]("admin@x")
    r = adm_().put("/api/connect-ai/engines/ollama", json={
        "auth": "none", "base_url": "http://gpu.lan:11434", "model": "qwen3-coder"})
    assert r.status_code == 200, r.text
    assert world["as"]("bob@x").put("/api/connect-ai/engines/ollama", json={
        "auth": "none", "base_url": "http://x:1"}).status_code == 403
    assert adm_().post("/api/connect-ai/default", json={"engine": "ollama"}).status_code == 200
    env = llm.session_env("bob@x")
    assert env["ANTHROPIC_BASE_URL"] == "http://gpu.lan:11434"
    assert llm.session_model() == "qwen3-coder"
    adm_().put("/api/connect-ai/engines/claude", json={"auth": "key", "key": SECRET_KEY})
    crew = [e["value"] for e in world["as"]("bob@x").get("/api/connect-ai/crew-engines").json()]
    assert crew == ["engine:claude", "engine:ollama"]
    env, model = connectai.session_overrides("any", "engine:claude")
    assert env["ANTHROPIC_API_KEY"] == SECRET_KEY and env["ANTHROPIC_BASE_URL"] == "" and model == ""
    # an engine that is gone: back to the instance default, never a stale key
    adm_().delete("/api/connect-ai/engines/claude")
    env, model = connectai.session_overrides("any", "engine:claude")
    assert env == {} and model == "qwen3-coder"
    # a normal session is untouched
    assert connectai.session_overrides("any", "sonnet") == ({}, None)
    v = adm_().get("/api/connect-ai").json()
    assert SECRET_KEY not in json.dumps(v)


def test_governed_mode_admin_list_zones_tiers_and_project_choice(world, monkeypatch):
    import projects
    monkeypatch.setenv("SOKKAN_CONNECT_AI_MODE", "governed")
    def adm_():
        return world["as"]("admin@x")
    r = adm_().put("/api/connect-ai/policy", json={
        "allowed": ["claude", "sokkan_router", "ollama"], "zones": {"sokkan_router": "CH"},
        "allowed_zones": ["CH", "local"], "tiers": ["sokkan-ship", "sokkan-swiss"],
        "per_project": True})
    assert r.status_code == 200, r.text
    v = world["as"]("bob@x").get("/api/connect-ai").json()
    # claude has no declared zone → not in an allowed zone → hidden
    assert [e["id"] for e in v["engines"]] == ["sokkan_router", "ollama"]
    assert v["welcome_url"] == ""
    assert adm_().put("/api/connect-ai/engines/openai", json={
        "auth": "key", "key": SECRET_KEY, "base_url": "http://litellm:4000"}).status_code == 403
    r = adm_().put("/api/connect-ai/engines/sokkan_router", json={
        "auth": "key", "key": "sr-key-123456789", "model": "gpt-big"})
    assert r.status_code == 400 and "allowed tiers" in r.json()["detail"]
    assert adm_().put("/api/connect-ai/engines/sokkan_router", json={
        "auth": "key", "key": "sr-key-123456789", "model": "sokkan-swiss"}).status_code == 200
    # project choice: maintainer of radio only
    alice = world["as"]("alice@x", "radio")
    assert alice.put("/api/connect-ai/project/radio", json={"engine": "sokkan_router"}).status_code == 403
    projects.grant("radio", "user", "alice@x", "maintainer")
    assert alice.get("/api/connect-ai").json()["project"]["can_choose"] is True
    assert alice.put("/api/connect-ai/project/radio",
                     json={"engine": "sokkan_router"}).status_code == 200
    assert world["as"]("bob@x").put("/api/connect-ai/project/radio",
                                    json={"engine": None}).status_code == 404
    import connectai
    env, model = connectai.session_overrides(RADIO_SID, "")
    assert env["ANTHROPIC_BASE_URL"] == "https://router.sokkan.ch" and model == "sokkan-swiss"
    assert connectai.session_overrides(DEFAULT_SID, "") == ({}, None)
    # personal mode has no policy
    monkeypatch.setenv("SOKKAN_CONNECT_AI_MODE", "personal")
    assert adm_().put("/api/connect-ai/policy", json={}).status_code == 400


def test_connect_ai_off_means_404_and_engine_models_fall_back(world, monkeypatch):
    import connectai
    monkeypatch.setenv("SOKKAN_FEATURE_CONNECT_AI", "0")
    assert world["as"]("bob@x").get("/api/connect-ai").status_code == 404
    assert world["as"]("bob@x").get("/api/connect-ai/crew-engines").json() == []
    assert connectai.session_overrides("x", "engine:claude") == ({}, "")
