"""4.0 — from « installed » to « the team's first validated deliverable » (backend/onboarding.py).

The administrator's first launch (six skippable, resumable steps, a model tested before it is
saved), a person's first sign-in (position, tour, first work on an example document), and the
administration of every day (Team, Usage, invitations, deactivation) — none of it through
`.env`."""
import time

import pytest
from fastapi.testclient import TestClient

OWNER = "owner@localhost"


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """A fresh appliance-like instance: local auth with an owner token, multi_project and the
    document drop on, no project but `default`, nothing done yet."""
    import audit
    import auth
    import board
    import iam
    import instance
    import llm
    import projects
    import revocation
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_DOCUMENTS", "1")
    monkeypatch.delenv("SOKKAN_SHIP_URL", raising=False)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(projects, "DB", tmp_path / "projects.db")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    monkeypatch.setattr(instance, "STORE", tmp_path / "instance.json")
    monkeypatch.setattr(revocation, "DB", tmp_path / "identity.db", raising=False)
    iam.init(force=True)
    projects.init(force=True)
    board.init(force=True)
    monkeypatch.setattr(auth, "MODE", "local")
    monkeypatch.setattr(auth, "LOCAL_TOKEN", "owner-token-0123456789")
    monkeypatch.setattr(auth, "OWNER_EMAIL", OWNER)
    iam.upsert_user(OWNER, "owner", "Owner")
    import app as a
    a._login_fails.clear()
    # after the app's import: its cookie flag (`Secure`) is read once, at import
    monkeypatch.setenv("SOKKAN_PUBLIC_URL", "https://sokkan.example")
    import onboarding
    onboarding._invite_fails.clear()
    return a


def _client(a, code: str) -> TestClient:
    c = TestClient(a.app, base_url="https://testserver")
    r = c.post("/api/auth/local", json={"token": code})
    assert r.status_code == 200, r.text
    return c


def _owner(a) -> TestClient:
    return _client(a, "owner-token-0123456789")


# ---- the first launch -------------------------------------------------------------------------
def test_a_fresh_instance_sends_its_administrator_through_the_six_steps(box):
    c = _owner(box)
    ob = c.get("/api/onboarding").json()
    assert ob["admin"] is True
    assert ob["setup"]["needed"] is True and ob["setup"]["next"] == "org"
    assert [s["id"] for s in ob["setup"]["steps"]] == ["org", "model", "projects", "people", "documents", "first_work"]
    assert ob["setup"]["started_at"] and ob["model"]["kind"] == "none"
    # the organisation, without .env
    r = c.put("/api/onboarding/org", json={"org_name": "Atelier Exemple", "lang": "fr"})
    assert r.status_code == 200 and r.json() == {"org_name": "Atelier Exemple", "lang": "fr"}
    assert c.get("/api/instance").json()["org_name"] == "Atelier Exemple"
    st = c.put("/api/onboarding/setup", json={"step": "org", "state": "done"}).json()
    assert st["next"] == "model"
    # every step can be skipped; the next visit resumes at the first one still to do
    c.put("/api/onboarding/setup", json={"step": "model", "state": "skipped"})
    ob = c.get("/api/onboarding").json()
    assert ob["setup"]["next"] == "projects" and ob["setup"]["needed"] is True
    for s in ("projects", "people", "documents", "first_work"):
        c.put("/api/onboarding/setup", json={"step": s, "state": "skipped"})
    ob = c.get("/api/onboarding").json()
    assert ob["setup"]["finished"] is True and ob["setup"]["needed"] is False
    # reopened on purpose: back to the first step
    st = c.put("/api/onboarding/setup", json={"reopen": True}).json()
    assert st["next"] == "org" and not st["finished"]


def test_an_upgraded_instance_with_works_is_not_sent_through_a_first_launch(box):
    import board
    board.add_sdk_session("a" * 32, "x", title="old work", project="default")
    ob = _owner(box).get("/api/onboarding").json()
    assert ob["setup"]["finished"] is True and ob["setup"]["preexisting"] is True and not ob["setup"]["needed"]


def test_only_an_instance_administrator_reaches_the_setup_and_the_team(box):
    import localaccounts
    import projects
    projects.create("clients", "Clients", created_by=OWNER)
    code = localaccounts.add("ana@example.org", "Ana", project="clients", project_role="admin")["code"]
    ana = _client(box, code)
    ob = ana.get("/api/onboarding").json()
    assert ob["admin"] is False and "setup" not in ob and ob["me"]["needs_welcome"] is True
    for method, url, body in (("put", "/api/onboarding/setup", {"step": "org"}), ("put", "/api/onboarding/org", {"org_name": "x"}),
                              ("post", "/api/onboarding/model/test", {"kind": "local"}), ("get", "/api/team", None),
                              ("post", "/api/team", {"email": "z@example.org", "projects": ["clients"]}),
                              ("get", "/api/team/usage", None), ("post", "/api/onboarding/projects", {"name": "Xx"})):
        r = getattr(ana, method)(url, **({"json": body} if body is not None else {}),
                                 headers={"x-sokkan-project": "clients"})
        assert r.status_code == 403, (url, r.status_code)


# ---- the model --------------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, body="{}", data=None):
        self.status_code, self.text, self._d = status, body, data

    def json(self):
        return self._d or {}


class _Http:
    def __init__(self, status=200, body="{}", exc=None, health=None):
        self.status, self.body, self.exc, self.health, self.calls = status, body, exc, health, []

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        if self.exc:
            raise self.exc
        return _Resp(self.status, self.body)

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        if self.exc:
            raise self.exc
        return _Resp(200, data=self.health or {})

    def close(self):
        pass


@pytest.mark.parametrize("status,body,code", [
    (200, "{}", "ok"), (401, '{"error":{"type":"authentication_error"}}', "auth"),
    (402, '{"error":{"type":"billing_error"}}', "quota_exhausted"), (429, "{}", "rate_limited"),
    (404, '{"error":{"message":"model: not_found"}}', "model"),
])
def test_the_connection_test_says_what_happened_in_words(status, body, code):
    import onboarding
    h = _Http(status, body)
    r = onboarding.test_model("sokkan", key="sik_test_0123456789", lang="fr", client=h)
    assert r["code"] == code and r["ok"] is (code == "ok")
    assert r["title"] and r["message"]
    assert "sik_test" not in str(r)                       # never the key back
    (_, url, kw), = h.calls
    assert url.endswith("/v1/messages") and kw["json"]["max_tokens"] == 1


def test_an_unreachable_service_and_a_missing_value_are_said(monkeypatch):
    import onboarding
    r = onboarding.test_model("sokkan", key="sik_x_0123456789", lang="en", client=_Http(exc=OSError("refused")))
    assert r["code"] == "unreachable" and "reach" in r["title"].lower()
    assert onboarding.test_model("sokkan", key="", client=_Http())["code"] == "bad_input"
    assert onboarding.test_model("key", key="abc", base_url="ftp://x", client=_Http())["code"] == "bad_input"
    monkeypatch.delenv("SOKKAN_SHIP_URL", raising=False)
    assert onboarding.test_model("local", client=_Http())["code"] == "no_local"
    monkeypatch.setenv("SOKKAN_SHIP_URL", "http://ship:8004")
    assert onboarding.test_model("local", client=_Http(health={"ship": True}))["ok"] is True
    assert onboarding.test_model("local", client=_Http(health={"ship": False}))["code"] == "unreachable"


def test_a_model_is_saved_only_after_its_test_passes(box, monkeypatch):
    import llm
    import onboarding
    c = _owner(box)
    calls = {"n": 0}
    real = onboarding.test_model

    def fake(kind, key="", base_url="", model="", lang="fr", client=None):
        calls["n"] += 1
        return real(kind, key, base_url, model, lang, client=_Http(401 if "bad" in key else 200))
    monkeypatch.setattr(onboarding, "test_model", fake)
    r = c.post("/api/onboarding/model", json={"kind": "sokkan", "key": "sik_bad_0123456789"}).json()
    assert r["saved"] is False and r["test"]["code"] == "auth"
    assert not llm.CONFIG.exists()
    r = c.post("/api/onboarding/model", json={"kind": "sokkan", "key": "sik_good_0123456789"}).json()
    assert r["saved"] is True and r["model"]["kind"] == "sokkan"
    cfg = llm.load()
    assert cfg["mode"] == "custom" and cfg["auth_token"] == "sik_good_0123456789"
    assert "sik_good" not in llm.CONFIG.read_text()          # sealed on disk
    r = c.post("/api/onboarding/model", json={"kind": "key", "key": "sk-ant-good-0123456789"}).json()
    assert r["saved"] and llm.load()["mode"] == "byok" and r["model"]["kind"] == "key"
    assert c.get("/api/onboarding").json()["model"]["configured"] is True


# ---- projects ---------------------------------------------------------------------------------
def test_a_project_is_created_classified_and_its_creator_is_in_it(box):
    import projects
    c = _owner(box)
    r = c.post("/api/onboarding/projects", json={"name": "Ressources humaines", "level": "confidential"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p == {"slug": "ressources-humaines", "name": "Ressources humaines", "level": "confidential"}
    assert projects.get("ressources-humaines")["level"] == 3
    assert projects.effective_role({"email": OWNER, "role": "owner"}, "ressources-humaines") == "admin"
    # same name again: a second project, never an overwrite
    assert c.post("/api/onboarding/projects", json={"name": "Ressources humaines", "level": "team"}).json()["slug"] == "ressources-humaines-2"
    assert c.post("/api/onboarding/projects", json={"name": "x", "level": "team"}).status_code == 400
    assert c.post("/api/onboarding/projects", json={"name": "Abc", "level": "secret"}).status_code == 400
    listed = {x["slug"]: x for x in c.get("/api/onboarding/projects").json()["projects"]}
    assert listed["ressources-humaines"]["level"] == "confidential" and listed["ressources-humaines"]["people"] == 1


# ---- people -----------------------------------------------------------------------------------
def test_an_access_code_signs_the_person_in_with_their_own_projects_only(box):
    c = _owner(box)
    c.post("/api/onboarding/projects", json={"name": "Clients", "level": "team"})
    c.post("/api/onboarding/projects", json={"name": "Direction", "level": "restricted"})
    r = c.post("/api/team", json={"email": "Ana@Example.org", "name": "Ana", "projects": ["clients"], "via": "code"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["email"] == "ana@example.org" and out["code"].startswith("sk-local-") and "link" not in out
    ana = _client(box, out["code"])
    slugs = {p["slug"] for p in ana.get("/api/projects").json()["projects"]}
    assert "clients" in slugs and "direction" not in slugs
    assert ana.get("/api/me", headers={"x-sokkan-project": "clients"}).json()["project_role"] == "dev"
    # a member needs a project; an e-mail must be one
    assert c.post("/api/team", json={"email": "ben@example.org", "projects": []}).status_code == 400
    assert c.post("/api/team", json={"email": "not-an-email", "projects": ["clients"]}).status_code == 400
    # an administrator needs none
    adm = c.post("/api/team", json={"email": "chef@example.org", "admin": True, "via": "code"}).json()
    assert _client(box, adm["code"]).get("/api/onboarding").json()["admin"] is True


def test_an_invitation_link_signs_in_once_and_hands_over_the_personal_code(box):
    import audit
    import onboarding
    c = _owner(box)
    c.post("/api/onboarding/projects", json={"name": "Clients", "level": "team"})
    out = c.post("/api/team", json={"email": "ben@example.org", "name": "Ben", "projects": ["clients"], "via": "link"}).json()
    assert "code" not in out and out["link"].startswith("https://sokkan.example/?invite=")
    token = out["link"].split("invite=")[1]
    assert token not in onboarding.store_path().read_text()     # only its hash is kept
    team = {p["email"]: p for p in c.get("/api/team").json()["people"]}
    assert team["ben@example.org"]["invite_pending"] is True
    guest = TestClient(box.app, base_url="https://testserver")
    r = guest.post("/api/auth/invite", json={"token": token})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert code.startswith("sk-local-") and r.json()["name"] == "Ben"
    assert guest.get("/api/me").json()["email"] == "ben@example.org"          # signed in by the link
    assert _client(box, code).get("/api/me").json()["email"] == "ben@example.org"   # and the code works
    # single use
    assert TestClient(box.app).post("/api/auth/invite", json={"token": token}).status_code == 404
    assert any(e["action"] == "auth.login" and e["resource"] == "invite" for e in audit.recent(20))
    assert {p["email"]: p for p in c.get("/api/team").json()["people"]}["ben@example.org"]["invite_pending"] is False


def test_an_expired_invitation_says_so(box, monkeypatch):
    import onboarding
    c = _owner(box)
    c.post("/api/onboarding/projects", json={"name": "Clients", "level": "team"})
    out = c.post("/api/team", json={"email": "cleo@example.org", "projects": ["clients"], "via": "link"}).json()
    token = out["link"].split("invite=")[1]
    later = time.time() + onboarding.INVITE_TTL_S + 10
    monkeypatch.setattr(onboarding.time, "time", lambda: later)
    r = TestClient(box.app).post("/api/auth/invite", json={"token": token})
    assert r.status_code == 410 and r.json()["detail"]["code"] == "expired"


def test_deactivate_reactivate_and_a_new_code(box):
    c = _owner(box)
    c.post("/api/onboarding/projects", json={"name": "Clients", "level": "team"})
    code = c.post("/api/team", json={"email": "dan@example.org", "projects": ["clients"]}).json()["code"]
    dan = _client(box, code)
    assert c.post("/api/team/dan@example.org/disable").status_code == 200
    assert dan.get("/api/me").status_code in (401, 403)                  # cut at once
    assert TestClient(box.app).post("/api/auth/local", json={"token": code}).status_code == 401
    assert {p["email"]: p for p in c.get("/api/team").json()["people"]}["dan@example.org"]["disabled"] is True
    assert c.post("/api/team/dan@example.org/enable").status_code == 200
    new = c.post("/api/team/dan@example.org/code").json()["code"]
    assert new != code
    assert TestClient(box.app).post("/api/auth/local", json={"token": code}).status_code == 401
    _client(box, new)
    # nobody deactivates the owner or themself
    assert c.post(f"/api/team/{OWNER}/disable").status_code == 400


def test_the_team_page_lists_everyone_once_and_sets_their_projects(box):
    c = _owner(box)
    for n in ("Clients", "Direction"):
        c.post("/api/onboarding/projects", json={"name": n, "level": "team"})
    c.post("/api/team", json={"email": "eve@example.org", "name": "Eve", "projects": ["clients"]})
    c.put("/api/onboarding/me", json={"role_id": "finance"})
    people = c.get("/api/team").json()["people"]
    assert [p["email"] for p in people].count("eve@example.org") == 1
    assert people[0]["owner"] is True and people[0]["position"]                      # the owner first, with a position
    eve = next(p for p in people if p["email"] == "eve@example.org")
    assert eve["has_code"] and [x["slug"] for x in eve["projects"]] == ["clients"] and eve["last_login"] is None
    r = c.patch("/api/team/eve@example.org", json={"projects": {"direction": "maintainer", "clients": ""}})
    assert [(x["slug"], x["role"]) for x in r.json()["person"]["projects"]] == [("direction", "maintainer")]
    r = c.patch("/api/team/eve@example.org", json={"admin": True})
    assert r.json()["person"]["admin"] is True


# ---- a person's first sign-in -----------------------------------------------------------------
def test_a_person_picks_a_position_and_gets_an_example_document_once(box):
    import documents
    c = _owner(box)
    c.post("/api/onboarding/projects", json={"name": "Clients", "level": "team"})
    code = c.post("/api/team", json={"email": "fay@example.org", "projects": ["clients"]}).json()["code"]
    fay = _client(box, code)
    fay.headers["x-sokkan-project"] = "clients"
    roles = fay.get("/api/onboarding/roles?lang=fr").json()
    assert roles["source"] == "starter" and {"finance", "sales", "other"} <= {r["id"] for r in roles["roles"]}
    assert all(r["label"] and r["description"] for r in roles["roles"])
    assert fay.put("/api/onboarding/me", json={"role_id": "sales"}).json()["role_id"] == "sales"
    s1 = fay.post("/api/onboarding/sample", json={"role_id": "sales", "lang": "fr"}).json()
    assert s1["ask"] and s1["document"]["name"].endswith(".md")
    s2 = fay.post("/api/onboarding/sample", json={"role_id": "sales", "lang": "fr"}).json()
    assert s2["document"]["id"] == s1["document"]["id"]                  # once per person and project
    assert len(documents.list_project("clients")) == 1
    me = fay.put("/api/onboarding/me", json={"welcomed": True, "tour_done": True}).json()
    assert me["welcomed"] and me["tour_done"]
    assert fay.get("/api/onboarding").json()["me"]["needs_welcome"] is False


def test_the_catalogue_of_positions_wins_when_it_is_installed(monkeypatch):
    import sys
    import types
    import onboarding
    fake = types.ModuleType("roles_catalog")
    fake.catalog = lambda lang: [{"id": "jurist", "label": "Juriste", "description": "Contrats"}]
    fake.sample = lambda rid, lang: {"ask": "Relis ce contrat.", "doc": ("Contrat.md", "# Contrat")}
    monkeypatch.setitem(sys.modules, "roles_catalog", fake)
    assert onboarding.roles("fr") == {"source": "catalog", "roles": [
        {"id": "jurist", "label": "Juriste", "description": "Contrats", "sample": True}]}
    assert onboarding.sample_for("jurist", "fr") == {"ask": "Relis ce contrat.", "name": "Contrat.md", "text": "# Contrat"}


def test_every_starter_position_has_both_languages_and_no_real_name():
    import onboarding_samples as S
    assert set(S.ORDER) == set(S.STARTER)
    for rid, langs in S.STARTER.items():
        for lang in ("fr", "en"):
            r = langs[lang]
            assert r["label"] and r["description"] and r["ask"] and r["doc"][0].endswith(".md") and len(r["doc"][1]) > 100
            assert "@" not in r["doc"][1]


# ---- usage, budget, time to the first validated deliverable ------------------------------------
def test_usage_per_person_and_project_with_the_instance_ceiling_alert(box, monkeypatch):
    import board
    import usage
    from datetime import datetime
    board.add_sdk_session("s1" * 16, "x", title="w1", project="default", owner="ana@example.org")
    board.add_sdk_session("s2" * 16, "x", title="w2", project="default", owner="ben@example.org")
    today = datetime.now(usage.TZ).strftime("%Y-%m-%d")

    def row(path, cost, turns):
        return {"path": path, "day": today, "model": "m", "turns": turns, "in_tokens": 10, "out_tokens": 5, "cache_read": 0,
                "cw_5m": 0, "cw_1h": 0, "cost_in": 0.0, "cost_out": 0.0, "cost_cr": 0.0, "cost_cw": 0.0, "priced": 1,
                "api_equiv": cost, "billed": cost, "metered": cost, "source": "sokkan", "project": "default", "basis": "api"}
    files = {"p1": {"session_id": "s1" * 16, "session": {}}, "p2": {"session_id": "s2" * 16, "session": {}}}
    monkeypatch.setattr(usage, "_scan", lambda project: {"rows": [row("p1", 1.5, 3), row("p2", 0.25, 1)], "files": files,
                                                          "billing": {}, "bases": {}, "other": {}})
    c = _owner(box)
    c.put("/api/team/budget", json={"project": "", "day": 2})
    u = c.get("/api/team/usage?days=7").json()
    assert [(p["email"], p["cost"], p["turns"]) for p in u["people"]] == [("ana@example.org", 1.5, 3), ("ben@example.org", 0.25, 1)]
    assert u["total"]["cost"] == 1.75 and u["today_total"] == 1.75
    assert u["alerts"] == [{"project": "", "name": "", "state": "warn", "pct_day": 87.5, "currency": "USD"}]
    assert any(p["slug"] == "default" and p["cost"] == 1.75 for p in u["projects"])


def test_the_time_to_the_first_validated_deliverable_is_measured(box):
    import audit
    c = _owner(box)
    started = c.get("/api/onboarding").json()["setup"]["started_at"]
    assert c.get("/api/onboarding").json()["ttfv"]["seconds"] is None
    audit.log("ana@example.org", "deliverable.validate", "x" * 32, "Synthèse.md · sha256 0", project="default")
    t = c.get("/api/onboarding").json()["ttfv"]
    assert t["started_at"] == started and t["seconds"] is not None and t["seconds"] >= 0


# ---- errors a person can act on -------------------------------------------------------------
@pytest.mark.parametrize("ev,code", [
    ({"type": "error", "message": "ConnectionError: kernel refused the session (503): {\"code\":\"sandbox_unavailable\"}"}, "sandbox_unavailable"),
    ({"type": "error", "code": "quota_exhausted", "message": "Crédit épuisé"}, "model_quota"),
    ({"type": "error", "code": "rate_limited", "message": "x"}, "model_rate"),
    ({"type": "error", "message": "API Error: Connection error."}, "model_unreachable"),
    ({"type": "error", "message": "API Error: 401 {}"}, "model_auth"),
    ({"type": "error", "message": "Session is already running"}, "busy"),
])
def test_engine_errors_become_a_notice_in_words(ev, code):
    import plain
    v = plain.View()
    out = v.feed(ev)
    assert out and out[0]["type"] == "notice" and out[0]["code"] == code, out
