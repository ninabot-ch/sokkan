"""Feature `study_mode` (backend/study.py, study_api.py, study_export.py), through the real
request path (middleware → projectgate → route); only the identity is chosen.

The gateway answers used here are the two real shapes: what infer.sokkan.ch serves today
(`LEGACY_USAGE`, the keys of its `GET /usage` handler — no study block) and the contract of
the study block (`STUDY_USAGE`). Nothing is real data: people are `…@x.example`, every
secret and every sentence is made up.

People:
  * camille@x.example, noah@x.example — volunteers (instance devs);
  * lea@x.example                     — a colleague who never consents;
  * admin@x.example                   — instance admin.
"""
import asyncio
import datetime
import io
import json
import sqlite3
import time
import zipfile

import pytest

CAMILLE, NOAH, LEA, ADMIN = ("camille@x.example", "noah@x.example", "lea@x.example",
                             "admin@x.example")
S_A, S_B, S_N, S_L = "a" * 32, "b" * 32, "c" * 32, "d" * 32
VAULT_SECRET = "hunter2-VAULT-value-9f8e7d6c5b4a"
PASTED_KEY = "sk-live-0123456789abcdefABCDEF"

# what the gateway serves today (sokkan-cloud infer/gateway.py, GET /usage): no study block
LEGACY_USAGE = {"client": "acme", "day": "2027-01-12", "used_today": 1200,
                "daily_quota_tokens": 6000000, "used_month": 45000,
                "monthly_quota_tokens": 120000000, "balance_centimes": 0,
                "spent_today_centimes": 0, "spent_month_centimes": 0, "per_user": []}
# the contract of the study block
STUDY_USAGE = {**LEGACY_USAGE,
               "study": {"active": True, "study_id": "STUDY-2027-A",
                         "expires_at": "2099-04-30T23:59:59Z", "audit_required": True},
               "limits": {"tokens_day": 6000000, "tokens_month": 120000000,
                          "deep_per_user_day": 20},
               "used": {"tokens_day": 2280000, "tokens_month": 37200000}}

# sentences that must never leave in a metadata export — one per text field that carries
# what a person typed or what a tool received
LEAK = {
    "session_title": "Formation Excel pour la fiduciaire Dubois",
    "session_prompt": "Prepare le plan de formation du client Dubois SA",
    "turn_prompt": "ajoute un module tableaux croises pour Mme Rochat",
    "tool_title": "/workspace/out/plan_dubois_v2.md",
    "tool_input": "contenu confidentiel du plan Dubois",
    "deny_message": "non, le tarif de Dubois est faux",
    "audit_detail": "prompt envoye: budget Dubois 12000 CHF",
    "recall_query": "tarifs negocies avec la fiduciaire Dubois",
    "note_name": "client-dubois-tarifs-2026",
    "project_slug": "camille-dubois",
}


class _Store:
    """The two logs of the memory store this export reads, with the columns the real
    Store.recall_log / Store.access_log / Store.get_note return (memory/core/store.py)."""

    def __init__(self):
        self.recalls, self.accesses, self.notes = [], [], {}

    def recall_log(self, *, session_id=None, note=None, limit=200, projects=None):
        return [dict(r) for r in self.recalls if r["session_id"] == session_id][:limit]

    def access_log(self, *, projects=None, actor=None, session_id=None, note=None, since=None,
                   limit=500):
        return [dict(r) for r in self.accesses if r["session_id"] == session_id][:limit]

    def get_note(self, name, project="default"):
        return self.notes.get((project, name))


class _Note:
    def __init__(self, modified):
        self.modified = modified


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import agentchat
    import app as a
    import audit
    import auth
    import board
    import iam
    import llm
    import projects
    import store_backend
    import study
    import vault

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_FEATURE_STUDY_MODE", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    for v in ("SOKKAN_EDITION", "SOKKAN_STUDY_ID", "SOKKAN_STUDY_EXPIRES_AT",
              "SOKKAN_STUDY_AUDIT_REQUIRED", "SOKKAN_INFER_BASE_URL", "SOKKAN_INFER_TOKEN",
              "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    for e, r in ((CAMILLE, "dev"), (NOAH, "dev"), (LEA, "dev"), (ADMIN, "admin")):
        iam.upsert_user(e, r)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(study, "DB", tmp_path / "study.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    monkeypatch.setattr(agentchat, "_registry", {})
    monkeypatch.setenv("SOKKAN_PROJECT_DIR", str(tmp_path / "transcripts"))
    study.reset_cache()
    projects.create(LEAK["project_slug"], "Camille", created_by=ADMIN)
    projects.grant(LEAK["project_slug"], "user", CAMILLE, "dev")
    board.add_sdk_session(S_A, "work", title=LEAK["session_title"],
                          prompt=LEAK["session_prompt"], project=LEAK["project_slug"],
                          owner=CAMILLE)
    board.add_sdk_session(S_B, "work", title="second task", project="default", owner=CAMILLE)
    board.add_sdk_session(S_N, "work", title="noah's task", project="default", owner=NOAH)
    board.add_sdk_session(S_L, "work", title="lea's task", project="default", owner=LEA)

    gw = {"usage": STUDY_USAGE, "calls": 0, "headers": []}

    def usage(headers=None):
        if headers:                      # a person's own consumption (study.my_usage)
            gw["headers"].append(dict(headers))
            u = gw["usage"]
            return u if isinstance(u, dict) else None
        gw["calls"] += 1
        if isinstance(gw["usage"], Exception):
            raise gw["usage"]
        return gw["usage"]
    monkeypatch.setattr(llm, "usage", usage)
    store = _Store()
    monkeypatch.setattr(store_backend, "enabled", lambda: True)
    monkeypatch.setattr(store_backend, "get_store", lambda: store)

    who = {"email": CAMILLE}

    def resolve(request):
        if who["email"] is None:
            from fastapi import HTTPException
            raise HTTPException(401, "not signed in")
        return who["email"]
    monkeypatch.setattr(auth, "resolve_email", resolve)
    c = TestClient(a.app)

    def as_(email, project=None):
        who["email"] = email
        c.headers.pop("x-sokkan-project", None)
        c.headers.pop("authorization", None)
        if project:
            c.headers["x-sokkan-project"] = project
        return c

    def set_usage(u):
        gw["usage"] = u
        study.reset_cache()

    return {"as": as_, "tmp": tmp_path, "gw": gw, "set_usage": set_usage, "store": store}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _consent(world, email, content=()):
    r = world["as"](email).post("/api/study/consent", json={"content_scenarios": list(content)})
    assert r.status_code == 200, r.text
    return r.json()


# ================================ off by default =========================================

def test_off_by_default_nothing_changes(world, monkeypatch):
    import features
    import llm
    import study
    monkeypatch.delenv("SOKKAN_FEATURE_STUDY_MODE")
    for ed in ("community", "enterprise"):
        monkeypatch.setenv("SOKKAN_EDITION", ed)
        assert features.enabled("study_mode") is False
    monkeypatch.delenv("SOKKAN_EDITION")
    f = features.BY_ID["study_mode"]
    assert f.doc == "docs/enterprise/STUDY-MODE.md" and f.status == "beta"
    c = world["as"](CAMILLE)
    assert c.get("/api/study/me").json() == {"enabled": False, "active": False}
    for method, path in (("post", "/api/study/consent"), ("get", "/api/study/sessions"),
                         ("get", f"/api/sessions/{S_B}/study"), ("get", "/api/admin/study"),
                         ("get", "/api/study/export")):
        kw = {"json": {}} if method == "post" else {}
        assert getattr(world["as"](ADMIN), method)(path, **kw).status_code == 404, path
    # the gateway receives what it received before; nobody is given a pseudonym; no gate
    llm.save({"mode": "included", "base_url": "https://infer.example", "auth_token": "sik_x"})
    assert llm.session_env(CAMILLE)["ANTHROPIC_CUSTOM_HEADERS"] == f"x-sokkan-user: {CAMILLE}"
    assert llm.identity_headers(CAMILLE, "assistant:") == {"x-sokkan-user": f"assistant:{CAMILLE}"}
    assert study.gate(CAMILLE) is None and study.turn_start(S_B, CAMILLE, "hello") is None
    assert study.correspondence() == [] and world["gw"]["calls"] == 0


def test_the_feature_is_in_the_registry_and_in_the_generated_doc(world):
    from pathlib import Path
    reg = world["as"](ADMIN).get("/api/features").json()["registry"]
    item = next(f for f in reg["items"] if f["id"] == "study_mode")
    assert item["enabled"] is True and item["requires"] == []
    assert item["defaults"] == {"community": False, "enterprise": False}
    assert {"SOKKAN_STUDY_ID", "SOKKAN_STUDY_EXPIRES_AT"} <= set(item["config"])
    doc = (Path(__file__).resolve().parent.parent / "docs/enterprise/FEATURES.md").read_text()
    assert "study_mode" in doc and "SOKKAN_FEATURE_STUDY_MODE" in doc


# ================================ state read from the gateway ============================

def test_state_is_read_from_the_gateway_usage_call(world):
    me = world["as"](CAMILLE).get("/api/study/me").json()
    assert (me["enabled"], me["active"], me["study_id"], me["source"]) == (
        True, True, "STUDY-2027-A", "gateway")
    assert me["expires_at"] == "2099-04-30T23:59:59Z" and me["audit_required"] is True
    assert me["limits"] == {"tokens_day": 6000000, "tokens_month": 120000000,
                            "deep_per_user_day": 20, "user_tokens_day": None}
    assert me["notice"] is False and me["mine"] is None
    assert me["used"] == {"tokens_day": 2280000, "tokens_month": 37200000}
    assert me["days_left"] > 365 and me["pseudonym"] == "P1" and me["blocked"] is True
    # asked at most once a minute, whatever the number of pages open
    for _ in range(5):
        world["as"](CAMILLE).get("/api/study/me")
    assert world["gw"]["calls"] == 1


def test_a_gateway_without_the_study_block_is_tolerated(world):
    import study
    world["set_usage"](LEGACY_USAGE)
    me = world["as"](CAMILLE).get("/api/study/me").json()
    assert (me["enabled"], me["active"], me["study_id"], me["source"]) == (True, False, "", "none")
    # ceilings and consumption still come from the fields the gateway has always served
    assert me["limits"]["tokens_day"] == 6000000 and me["used"]["tokens_day"] == 1200
    assert me["limits"]["deep_per_user_day"] is None and me["pseudonym"] == ""
    assert study.gate(CAMILLE) is None
    for odd in ({}, {"study": None}, {"study": "yes"}, {"study": {}},
                {"study": {"active": True}},                      # active without a study id
                {"study": {"active": True, "study_id": "x y/../z"}},
                {"study": {"active": False, "study_id": "S", "expires_at": None}},
                {"limits": None, "used": "n/a", "study": {"active": True, "study_id": "S",
                                                           "expires_at": "not a date"}}):
        world["set_usage"](odd)
        st = study.state()
        assert st["enabled"] is True and st["active"] == (odd.get("study") == {
            "active": True, "study_id": "S", "expires_at": "not a date"}), odd


def test_an_unreachable_gateway_keeps_the_last_known_state_and_its_end_date(world):
    import study
    assert study.state()["active"] is True
    for down in (None, RuntimeError("gateway down")):
        world["set_usage"](down)
        st = study.state()
        assert (st["active"], st["study_id"], st["stale"], st["source"]) == (
            True, "STUDY-2027-A", True, "gateway")
    # a study whose end date has passed is over, even when the gateway still says active
    past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat()
    world["set_usage"]({**STUDY_USAGE, "study": {**STUDY_USAGE["study"], "expires_at": past}})
    st = study.state()
    assert (st["active"], st["expired"], st["days_left"]) == (False, True, 0)
    assert study.gate(CAMILLE) is None
    world["set_usage"]({**STUDY_USAGE, "study": {**STUDY_USAGE["study"], "active": False}})
    assert study.state()["active"] is False


def test_a_study_can_be_declared_locally_when_the_gateway_serves_none(world, monkeypatch):
    import study
    world["set_usage"](None)                      # not on managed inference
    monkeypatch.setenv("SOKKAN_STUDY_ID", "LOCAL-1")
    monkeypatch.setenv("SOKKAN_STUDY_EXPIRES_AT", "2099-01-01")
    st = study.state(refresh=True)
    assert (st["active"], st["study_id"], st["source"], st["audit_required"]) == (
        True, "LOCAL-1", "local", True)
    world["set_usage"](STUDY_USAGE)               # the gateway's word wins when it has one
    assert study.state()["study_id"] == "STUDY-2027-A"


# the answer of the gateway as delivered (sokkan-cloud infer/pilote-etude.md § 4, GET /usage)
FINAL_USAGE = {**LEGACY_USAGE,
               "study": {"active": True, "study_id": "study-2027-a",
                         "expires_at": "2099-04-30T23:59:59Z", "audit_required": True,
                         "expired": False, "days_left": 9, "notice": True},
               "free": True,
               "limits": {"tokens_day": 6000000, "tokens_month": 120000000,
                          "deep_per_user_day": 20, "user_tokens_day": 0},
               "used": {"tokens_day": 4960, "tokens_month": 4960, "user_tokens_day": 4960,
                        "deep_user_day": 2}}
# its refusals, word for word (infer/gateway.py `_refuse`), as Claude Code shows them
REFUSALS = {
    "study_expired": "API Error: 410 The free inference period for this environment ended on "
                     "2027-04-30. Nothing was sent to a model. Contact your SOKKAN administrator "
                     "to extend it or to switch to a paid plan (same token).",
    "study_mode_required": "API Error: 412 Free inference for this environment is part of study "
                           "'study-2027-a' and is only served while the instance runs in "
                           "study mode (audited). Turn study mode on in the instance settings — "
                           "it then sends the header 'x-sokkan-study: study-2027-a'. Nothing "
                           "was sent to a model.",
    "user_daily_cap": "API Error: 429 personal daily inference cap reached for this user in this "
                      "environment — resets at midnight UTC (other users are not affected)",
}


def test_the_gateway_as_delivered_notice_personal_ceilings_and_zero_means_none(world):
    import study
    world["set_usage"](FINAL_USAGE)
    me = world["as"](CAMILLE).get("/api/study/me").json()
    assert (me["active"], me["study_id"], me["notice"], me["source"]) == (
        True, "study-2027-a", True, "gateway")
    assert me["limits"] == {"tokens_day": 6000000, "tokens_month": 120000000,
                            "deep_per_user_day": 20, "user_tokens_day": None}   # 0 = no ceiling
    assert me["used"] == {"tokens_day": 4960, "tokens_month": 4960}
    # my own consumption: asked with MY pseudonym and the study, never my address
    assert me["mine"] == {"tokens_day": 4960, "deep_day": 2}
    assert world["gw"]["headers"] == [{"x-sokkan-study": "study-2027-a", "x-sokkan-user": "P1"}]
    world["as"](CAMILLE).get("/api/study/me")
    assert len(world["gw"]["headers"]) == 1                    # once a minute per person
    # the gateway says the free period is over: no study, whatever `active` says
    world["set_usage"]({**FINAL_USAGE, "study": {**FINAL_USAGE["study"], "expired": True}})
    st = study.state()
    assert (st["active"], st["notice"]) == (False, False)
    world["set_usage"]({**FINAL_USAGE, "study": {**FINAL_USAGE["study"], "expires_at": None,
                                                  "notice": False}})
    st = study.state()
    assert (st["active"], st["expires_at"], st["days_left"], st["notice"]) == (True, "", None, False)
    # an older gateway (no personal figures): nothing shown, nothing invented
    world["set_usage"](STUDY_USAGE)
    assert world["as"](CAMILLE).get("/api/study/me").json()["mine"] is None


def test_usage_sends_the_identity_headers_over_http(world, monkeypatch):
    """The real llm.usage against a local HTTP server: the headers arrive with the token."""
    import http.server
    import threading

    import llm
    monkeypatch.undo()                     # the real llm.usage, not the fixture's
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.update({k.lower(): v for k, v in self.headers.items()})
            body = json.dumps(FINAL_USAGE).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    cfg = world["tmp"] / "llm-http.json"
    cfg.write_text(json.dumps({"mode": "included", "auth_token": "sik_test",
                               "base_url": f"http://127.0.0.1:{srv.server_address[1]}"}))
    old = llm.CONFIG
    llm.CONFIG = cfg
    try:
        u = llm.usage(headers={"x-sokkan-user": "P1", "x-sokkan-study": "study-2027-a"})
    finally:
        llm.CONFIG = old
        srv.server_close()
    assert u["study"]["study_id"] == "study-2027-a"
    assert (seen["x-sokkan-user"], seen["x-sokkan-study"], seen["x-api-key"]) == (
        "P1", "study-2027-a", "sik_test")


def test_a_refusal_of_the_gateway_is_said_in_words_in_both_languages(world):
    import agentchat
    import study
    from claude_agent_sdk import ResultMessage
    said = {code: study.explain_refusal(text) for code, text in REFUSALS.items()}
    assert "période gratuite" in said["study_expired"] and "free period" in said["study_expired"]
    assert "mode étude" in said["study_mode_required"] and "study mode" in said["study_mode_required"]
    assert "plafond personnel" in said["user_daily_cap"] and "personal ceiling" in said["user_daily_cap"]
    assert len(set(said.values())) == 3
    # the code alone (error.code, header x-sokkan-error) is enough too
    assert study.explain_refusal('{"error": {"code": "study_expired"}}') == said["study_expired"]
    for other in ("", "API Error: 429 daily token quota reached", "API Error: 402 wallet empty",
                  "Failed to authenticate. API Error: 401", "RuntimeError: boom"):
        assert study.explain_refusal(other) is None
    # through a session: the person reads the sentence, once, next to the raw error
    _consent(world, CAMILLE)

    class Client:
        async def query(self, text):
            pass

        async def receive_response(self):
            yield ResultMessage(subtype="error_during_execution", duration_ms=1,
                                duration_api_ms=1, is_error=True, num_turns=1, session_id="cs",
                                total_cost_usd=0.0, usage={}, result=REFUSALS["study_expired"])
    sess = agentchat.AgentSession(S_B, cwd="/tmp", user=CAMILLE)
    sess.client = Client()
    _run(sess.handle_user("bonjour"))
    errors = [e["message"] for e in sess.events if e["type"] == "error"]
    assert errors == [said["study_expired"]]
    assert any(e["type"] == "result" and e["is_error"] for e in sess.events)


# ================================ pseudonyms and the gateway headers =====================

def test_the_gateway_receives_the_pseudonym_and_the_study_never_the_email(world):
    import llm
    import study
    llm.save({"mode": "included", "base_url": "https://infer.example", "auth_token": "sik_x"})
    env = llm.session_env(CAMILLE)
    assert env["ANTHROPIC_CUSTOM_HEADERS"].split("\n") == [
        "x-sokkan-study: STUDY-2027-A", "x-sokkan-user: P1"]
    assert "camille" not in json.dumps(env)
    assert llm.identity_headers(NOAH, "assistant:") == {
        "x-sokkan-study": "STUDY-2027-A", "x-sokkan-user": "assistant:P2"}
    assert llm.session_env("")["ANTHROPIC_CUSTOM_HEADERS"] == "x-sokkan-study: STUDY-2027-A"
    # stable, in order, never reused
    assert [study.pseudonym_of(e) for e in (CAMILLE, NOAH, CAMILLE.upper(), LEA)] == [
        "P1", "P2", "P1", "P3"]
    # study over: back to what the gateway received before
    world["set_usage"](LEGACY_USAGE)
    assert llm.session_env(CAMILLE)["ANTHROPIC_CUSTOM_HEADERS"] == f"x-sokkan-user: {CAMILLE}"


def test_nina_sends_the_pseudonym_too(world, monkeypatch):
    import assistant
    seen = {}

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "content": [{"text": "ok"}]}

    def post(url, headers=None, **kw):
        seen.update(headers or {})
        return R()
    monkeypatch.setattr(assistant.httpx, "post", post)
    assistant._ask({"api": "openai", "url": "https://infer.example/v1", "token": "t",
                    "model": "m"}, "sys", [{"role": "user", "content": "hi"}], CAMILLE)
    assert seen["x-sokkan-user"] == "assistant:P1" and seen["x-sokkan-study"] == "STUDY-2027-A"
    assert CAMILLE not in json.dumps(seen)


def test_the_correspondence_table_is_the_only_place_with_an_email(world):
    import study
    _consent(world, CAMILLE, ["S2"])
    _consent(world, NOAH)
    world["as"](CAMILLE).put(f"/api/sessions/{S_A}/study", json={"scenario": "S2",
                                                                "reference_minutes": 90})
    t = study.turn_start(S_A, CAMILLE, LEAK["turn_prompt"])
    study.turn_end(t, model="sokkan-ship", tokens_in=10, tokens_out=5)
    world["as"](CAMILLE).post(f"/api/sessions/{S_A}/study/feedback",
                              json={"useful": 4, "verify_minutes": 12})
    world["as"](ADMIN).post("/api/admin/study/tokens", json={"label": "uni"})
    world["as"](ADMIN).get("/api/study/export?format=json")
    con = sqlite3.connect(world["tmp"] / "study.db")
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "participants" in tables
    for t in tables:
        dump = json.dumps(con.execute(f"SELECT * FROM {t}").fetchall(), default=str)
        if t == "participants":
            assert CAMILLE in dump and NOAH in dump
        else:
            assert "@" not in dump, t
            assert LEAK["turn_prompt"] not in dump, t        # a turn keeps a size and a hash
    con.close()
    # the table is shown to the instance's admins only, and reading it is journaled
    assert world["as"](CAMILLE).get("/api/admin/study").status_code == 403
    adm = world["as"](ADMIN).get("/api/admin/study").json()
    assert [(p["pseudonym"], p["email"], p["consent"]) for p in adm["participants"]] == [
        ("P1", CAMILLE, "given"), ("P2", NOAH, "given")]
    import audit
    assert any(e["action"] == "study.correspondence.read" for e in audit.recent(50))
    # the action journal does not become a second correspondence table
    for e in audit.recent(200):
        if e["action"].startswith("study.consent") or e["action"] == "study.tag":
            assert e["user"] == "" and "@" not in e["detail"]


# ================================ consent ================================================

def test_consent_is_explicit_per_person_before_the_first_session_and_can_be_withdrawn(world):
    import agentchat
    import study
    c = world["as"](CAMILLE)
    me = c.get("/api/study/me").json()
    assert me["consent"]["given"] is False and me["blocked"] is True
    # before consenting: no turn — through the session and through the cockpit's WebSocket
    sess = agentchat.AgentSession(S_B, cwd="/tmp", user=CAMILLE)
    _run(sess.handle_user("hello"))
    assert sess.client is None and "consent" in sess.events[-1]["message"]
    with c.websocket_connect(f"/api/agent/ws/{S_B}") as ws:
        ws.send_json({"type": "user", "text": "hello"})
        for _ in range(50):
            ev = ws.receive_json()
            if ev.get("type") == "error":
                break
    assert "Setup › Study" in ev["message"] and study.tag_of(S_B) is None
    assert c.put(f"/api/sessions/{S_B}/study", json={"scenario": "S1"}).status_code == 409
    # explicit consent, with the scenarios whose content the person agrees to share
    got = _consent(world, CAMILLE, ["S2", "s3", "S9", "nope"])
    assert got["given"] is True and got["current"] is True
    assert got["content_scenarios"] == ["S2", "S3"] and got["version"] == study.CONSENT_VERSION
    assert study.gate(CAMILLE) is None and study.gate(NOAH) is not None      # per person
    assert c.put("/api/study/consent/content", json={"content_scenarios": []}).json()[
        "content_scenarios"] == []
    # a new version of the text asks everyone again
    old = study.CONSENT_VERSION
    study.CONSENT_VERSION = "2099-01"
    try:
        assert study.gate(CAMILLE) is not None
        assert c.get("/api/study/me").json()["consent"]["current"] is False
    finally:
        study.CONSENT_VERSION = old
    # withdrawal
    out = c.delete("/api/study/consent").json()
    assert out["given"] is False and out["withdrawn_at"] is not None
    assert study.gate(CAMILLE) is not None and study.turn_start(S_B, CAMILLE, "x") is None


def test_when_the_audit_is_not_required_nobody_is_blocked_and_only_consenting_people_are_recorded(world):
    import study
    world["set_usage"]({**STUDY_USAGE, "study": {**STUDY_USAGE["study"], "audit_required": False}})
    assert study.gate(LEA) is None
    assert study.turn_start(S_L, LEA, "hello") is None and study.tag_of(S_L) is None
    _consent(world, NOAH)
    assert study.turn_start(S_N, NOAH, "hello") is not None and study.tag_of(S_N)["auto"] == 1


# ================================ tags, turns, feedback ==================================

def test_tagging_a_session_scenario_reference_time_participant(world):
    import study
    _consent(world, CAMILLE)
    c = world["as"](CAMILLE)
    assert c.get(f"/api/sessions/{S_B}/study").json() == {"tagged": False}
    t = c.put(f"/api/sessions/{S_B}/study", json={"scenario": "s3", "reference_minutes": 45}).json()
    assert t == {"tagged": True, "study_id": "STUDY-2027-A", "participant": "P1",
                 "scenario": "S3", "reference_minutes": 45.0}
    assert c.put(f"/api/sessions/{S_B}/study", json={"reference_minutes": 60}).json()[
        "scenario"] == "S3"                                           # a field left out is kept
    assert c.put(f"/api/sessions/{S_B}/study", json={"scenario": "S7"}).status_code == 400
    assert c.put(f"/api/sessions/{S_B}/study", json={"reference_minutes": -1}).status_code == 400
    assert c.put("/api/sessions/unknown-session/study", json={"scenario": "S1"}).status_code == 404
    # another dev does not tag my session; an admin of the project may; the tag stays mine
    assert world["as"](NOAH).put(f"/api/sessions/{S_B}/study",
                                 json={"scenario": "S1"}).status_code == 403
    assert world["as"](ADMIN).put(f"/api/sessions/{S_B}/study",
                                  json={"scenario": "S4"}).json()["participant"] == "P1"
    assert world["as"](NOAH).get(f"/api/sessions/{S_B}/study").json()["mine"] is False
    assert study.tag_of(S_B)["auto"] == 0
    mine = world["as"](CAMILLE).get("/api/study/sessions").json()["sessions"]
    assert [(s["session_id"], s["scenario"], s["reference_minutes"], s["title"]) for s in mine] == [
        (S_B, "S4", 60.0, "second task")]
    assert world["as"](NOAH).get("/api/study/sessions").json()["sessions"] == []


def test_a_card_tag_is_inherited_by_its_session(world):
    import board
    import study
    _consent(world, CAMILLE)
    card = board.add_card("Plan", description="x", tag="work")
    con = board._con()
    con.execute("UPDATE cards SET session_id = ? WHERE id = ?", (S_B, card["id"]))
    con.commit()
    con.close()
    r = world["as"](CAMILLE).put(f"/api/board/card/{card['id']}/study",
                                 json={"scenario": "S5", "reference_minutes": 30})
    assert r.status_code == 200 and r.json()["scenario"] == "S5"
    assert world["as"](CAMILLE).put("/api/board/card/99999/study",
                                    json={"scenario": "S5"}).status_code == 404
    study.turn_start(S_B, CAMILLE, "go")
    t = study.tag_of(S_B)
    assert (t["scenario"], t["reference_minutes"], t["card_id"], t["auto"]) == (
        "S5", 30.0, card["id"], 1)


def test_a_turn_records_duration_and_tier_through_the_session(world):
    """AgentSession.handle_user with a client that answers at once: what the study keeps of
    a turn is its duration, the tier that served it and its token counts."""
    import agentchat
    import study
    from claude_agent_sdk import ResultMessage
    _consent(world, CAMILLE)

    class Client:
        async def query(self, text):
            await asyncio.sleep(0.03)

        async def receive_response(self):
            yield ResultMessage(subtype="success", duration_ms=30, duration_api_ms=20,
                                is_error=False, num_turns=1, session_id="cs-1",
                                total_cost_usd=0.0, usage={"input_tokens": 1200,
                                                           "output_tokens": 340}, result="done")

    sess = agentchat.AgentSession(S_B, cwd="/tmp", user=CAMILLE)
    sess.client = Client()
    sess._model_seen = "sokkan-ship"
    _run(sess.handle_user(LEAK["turn_prompt"]))
    _run(sess.handle_user(LEAK["turn_prompt"]))
    con = sqlite3.connect(world["tmp"] / "study.db")
    con.row_factory = sqlite3.Row
    a, b = [dict(r) for r in con.execute("SELECT * FROM turns ORDER BY id")]
    con.close()
    assert (a["session_id"], a["pseudonym"], a["study_id"], a["model"]) == (
        S_B, "P1", "STUDY-2027-A", "sokkan-ship")
    assert (a["tokens_in"], a["tokens_out"], a["is_error"]) == (1200, 340, 0)
    assert (b["tokens_in"], b["tokens_out"]) == (1200, 340)          # per turn, not cumulated
    assert a["duration_ms"] >= 30 and a["ended_at"] > a["started_at"]
    assert a["prompt_chars"] == len(LEAK["turn_prompt"]) and a["prompt_hmac"] == b["prompt_hmac"]
    assert LEAK["turn_prompt"] not in json.dumps(a)
    assert study.tag_of(S_B)["auto"] == 1                             # tagged by the first turn
    assert world["as"](CAMILLE).get("/api/study/me").json()["pending_feedback"] == 1


def test_the_two_questions_at_the_end_of_a_task(world):
    import study
    _consent(world, CAMILLE)
    c = world["as"](CAMILLE)
    assert c.post(f"/api/sessions/{S_B}/study/feedback",
                  json={"useful": 4, "verify_minutes": 10}).status_code == 404    # not tagged
    c.put(f"/api/sessions/{S_B}/study", json={"scenario": "S1", "reference_minutes": 40})
    study.turn_end(study.turn_start(S_B, CAMILLE, "x"), model="sokkan-ship")
    for bad in ({"useful": 0, "verify_minutes": 5}, {"useful": 6, "verify_minutes": 5},
                {"useful": 3, "verify_minutes": -2}):
        assert c.post(f"/api/sessions/{S_B}/study/feedback", json=bad).status_code == 400
    assert world["as"](NOAH).post(f"/api/sessions/{S_B}/study/feedback",
                                  json={"useful": 5, "verify_minutes": 1}).status_code == 403
    f = world["as"](CAMILLE).post(f"/api/sessions/{S_B}/study/feedback",
                                  json={"useful": 4, "verify_minutes": 12.5}).json()
    assert (f["useful"], f["verify_minutes"]) == (4, 12.5)
    world["as"](CAMILLE).post(f"/api/sessions/{S_B}/study/feedback",
                              json={"useful": 2, "verify_minutes": 20})    # corrected: one answer
    s = world["as"](CAMILLE).get("/api/study/sessions").json()["sessions"][0]
    assert (s["useful"], s["verify_minutes"], s["closed_at"] is not None) == (2, 20.0, True)
    assert world["as"](CAMILLE).get("/api/study/me").json()["pending_feedback"] == 0


# ================================ the researcher's package ===============================

def _transcript(world, sid, csid):
    import board
    board.set_claude_session_id(sid, csid)
    d = world["tmp"] / "transcripts"
    d.mkdir(exist_ok=True)
    lines = [
        {"type": "user", "sessionId": csid, "message": {"role": "user", "content":
            f"{LEAK['turn_prompt']} — ecris a {NOAH} et a client@dubois.example"}},
        {"type": "assistant", "sessionId": csid, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Voici le plan en trois modules."},
            {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {
                "command": f"curl -H 'Authorization: Bearer {VAULT_SECRET}' https://api.example.org"
                           f" && export K={PASTED_KEY}"}}]}},
        {"type": "user", "sessionId": csid, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": f"ok {VAULT_SECRET}"}]}},
    ]
    (d / f"{csid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _fill(world, content=()):
    """A small study: Camille (two sessions, S2 then S1), Noah (one), Lea (never consents)."""
    import agents
    import audit
    import hitl_journal
    import study
    _consent(world, CAMILLE, content)
    _consent(world, NOAH)
    c = world["as"](CAMILLE, LEAK["project_slug"])
    assert c.put(f"/api/sessions/{S_A}/study",
                 json={"scenario": "S2", "reference_minutes": 90}).status_code == 200
    world["as"](CAMILLE).put(f"/api/sessions/{S_B}/study",
                             json={"scenario": "S1", "reference_minutes": 30})
    for i in range(3):
        t = study.turn_start(S_A, CAMILLE, LEAK["turn_prompt"] + str(i))
        study.turn_end(t, model="sokkan-deep" if i == 2 else "sokkan-ship",
                       tokens_in=1000 + i, tokens_out=200)
    study.turn_end(study.turn_start(S_B, CAMILLE, "resume ce fichier"), model="sokkan-ship",
                   tokens_in=500, tokens_out=100)
    study.turn_end(study.turn_start(S_N, NOAH, "bonjour"), model="sokkan-ship")
    sec = {"DEPLOY_TOKEN": VAULT_SECRET}
    for pid, decision, who, origin, kw in (
            ("p1", "allow", CAMILLE, "owner", {}),
            ("p2", "deny", CAMILLE, "owner", {"message": LEAK["deny_message"]}),
            ("p3", "allow", CAMILLE, "owner", {"edited_input": {"file_path": "/x", "content": "y"}}),
            ("p4", "allow", ADMIN, "project_member", {})):
        hitl_journal.requested(pid, "permission", session_id=S_A, tool="Write",
                               title=LEAK["tool_title"], secrets=sec, owner=CAMILLE,
                               input_data={"file_path": LEAK["tool_title"],
                                           "content": f"{LEAK['tool_input']} {VAULT_SECRET}"})
        time.sleep(0.01)
        hitl_journal.decided(pid, decision, actor=who, origin=origin, secrets=sec, **kw)
    hitl_journal.automatic("permission", session_id=S_A, tool="Edit", title=LEAK["tool_title"],
                           input_data={"file_path": LEAK["tool_title"]}, allowed=True,
                           reason="mode:acceptEdits", owner=CAMILLE)
    hitl_journal.requested("pl", "permission", session_id=S_L, tool="Bash", title="ls",
                           input_data={"command": "ls"}, owner=LEA)
    hitl_journal.decided("pl", "allow", actor=LEA, origin="owner")
    audit.log(CAMILLE, "session.send", f"session:{S_A}", LEAK["audit_detail"],
              project=LEAK["project_slug"])
    audit.log(LEA, "session.send", f"session:{S_L}", "lea's own work", project="default")
    now = datetime.datetime.now(datetime.timezone.utc)
    old = (now - datetime.timedelta(days=420)).date().isoformat()
    world["store"].notes[(LEAK["project_slug"], LEAK["note_name"])] = _Note(old)
    world["store"].recalls.append({
        "id": 1, "at": now.isoformat(), "channel": "prompt", "session_id": S_A, "agent_id": None,
        "query": LEAK["recall_query"], "note_name": LEAK["note_name"], "rank": 1, "score": 0.81,
        "rerank": None, "content_hash": "abc", "generation_id": 1,
        "project": LEAK["project_slug"], "level": 2})
    world["store"].accesses.append({
        "id": 9, "at": (now + datetime.timedelta(seconds=40)).isoformat(), "via": "mcp",
        "actor": CAMILLE, "session_id": S_A, "project": LEAK["project_slug"],
        "note_name": LEAK["note_name"], "level": 2, "query": LEAK["recall_query"]})
    world["as"](CAMILLE).post(f"/api/sessions/{S_A}/study/feedback",
                              json={"useful": 4, "verify_minutes": 25})
    _transcript(world, S_A, "cs-a")
    _transcript(world, S_B, "cs-b")
    # what the session's environment held: masked wherever it would be written
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(agents, "session_secret_values", lambda sid: sec)
    world["mp"] = mp


@pytest.fixture()
def study_world(world):
    yield world
    if world.get("mp"):
        world["mp"].undo()


def _export(world, fmt="json", **params):
    q = "&".join(f"{k}={v}" for k, v in {"format": fmt, **params}.items())
    r = world["as"](ADMIN).get(f"/api/study/export?{q}")
    assert r.status_code == 200, r.text
    return r


def _all_text(world, package_response_json, zip_bytes) -> str:
    """Everything a researcher receives, as one text: the JSON package and every zip member."""
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    return json.dumps(package_response_json, ensure_ascii=False) + "".join(
        z.read(n).decode() for n in z.namelist())


def assert_no_leak(text: str) -> None:
    """THE property of a metadata export: no e-mail, no name of a person, no user content."""
    low = text.lower()
    for person in (CAMILLE, NOAH, LEA, ADMIN, "camille", "noah@", "lea@"):
        assert person.lower() not in low, f"a person is named: {person}"
    assert "@x.example" not in low and "dubois" not in low
    for field, sentence in LEAK.items():
        assert sentence.lower() not in low, f"user content leaked: {field}"
    for secret in (VAULT_SECRET, PASTED_KEY):
        assert secret not in text, "a secret leaked"


def test_metadata_export_holds_what_the_protocol_asks_and_names_nobody(study_world):
    world = study_world
    _fill(world)
    pkg = _export(world).json()
    zip_bytes = _export(world, "zip").content
    assert_no_leak(_all_text(world, pkg, zip_bytes))
    m = pkg["manifest"]
    assert (m["format"], m["study_id"], m["mode"]) == ("sokkan-study-export/1",
                                                       "STUDY-2027-A", "metadata")
    assert m["counts"] == {"participants": 2, "sessions": 3, "turns": 5, "validations": 5,
                           "sources": 2, "events": 7, "feedback": 1, "sessions_with_content": 0}
    assert pkg["content"] == []
    assert [p["participant"] for p in pkg["participants"]] == ["P1", "P2"]
    s = next(x for x in pkg["sessions"] if x["scenario"] == "S2")
    assert s["participant"] == "P1" and s["reference_minutes"] == 90 and s["tagged_by_hand"] is True
    assert s["session"].startswith("s_") and s["project"].startswith("prj_")
    assert (s["turns"], s["followup_prompts"], s["tokens_in"], s["tokens_out"]) == (3, 2, 3003, 600)
    assert s["tiers_served"] == {"sokkan-ship": 2, "sokkan-deep": 1}
    v = s["validations"]
    assert (v["asked"], v["allowed"], v["denied"], v["edited"], v["auto_allowed"]) == (4, 2, 1, 1, 1)
    assert v["decision_delay"]["count"] == 4 and v["decision_delay"]["median_ms"] >= 10
    assert s["corrections"] == 2 and s["agent_seconds"] >= 0 and s["elapsed_seconds"] is not None
    assert s["feedback"]["useful"] == 4 and s["feedback"]["verify_minutes"] == 25
    assert s["title"]["chars"] == len(LEAK["session_title"]) and len(s["title"]["hmac"]) == 16
    assert s["prompt"]["chars"] == len(LEAK["session_prompt"]) and s["content_shared"] is False
    # validations: decision, delay, origin, who by pseudonym — text fields withheld
    rows = [x for x in pkg["validations"] if x["session"] == s["session"]]
    assert [(x["decision"], x["auto"], x["origin"], x["actor"]) for x in rows] == [
        ("allow", False, "owner", "P1"), ("deny", False, "owner", "P1"),
        ("edit", False, "owner", "P1"), ("allow", False, "project_member", "non-participant"),
        ("auto_allow", True, "system", "")]
    assert all(x["tool"] in ("Write", "Edit") and set(x["title"]) == {"chars", "hmac"}
               and set(x["input"]) == {"chars", "hmac"} and "content" not in x for x in rows)
    assert rows[1]["message"]["chars"] == len(LEAK["deny_message"]) and rows[0]["delay_ms"] >= 10
    assert rows[0]["requested_at"] < rows[0]["decided_at"]
    # sources: which note (hashed), how old it was when recalled
    src = pkg["sources"]
    assert [x["log"] for x in src] == ["recall", "access"] and src[0]["channel"] == "prompt"
    assert src[0]["note"] == src[1]["note"] and src[0]["note"].startswith("n_")
    assert 419 <= src[0]["note_age_days"] <= 421 and src[0]["score"] == 0.81
    assert src[0]["query"] == src[1]["query"] and src[0]["query"]["chars"] == len(LEAK["recall_query"])
    # the journal's actions about these sessions: the action, never its detail
    mine = [e for e in pkg["events"] if e["session"] == s["session"]]
    assert sorted((e["action"], e["actor"]) for e in mine) == [
        ("hitl.allow", "P1"), ("hitl.allow", "non-participant"), ("hitl.deny", "P1"),
        ("hitl.edit", "P1"), ("session.send", "P1"), ("study.tag", "")]
    sent = next(e for e in mine if e["action"] == "session.send")
    assert sent["detail"]["chars"] == len(LEAK["audit_detail"]) and "content" not in sent
    assert pkg["feedback"] == [{"session": s["session"], "participant": "P1", "scenario": "S2",
                                "useful": 4, "verify_minutes": 25.0, "reference_minutes": 90.0,
                                "at": s["feedback"]["at"]}]
    # the zip: one CSV per table + the manifest, nothing under content/
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    assert sorted(z.namelist()) == ["README.txt", "events.csv", "feedback.csv", "manifest.json",
                                    "participants.csv", "sessions.csv", "sources.csv",
                                    "turns.csv", "validations.csv"]
    head = z.read("validations.csv").decode().splitlines()[0].split(",")
    assert {"participant", "decision", "delay_ms", "origin", "title.chars", "input.hmac"} <= set(head)
    # somebody who never consented is in no table (Lea's session, validation and action)
    assert all(x["participant"] in ("P1", "P2") for k in ("sessions", "turns", "validations",
                                                           "sources", "events") for x in pkg[k])
    # the same text hashes to the same value inside the study, and the hash is keyed
    import hashlib
    assert s["title"]["hmac"] != hashlib.sha256(LEAK["session_title"].encode()).hexdigest()[:16]


@pytest.mark.parametrize("removed", ["_withhold", "_person", "_sid+_prj+_note"])
def test_the_no_leak_test_fails_when_a_filter_is_removed(study_world, monkeypatch, removed):
    """Mutation: each filter of the metadata export is removed in turn. With the guard still
    there the export is REFUSED when a person would be named; with the guard removed too,
    `assert_no_leak` — the check the test above relies on — fails. A test that passed here
    would prove nothing."""
    import study_export
    world = study_world
    _fill(world)
    if removed == "_withhold":      # texts leave as they are
        monkeypatch.setattr(study_export, "_withhold",
                            lambda text: {"chars": len(str(text)), "hmac": text})
    elif removed == "_person":      # people leave under their address
        monkeypatch.setattr(study_export, "_person", lambda email, people: email)
    else:                           # ids, project slugs and note names leave as they are
        monkeypatch.setattr(study_export, "_sid", lambda s: s)
        monkeypatch.setattr(study_export, "_prj", lambda p: p)
        monkeypatch.setattr(study_export, "_note", lambda p, n: n)
    if removed == "_person":
        r = world["as"](ADMIN).get("/api/study/export?format=json")
        assert r.status_code == 500 and "export refused" in r.json()["detail"]
        import audit
        assert any(e["action"] == "study.export.refused" for e in audit.recent(20))
    monkeypatch.setattr(study_export, "_guard", lambda package, forbidden: None)
    text = _all_text(world, _export(world).json(), _export(world, "zip").content)
    with pytest.raises(AssertionError, match="named|leaked|assert"):
        assert_no_leak(text)


def test_the_guard_refuses_a_package_that_names_somebody():
    import study_export
    ok = {"sessions": [{"participant": "P1", "tool": "mcp__sokkan-memory__memory_search"}]}
    study_export._guard(ok, ["camille@x.example", "camille.durand"])
    for bad in ({"x": "write to someone@else.example"}, {"x": ["Camille.Durand was here"]}):
        with pytest.raises(study_export.ExportLeak):
            study_export._guard(bad, ["camille@x.example", "camille.durand"])


def test_content_is_exported_only_for_the_scenarios_a_person_agreed_to(study_world):
    world = study_world
    _fill(world, content=["S2"])            # Camille shares S2 (session A), not S1 (session B)
    pkg = _export(world).json()
    z = zipfile.ZipFile(io.BytesIO(_export(world, "zip").content))
    assert pkg["manifest"]["mode"] == "metadata+content"
    assert pkg["manifest"]["counts"]["sessions_with_content"] == 1
    a = next(x for x in pkg["sessions"] if x["scenario"] == "S2")
    b = next(x for x in pkg["sessions"] if x["scenario"] == "S1")
    assert a["content_shared"] is True and b["content_shared"] is False
    (c,) = pkg["content"]
    assert c["session"] == a["session"] and c["participant"] == "P1"
    text = json.dumps(c, ensure_ascii=False)
    assert LEAK["turn_prompt"] in text and "Voici le plan en trois modules." in text
    # secrets masked when the package is written; nobody named, even inside the content
    blob = json.dumps(pkg, ensure_ascii=False) + "".join(z.read(n).decode() for n in z.namelist())
    assert VAULT_SECRET not in blob and PASTED_KEY not in blob
    assert "[secret:DEPLOY_TOKEN]" in text and "[masked:key]" in text
    # what a tool returned (the text of a file it read) leaves as a size, consent or not
    tool = next(m for m in c["messages"] if m["kind"] == "tool")
    assert tool["result"] == {"chars": len(f"ok {VAULT_SECRET}"), "is_error": False}
    assert "@" not in blob and "ecris a P2 et a [email]" in text
    # the consented session's validations carry their text; the other session's do not
    va = [x for x in pkg["validations"] if x["session"] == a["session"] and "content" in x]
    assert va and va[0]["content"]["title"] == LEAK["tool_title"]
    assert LEAK["tool_input"] in va[0]["content"]["input_summary"]
    assert va[1]["content"]["message"] == LEAK["deny_message"]
    assert pkg["sources"][0]["content"] == {"note_name": LEAK["note_name"],
                                            "query": LEAK["recall_query"]}
    sent = next(e for e in pkg["events"] if e["action"] == "session.send")
    assert sent["content"]["detail"] == LEAK["audit_detail"]
    assert all("content" not in x for x in pkg["validations"] if x["session"] == b["session"])
    assert "resume ce fichier" not in blob                     # session B: S1, not shared
    assert f"content/{a['session']}.json" in z.namelist() and "content/fields.json" in z.namelist()
    assert f"content/{b['session']}.json" not in z.namelist()
    # the person changes their mind: the next package has no content
    world["as"](CAMILLE).put("/api/study/consent/content", json={"content_scenarios": []})
    again = _export(world).json()
    assert again["content"] == [] and again["manifest"]["mode"] == "metadata"
    assert LEAK["turn_prompt"] not in json.dumps(again, ensure_ascii=False)


def test_a_withdrawal_takes_everything_collected_under_that_consent_out(study_world):
    import study
    world = study_world
    _fill(world, content=["S2"])
    assert world["as"](CAMILLE).delete("/api/study/consent").status_code == 200
    pkg = _export(world).json()
    assert [p["participant"] for p in pkg["participants"]] == ["P2"]
    assert all(x["participant"] == "P2" for k in ("sessions", "turns", "validations", "sources",
                                                  "events", "feedback") for x in pkg[k])
    assert pkg["content"] == [] and pkg["manifest"]["counts"]["sessions"] == 1
    # consenting again starts a new period: what was collected before stays out
    time.sleep(0.01)
    _consent(world, CAMILLE)
    assert _export(world).json()["manifest"]["counts"]["sessions"] == 1
    board_sid = "e" * 32
    import board
    board.add_sdk_session(board_sid, "work", title="after", project="default", owner=CAMILLE)
    study.turn_end(study.turn_start(board_sid, CAMILLE, "again"), model="sokkan-ship")
    assert _export(world).json()["manifest"]["counts"]["sessions"] == 2


def test_export_period_filter(study_world):
    world = study_world
    _fill(world)
    tomorrow = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
    assert _export(world, since=tomorrow).json()["manifest"]["counts"]["sessions"] == 0
    assert _export(world, until=tomorrow).json()["manifest"]["counts"]["sessions"] == 3
    assert world["as"](ADMIN).get("/api/study/export?since=soon").status_code == 400


def test_the_export_token_is_read_only_and_opens_this_package_only(study_world):
    import study
    world = study_world
    _fill(world)
    assert world["as"](CAMILLE).post("/api/admin/study/tokens", json={}).status_code == 403
    made = world["as"](ADMIN).post("/api/admin/study/tokens", json={"label": "uni", "days": 7}).json()
    tok = made["token"]
    assert tok.startswith("skx_") and made["expires_at"] > time.time() + 6 * 86400
    listed = world["as"](ADMIN).get("/api/admin/study").json()["tokens"]
    assert listed[0]["hint"] == tok[-4:] and tok not in json.dumps(listed)   # shown once
    assert tok not in (world["tmp"] / "study.db").read_bytes().decode("latin-1")
    # the researcher: no session on the instance, only the token
    c = world["as"](None)
    assert c.get("/api/study/export?format=json").status_code == 401
    bearer = {"authorization": f"Bearer {tok}"}
    r = c.get("/api/study/export?format=json", headers=bearer)
    assert r.status_code == 200 and r.json()["manifest"]["counts"]["sessions"] == 3
    assert c.get("/api/study/export", headers=bearer).headers["content-type"] == "application/zip"
    # it opens nothing else, and writes nothing
    for method, path in (("get", "/api/study/me"), ("get", "/api/admin/study"),
                         ("get", "/api/hitl/journal"), ("get", "/api/sessions"),
                         ("get", "/api/journal"), ("post", "/api/study/consent"),
                         ("post", "/api/admin/study/tokens"), ("delete", "/api/study/consent"),
                         ("post", "/api/study/export"), ("delete", "/api/study/export")):
        kw = {"json": {}} if method == "post" else {}
        assert getattr(c, method)(path, headers=bearer, **kw).status_code in (401, 405), path
    for bad in ("skx_wrong", tok[:-1] + ("A" if tok[-1] != "A" else "B"), "", "sik_x"):
        assert c.get("/api/study/export", headers={"authorization": f"Bearer {bad}"}
                     ).status_code == 401
    # a dev's own session is not enough
    assert world["as"](CAMILLE).get("/api/study/export").status_code == 403
    # use is counted; revoked or expired = refused
    row = study.list_tokens()[0]
    assert row["uses"] == 2 and row["last_used_at"] is not None
    assert world["as"](ADMIN).delete(f"/api/admin/study/tokens/{row['id']}").status_code == 200
    assert world["as"](None).get("/api/study/export", headers=bearer).status_code == 401
    short = study.create_token("short", days=1)
    con = sqlite3.connect(world["tmp"] / "study.db")
    con.execute("UPDATE tokens SET expires_at = ? WHERE id = ?", (time.time() - 1, short["id"]))
    con.commit()
    con.close()
    assert world["as"](None).get("/api/study/export", headers={
        "authorization": f"Bearer {short['token']}"}).status_code == 401
    # every export is journaled, and logged with what it held
    import audit
    assert [e["detail"] for e in audit.recent(100) if e["action"] == "study.export"]
    assert {e["by"] for e in study.exports()} == {f"token:{row['id']}"}
    assert any(e["action"] == "study.token.create" and e["user"] == ADMIN for e in audit.recent(100))


def test_sources_are_said_missing_when_the_memory_keeps_no_recall_log(study_world, monkeypatch):
    import store_backend
    world = study_world
    _fill(world)
    monkeypatch.setattr(store_backend, "enabled", lambda: False)
    pkg = _export(world).json()
    assert pkg["sources"] == [] and any("recall log" in n for n in pkg["manifest"]["notes"])
