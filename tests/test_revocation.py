"""3.2 lot 6 — revocation: SCIM 2.0, « Revoke now », teams at login, owner checks.

Each path ends in the same effect, checked on the real middleware (TestClient):
cookie refused, API refused, live SDK session stopped, open WebSocket closed, owned agents
paused (+ notification), forge tokens erased, access cache purged, audit entries.
"""
import asyncio
import time

import jwt
import pytest

TOKEN = "scim-test-token-0123456789"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/scim+json"}
SCIM = "/api/scim/v2"


class FakeWS:
    def __init__(self):
        self.closed = None

    async def close(self, code=1000):
        self.closed = code


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import agentchat
    import agents
    import app as a
    import audit
    import auth
    import board
    import iam
    import notify
    import projects
    import revocation

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_AUTH_MODE", "oidc")             # sso → sso_teams → revocation
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_REVOCATION", "1")
    monkeypatch.setenv("SOKKAN_SCIM_TOKEN", TOKEN)
    monkeypatch.setenv("SOKKAN_AGENTS_USE_CLI_LOGIN", "1")
    monkeypatch.setattr(revocation, "DB", tmp_path / "identity.db")
    monkeypatch.setattr(revocation, "_init_for", None)
    monkeypatch.setattr(revocation, "_sockets", {})
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    iam.upsert_user("admin@x", "admin")
    iam.upsert_user("bob@x", "dev")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    sent = []
    monkeypatch.setattr(notify, "send", lambda *x, **k: sent.append(x) or {})
    monkeypatch.setattr(agentchat, "_registry", {})
    projects.create("radio", "Radio", created_by="admin@x")
    projects.grant("radio", "team", "sso:radio-devs", "dev")
    projects.sync_sso_groups("alice@x", ["radio-devs"])
    who = {"email": "admin@x"}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    monkeypatch.setattr(a, "_bg", lambda coro: coro.close())
    c = TestClient(a.app)

    def as_(email, project=None):
        who["email"] = email
        c.headers.pop("x-sokkan-project", None)
        if project:
            c.headers["x-sokkan-project"] = project
        return c

    def alice_world():
        """Alice: a live SDK session + an active agent in radio, a forge token, a cache
        row, an open chat WebSocket."""
        sid = agentchat.new_sid()
        board.add_sdk_session(sid, "t", project="radio")
        agentchat._registry[sid] = agentchat.AgentSession(sid, cwd=str(tmp_path), user="alice@x")
        ag = agents.create({"email": "alice@x", "role": "dev", "project": "radio"},
                           {"name": "radio-nightly", "purpose": "p", "deliverable": "d",
                            "trigger": "manual"}, activate=True)
        con = projects._con()
        with con:
            con.execute("INSERT INTO forge_links(email, provider, base_url, token_enc, "
                        "refresh_enc, linked_at) VALUES('alice@x','gitlab','https://gl','enc',"
                        "'renc',?)", (time.time(),))
            con.execute("INSERT INTO access_cache(email, project, role, source, computed_at, "
                        "expires_at) VALUES('alice@x','radio','dev','forge',?,?)",
                        (time.time(), time.time() + 600))
        con.close()
        ws = FakeWS()
        revocation.track("alice@x", ws)
        return {"sid": sid, "agent": ag, "ws": ws}

    return {"c": c, "as": as_, "alice": alice_world, "sent": sent, "tmp": tmp_path}


def _effects_gone(w, al):
    import agentchat
    import agents
    import projects
    assert al["sid"] not in agentchat._registry, "live SDK session not stopped"
    assert al["ws"].closed == 4401, "open WebSocket not closed"
    assert agents.get(al["agent"]["id"])["status"] == "paused", "owned agent not paused"
    assert any("paused" in str(s) for s in w["sent"]), "no notification"
    con = projects._con()
    fl = dict(con.execute("SELECT * FROM forge_links WHERE email='alice@x'").fetchone())
    ac = con.execute("SELECT count(*) FROM access_cache WHERE email='alice@x'").fetchone()[0]
    con.close()
    assert fl["token_enc"] == "" and fl["refresh_enc"] == "" and fl["revoked_at"]
    assert ac == 0


def _cookie_request(email, iat):
    import session as sess

    class R:
        cookies = {sess.COOKIE: jwt.encode({"email": email, "iat": iat, "exp": iat + 3600},
                                           sess.SECRET, algorithm="HS256")}
    return R()


# ---- SCIM -----------------------------------------------------------------------------

def test_scim_needs_its_token_and_the_feature(world, monkeypatch):
    c = world["c"]
    assert c.get(f"{SCIM}/Users").status_code == 401
    assert c.get(f"{SCIM}/Users", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get(f"{SCIM}/ServiceProviderConfig", headers=H).json()["patch"]["supported"]
    monkeypatch.setenv("SOKKAN_SCIM_TOKEN", "")
    assert c.get(f"{SCIM}/Users", headers=H).status_code == 404
    monkeypatch.setenv("SOKKAN_SCIM_TOKEN", TOKEN)
    monkeypatch.setenv("SOKKAN_FEATURE_REVOCATION", "0")
    assert c.get(f"{SCIM}/Users", headers=H).status_code == 404


def _create_alice(c):
    r = c.post(f"{SCIM}/Users", headers=H, json={   # Entra ID shape
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "externalId": "00aa-entra-oid", "userName": "Alice@X", "active": True,
        "displayName": "Alice", "emails": [{"primary": True, "type": "work",
                                            "value": "alice@x"}]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_scim_user_lifecycle_deactivate_is_immediate(world):
    import iam
    import revocation
    import session as sess
    c, al = world["c"], world["alice"]()
    uid = _create_alice(c)
    assert c.post(f"{SCIM}/Users", headers=H, json={"userName": "alice@x"}).status_code == 409
    found = c.get(f'{SCIM}/Users?filter=userName eq "alice@x"', headers=H).json()
    assert found["totalResults"] == 1 and found["Resources"][0]["active"] is True
    old_cookie = _cookie_request("alice@x", int(time.time()) - 5)
    assert sess.email_from_request(old_cookie) == "alice@x"
    assert world["as"]("alice@x", "radio").get("/api/sessions").status_code == 200

    # Entra ID deactivation: PATCH replace active "False" (a string)
    r = world["as"]("admin@x").patch(f"{SCIM}/Users/{uid}", headers=H, json={
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "Replace", "path": "active", "value": "False"}]})
    assert r.status_code == 200 and r.json()["active"] is False
    assert revocation.is_disabled("alice@x")
    assert sess.email_from_request(old_cookie) is None
    assert world["as"]("alice@x", "radio").get("/api/sessions").status_code == 403
    _effects_gone(world, al)
    import audit
    acts = [e["action"] for e in audit.recent(50)]
    assert "user.revoke" in acts and "agent.pause.owner_access" in acts

    # reactivated: a NEW login works, the old cookie stays dead, the agent stays paused
    r = world["as"]("admin@x").patch(f"{SCIM}/Users/{uid}", headers=H, json={
        "Operations": [{"op": "replace", "value": {"active": True}}]})   # Okta/Entra shape
    assert r.json()["active"] is True and not revocation.is_disabled("alice@x")
    assert sess.email_from_request(old_cookie) is None
    time.sleep(1.1)   # a cookie's iat has a one-second resolution
    assert sess.email_from_request(_cookie_request("alice@x", int(time.time()))) == "alice@x"
    import agents
    assert agents.get(al["agent"]["id"])["status"] == "paused"
    del iam


def test_scim_delete_and_authentik_put(world):
    import revocation
    c = world["c"]
    al = world["alice"]()
    uid = _create_alice(c)
    # Authentik: PUT the whole user with active false
    r = c.put(f"{SCIM}/Users/{uid}", headers=H, json={"userName": "alice@x", "active": False,
                                                      "emails": [{"value": "alice@x"}]})
    assert r.status_code == 200 and revocation.is_disabled("alice@x")
    _effects_gone(world, al)
    assert c.delete(f"{SCIM}/Users/{uid}", headers=H).status_code == 204
    assert c.get(f"{SCIM}/Users/{uid}", headers=H).status_code == 404
    assert revocation.is_disabled("alice@x")


def test_scim_group_membership_drives_the_team(world):
    import agentchat
    import agents
    import projects
    c = world["c"]
    al = world["alice"]()
    uid = _create_alice(c)
    bob = c.post(f"{SCIM}/Users", headers=H, json={"userName": "bob@x"}).json()["id"]
    g = c.post(f"{SCIM}/Groups", headers=H, json={
        "displayName": "radio-devs", "members": [{"value": uid}, {"value": bob}]}).json()
    assert {m["display"] for m in g["members"]} == {"alice@x", "bob@x"}
    assert "bob@x" in [m for m in _team("sso:radio-devs")]
    assert projects.effective_role({"email": "bob@x", "role": "dev"}, "radio") == "dev"
    # Entra removes alice from the group → she loses radio: session stopped, agent paused
    r = c.patch(f"{SCIM}/Groups/{g['id']}", headers=H, json={"Operations": [
        {"op": "Remove", "path": f'members[value eq "{uid}"]'}]})
    assert r.status_code == 200 and [m["display"] for m in r.json()["members"]] == ["bob@x"]
    assert projects.effective_role({"email": "alice@x", "role": "none"}, "radio") is None
    assert al["sid"] not in agentchat._registry
    assert agents.get(al["agent"]["id"])["status"] == "paused"
    # Authentik pushes the full list with PUT; a group delete empties the team
    r = c.put(f"{SCIM}/Groups/{g['id']}", headers=H, json={"displayName": "radio-devs",
                                                           "members": [{"value": uid}]})
    assert sorted(_team("sso:radio-devs")) == ["alice@x"]
    assert c.delete(f"{SCIM}/Groups/{g['id']}", headers=H).status_code == 204
    assert _team("sso:radio-devs") == []
    assert c.get(f'{SCIM}/Groups?filter=displayName eq "radio-devs"', headers=H
                 ).json()["totalResults"] == 0


def _team(tid):
    import projects
    con = projects._con()
    rows = [r["email"] for r in con.execute("SELECT email FROM team_members WHERE team_id=?",
                                            (tid,))]
    con.close()
    return rows


# ---- « Revoke now » -------------------------------------------------------------------

def test_revoke_now_button(world):
    import revocation
    al = world["alice"]()
    as_ = world["as"]
    assert as_("bob@x").post("/api/admin/users/alice@x/revoke").status_code == 403
    assert as_("admin@x").post("/api/admin/users/admin@x/revoke").status_code == 400
    r = as_("admin@x").post("/api/admin/users/alice@x/revoke", json={"reason": "left"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sessions_stopped"] == 1 and out["agents_paused"] == 1
    assert out["forge_tokens_erased"] == 1 and out["sockets_closed"] == 1
    _effects_gone(world, al)
    assert _team("sso:radio-devs") == []
    st = as_("admin@x").get("/api/admin/revocation").json()
    assert [d["email"] for d in st["disabled"]] == ["alice@x"] and st["scim"]["enabled"]
    assert as_("alice@x", "radio").get("/api/projects").status_code == 403
    assert as_("admin@x").post("/api/admin/users/alice@x/reinstate").status_code == 200
    assert not revocation.is_disabled("alice@x")


def test_revoke_now_is_404_when_the_feature_is_off(world, monkeypatch):
    monkeypatch.setenv("SOKKAN_FEATURE_REVOCATION", "0")
    assert world["as"]("admin@x").post("/api/admin/users/alice@x/revoke").status_code == 404


def test_live_chat_websocket_is_closed_on_revoke(world, monkeypatch):
    """The real agent WebSocket: a message after the revocation closes it."""
    import agentchat
    import board
    import revocation
    import app as a
    world["alice"]()
    monkeypatch.setattr(a, "_origin_ok", lambda ws: True)
    monkeypatch.setattr(a, "_ws_user", lambda ws: {"email": "alice@x", "role": "none"})
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "t", project="radio")
    c = world["c"]
    with c.websocket_connect(f"/api/agent/ws/{sid}") as ws:
        assert "alice@x" in revocation._sockets
        # revoked while connected (the close itself is covered with FakeWS above): the
        # next message is refused and the socket closed
        revocation._set_state("alice@x", disabled=True, by="admin@x", cut_sessions=True)
        ws.send_json({"type": "user", "text": "still there?"})
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(20):
                ws.receive_json()
        assert e.value.code == 4401


# ---- SSO login ------------------------------------------------------------------------

def _login(world, monkeypatch, email, groups):
    import app as a
    import oidc
    import session as sess
    monkeypatch.setattr(oidc, "exchange", lambda *x: {"id_token": "t"})
    monkeypatch.setattr(oidc, "verify_id_token",
                        lambda t: {"email": email, "name": email, "groups": groups})
    tx = jwt.encode({"s": "st", "v": "ver", "exp": int(time.time()) + 600}, sess.SECRET,
                    algorithm="HS256")
    c = world["c"]
    c.cookies.set("sokkan_oidc_tx", tx)
    r = c.get("/api/auth/callback?code=x&state=st", follow_redirects=False)
    c.cookies.clear()
    del a
    return r


def test_login_recomputes_teams_and_withdraws_lost_access(world, monkeypatch):
    import agentchat
    import agents
    import projects
    al = world["alice"]()
    r = _login(world, monkeypatch, "alice@x", ["radio-devs", "other"])
    assert r.status_code == 302
    assert agents.get(al["agent"]["id"])["status"] == "active"
    assert al["sid"] in agentchat._registry
    r = _login(world, monkeypatch, "alice@x", ["other"])       # left the radio team
    assert r.status_code == 302
    assert projects.team_ids("alice@x") == ["sso:other"]
    assert agents.get(al["agent"]["id"])["status"] == "paused"
    assert al["sid"] not in agentchat._registry


def test_a_disabled_account_cannot_log_in(world, monkeypatch):
    import revocation
    asyncio.run(revocation.revoke("alice@x", "admin@x", "left"))
    assert _login(world, monkeypatch, "alice@x", ["radio-devs"]).status_code == 403
    assert projects_teams_unchanged()


def projects_teams_unchanged():
    import projects
    return projects.team_ids("alice@x") == []


# ---- scheduler ------------------------------------------------------------------------

def _run_once(a):
    import agents
    import agents_runtime
    run = agents.request_run({"email": "admin@x", "role": "admin", "project": "radio"}, a["id"])
    rt = agents_runtime.Runtime(recall=lambda q, sid: "")
    started = []

    async def fake_exec(agent, run_):
        started.append(run_["id"])
        agents.update_run(run_["id"], status="succeeded")
    rt._execute = fake_exec

    async def go():
        rt.start_queued()
        for t in list(rt.tasks.values()):
            await t
    asyncio.new_event_loop().run_until_complete(go())
    return agents.get_run(run["id"]), started


def test_an_agent_does_not_outlive_its_owner_access(world, monkeypatch):
    import agents
    import projects
    al = world["alice"]()
    a = al["agent"]
    done, started = _run_once(a)
    assert started and done["status"] == "succeeded"           # owner has access: runs
    projects.sync_sso_groups("alice@x", [])                    # left the team (no login yet)
    done, started = _run_once(a)
    assert not started and done["status"] == "cancelled" and "no longer" in done["error"]
    assert agents.get(a["id"])["status"] == "paused"
    assert any("radio-nightly" in str(s) for s in world["sent"])
    # feature off: the 3.1 behaviour (no owner check)
    monkeypatch.setenv("SOKKAN_FEATURE_REVOCATION", "0")
    agents.set_next_run(a["id"], None, status="active")
    done, started = _run_once(a)
    assert started
