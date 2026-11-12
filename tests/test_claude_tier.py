"""« Write with Claude » (3.6.5, feature `claude_tier`): the router's third tier, the decision by
the project's classification, the refusals a person can read, and the model named on every
answer, deliverable, validation and export (backend/claudetier.py, shiprouter.py, plain.py).

The router is played against real sockets: a fake local model, a fake Deep gateway, a fake
api (the grant) and a fake Anthropic API — what each of them receives is what leaves."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import claudetier
import llmerrors
import plain
import shiprouter
from simple_fixtures import world  # noqa: F401 — the fixture

KEY = "sk-ant-api03-THE-ORGANISATION-KEY-never-shown-0123456789"
SECRET = "router-secret-for-tests-0123456789abcdef"

SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"%s"}}\n\n'
       b'event: message_stop\ndata: {"type":"message_stop"}\n\n')


class Up:
    """An HTTP upstream recording what it receives. POST /v1/messages answers a stream whose
    message_start names `model`; GET answers `get_body`."""

    def __init__(self, model="x"):
        self.model, self.status, self.seen, self.get_body, self.get_status = model, 200, [], {}, 200
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                out = json.dumps(outer.get_body).encode()
                self.send_response(outer.get_status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": json.loads(raw or b"{}")})
                if outer.status == 200:
                    out, ctype = SSE % outer.model.encode(), "text/event-stream"
                else:
                    out, ctype = json.dumps({"type": "error", "error": {"type": "x", "message": "no"}}).encode(), \
                        "application/json"
                self.send_response(outer.status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def box(monkeypatch):
    ship, deep, api, anthropic = Up("qwen3.8-27b"), Up("deep-model"), Up(), Up("claude-opus-5-5")
    import os
    for k in list(os.environ):
        if k.startswith(("SOKKAN_SHIP", "SOKKAN_DEEP", "SOKKAN_ESCALATION", "SOKKAN_ERROR_LANG",
                         "SOKKAN_CLAUDE", "SOKKAN_ROUTER")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("SOKKAN_SHIP_URL", ship.url)
    monkeypatch.setenv("SOKKAN_SHIP_MODEL", "qwen3.8-27b")
    monkeypatch.setenv("SOKKAN_DEEP_URL", deep.url)
    monkeypatch.setenv("SOKKAN_DEEP_TOKEN", "sik_instance_token")
    monkeypatch.setenv("SOKKAN_SHIP_CONNECT_TIMEOUT_S", "1")
    monkeypatch.setenv("SOKKAN_ROUTER_SECRET", SECRET)
    monkeypatch.setenv("SOKKAN_ROUTER_API_URL", api.url)
    monkeypatch.setenv("SOKKAN_CLAUDE_URL", anthropic.url)
    api.get_body = {"allowed": True, "key": KEY, "models": list(claudetier.DEFAULT_MODELS),
                    "default_model": "claude-opus-5-5"}
    shiprouter._stats.clear()
    shiprouter._grants.clear()
    yield TestClient(shiprouter.app), ship, deep, api, anthropic
    for u in (ship, deep, api, anthropic):
        u.close()


def ask(text="Rédige le module 3 du cours", model="claude-opus-5-5", **kw):
    return {"model": model, "max_tokens": 64, "stream": True,
            "tools": [{"name": "Write", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": text}], **kw}


CLAUDE = {"x-sokkan-tier": "claude", "x-sokkan-project": "cours", "x-sokkan-user": "ana@school.example",
          "x-sokkan-study": "s1", "x-api-key": "local", "authorization": "Bearer local",
          "anthropic-version": "2023-06-01", "anthropic-beta": "some-beta"}


# ---------------------------------------------------------------- the router

def test_the_gesture_sends_the_request_to_anthropic_with_the_key_and_nothing_of_the_box(box):
    c, ship, deep, api, anthropic = box
    r = c.post("/v1/messages", json=ask(), headers=CLAUDE)
    assert r.status_code == 200, r.text
    assert r.headers["x-sokkan-tier"] == "claude" and b'"model":"claude-opus-5-5"' in r.content
    assert ship.seen == [] and deep.seen == []
    (sent,) = anthropic.seen
    h = sent["headers"]
    assert sent["path"] == "/v1/messages" and h["x-api-key"] == KEY
    assert h["anthropic-version"] == "2023-06-01" and h["anthropic-beta"] == "some-beta"
    assert "authorization" not in h                                        # no Bearer to Anthropic
    assert not [k for k in h if k.startswith("x-sokkan-")]                 # user, project, study stay home
    assert sent["body"]["model"] == "claude-opus-5-5"                      # the model asked, untouched
    # the router asked the api for THIS project and model, with its secret
    (g,) = [x for x in api.seen if x["path"].startswith("/api/internal/claude-grant")]
    assert "project=cours" in g["path"] and "model=claude-opus-5-5" in g["path"]
    assert g["headers"]["x-sokkan-router-secret"] == SECRET
    assert KEY not in r.text and KEY not in json.dumps(c.get("/stats").json())


def test_anthropic_version_is_set_when_the_caller_did_not(box):
    c, *_, anthropic = box
    h = {k: v for k, v in CLAUDE.items() if k != "anthropic-version"}
    assert c.post("/v1/messages", json=ask(), headers=h).status_code == 200
    assert anthropic.seen[0]["headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize("payload,headers", [
    (ask(model="claude-opus-5-5"), {}),                                  # a Claude model by name
    (ask("rédige ça avec claude /claude #claude"), {}),                 # a keyword
    (ask("x" * 50000), {}),                                              # a size
    (ask(), {"x-sokkan-tier": "deep"}),                                  # another explicit tier
])
def test_nothing_but_the_header_ever_leads_to_claude(box, payload, headers):
    c, ship, deep, api, anthropic = box
    r = c.post("/v1/messages", json=payload, headers=headers)
    assert r.status_code == 200 and r.headers["x-sokkan-tier"] in ("ship", "deep")
    assert anthropic.seen == [] and api.seen == []


def test_ship_down_never_goes_to_claude(box, monkeypatch):
    c, ship, deep, api, anthropic = box
    ship.status = 500
    r = c.post("/v1/messages", json=ask("bonjour", model="sokkan-ship"))
    assert r.headers["x-sokkan-tier"] == "deep" and anthropic.seen == []
    monkeypatch.setenv("SOKKAN_SHIP_FALLBACK_DEEP", "0")
    r = c.post("/v1/messages", json=ask("bonjour", model="sokkan-ship"))
    assert r.status_code >= 500 and anthropic.seen == []


def test_side_calls_of_a_claude_work_stay_on_the_local_model(box):
    c, ship, deep, api, anthropic = box
    title = {"model": "claude-opus-5-5", "max_tokens": 32, "tools": [],
             "messages": [{"role": "user", "content": "<session>Rédige le module 3</session>"}]}
    r = c.post("/v1/messages", json=title, headers={**CLAUDE, "x-app": "cli"})
    assert r.headers["x-sokkan-tier"] == "ship" and anthropic.seen == []
    assert ship.seen[0]["body"]["model"] == "qwen3.8-27b"


@pytest.mark.parametrize("grant,code", [
    ({"allowed": False, "code": "level", "reason": "classé confidentiel"}, "claude_forbidden"),
    ({"allowed": False, "code": "off", "reason": "off"}, "claude_forbidden"),
    ({"allowed": False, "code": "no_key", "reason": "pas de clé"}, "claude_no_key"),
    ({"allowed": True, "key": KEY, "models": ["claude-sonnet-5-5"]}, "claude_model"),
])
def test_a_refusal_is_readable_never_retried_and_nothing_leaves(box, grant, code, monkeypatch):
    c, ship, deep, api, anthropic = box
    monkeypatch.setenv("SOKKAN_ERROR_LANG", "fr")
    api.get_body = grant
    r = c.post("/v1/messages", json=ask(), headers=CLAUDE)
    assert r.status_code == llmerrors.HTTP_STATUS[code] == 403
    assert r.headers["x-should-retry"] == "false" and r.headers["x-sokkan-error"] == code
    msg = r.json()["error"]["message"]
    assert f"[sokkan:{code}]" in msg and "Rien n'a été envoyé" in msg and KEY not in r.text
    assert anthropic.seen == [] and ship.seen == [] and deep.seen == []   # no silent fall back


@pytest.mark.parametrize("break_it", ["api_down", "api_404", "no_secret"])
def test_when_the_right_cannot_be_checked_claude_is_refused(box, monkeypatch, break_it):
    c, ship, deep, api, anthropic = box
    if break_it == "api_down":
        monkeypatch.setenv("SOKKAN_ROUTER_API_URL", "http://127.0.0.1:9")
    elif break_it == "api_404":
        api.get_status = 404
    else:
        monkeypatch.delenv("SOKKAN_ROUTER_SECRET")
    r = c.post("/v1/messages", json=ask(), headers=CLAUDE)
    assert r.status_code == 503 and r.headers["x-sokkan-error"] == "claude_unavailable"
    assert r.headers["x-should-retry"] == "false"
    assert anthropic.seen == [] and ship.seen == [] and deep.seen == []


def test_a_refused_key_says_so_and_the_key_is_never_in_an_error_or_a_log(box, caplog):
    c, ship, deep, api, anthropic = box
    anthropic.status = 401
    with caplog.at_level(logging.DEBUG):
        r = c.post("/v1/messages", json=ask(), headers=CLAUDE)
    assert r.status_code == 401 and r.headers["x-sokkan-error"] == "claude_auth"
    assert KEY not in r.text and KEY not in caplog.text and SECRET not in caplog.text
    anthropic.status = 429
    r = c.post("/v1/messages", json=ask(), headers=CLAUDE)
    assert r.status_code == 429 and r.headers["x-should-retry"] == "false"
    assert ship.seen == [] and deep.seen == []                             # still no fall back


def test_a_refusal_is_not_cached_an_allowance_briefly(box):
    c, ship, deep, api, anthropic = box
    api.get_body = {"allowed": False, "code": "level", "reason": "x"}
    c.post("/v1/messages", json=ask(), headers=CLAUDE)
    api.get_body = {"allowed": True, "key": KEY, "models": ["claude-opus-5-5"]}
    assert c.post("/v1/messages", json=ask(), headers=CLAUDE).status_code == 200
    c.post("/v1/messages", json=ask(), headers=CLAUDE)
    assert len([x for x in api.seen if "claude-grant" in x["path"]]) == 2   # 2nd allowance from cache


# ---------------------------------------------------------------- the decision (instance side)

@pytest.fixture()
def inst(world, monkeypatch, tmp_path):  # noqa: F811
    import modelkeys
    import projects
    monkeypatch.setenv("SOKKAN_FEATURE_CLAUDE_TIER", "1")
    monkeypatch.setenv("SOKKAN_SHIP_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SOKKAN_ROUTER_SECRET", SECRET)
    monkeypatch.setenv("SOKKAN_MODELKEYS_STORE", str(tmp_path / "modelkeys.json"))
    for v in ("SOKKAN_CLAUDE_MODELS", "SOKKAN_CLAUDE_MODEL", "SOKKAN_CLAUDE_MAX_LEVEL"):
        monkeypatch.delenv(v, raising=False)
    projects.create("cours", "Contenus de cours", created_by="admin@x")
    projects.create("gestion", "Gestion", created_by="admin@x")
    for p in ("cours", "gestion"):
        projects.grant(p, "user", "bob@x", "dev")
    projects.set_level("cours", 0)          # public
    projects.set_level("gestion", 3)        # confidential
    modelkeys.set_key("instance", "anthropic", KEY, "admin@x")
    return world


def test_the_threshold_follows_the_project_classification(inst, monkeypatch):
    import projects
    assert claudetier.gate("cours")["allowed"] is True
    projects.set_level("cours", 1)                                          # team / internal
    assert claudetier.gate("cours")["allowed"] is True
    projects.set_level("cours", 2)                                          # project: the default
    g = claudetier.gate("cours")
    assert g["allowed"] is False and g["code"] == "level"
    g = claudetier.gate("gestion")
    assert g["allowed"] is False and g["code"] == "level" and "Confidentiel" in g["reason"]
    assert "Confidential" in claudetier.gate("gestion", lang="en")["reason"]
    # no variable opens confidential and above
    monkeypatch.setenv("SOKKAN_CLAUDE_MAX_LEVEL", "restricted")
    assert claudetier.max_level() == 2 and claudetier.gate("gestion")["allowed"] is False
    assert claudetier.gate("default")["allowed"] is True                    # a project never classified = 2
    monkeypatch.setenv("SOKKAN_CLAUDE_MAX_LEVEL", "public")
    projects.set_level("cours", 1)
    assert claudetier.gate("cours")["allowed"] is False


def test_no_key_feature_off_or_no_router_secret_are_said(inst, monkeypatch):
    import modelkeys
    modelkeys.delete_key("instance", "anthropic")
    assert claudetier.gate("cours")["code"] == "no_key"
    monkeypatch.delenv("SOKKAN_ROUTER_SECRET")
    assert claudetier.gate("cours")["code"] == "router"
    monkeypatch.setenv("SOKKAN_FEATURE_CLAUDE_TIER", "0")
    assert claudetier.gate("cours")["code"] == "off"


def test_the_grant_answers_the_router_only(inst):
    assert claudetier.grant("cours", "claude-opus-5-5", "wrong", False)[0] == 404
    assert claudetier.grant("cours", "claude-opus-5-5", SECRET, True)[0] == 404   # came through the proxy
    st, body = claudetier.grant("cours", "claude-opus-5-5", SECRET, False)
    assert st == 200 and body["allowed"] and body["key"] == KEY
    st, body = claudetier.grant("gestion", "claude-opus-5-5", SECRET, False)
    assert st == 200 and not body["allowed"] and "key" not in body
    st, body = claudetier.grant("cours", "claude-unknown-9", SECRET, False)
    assert not body["allowed"] and body["code"] == "model"
    c = inst["as"]("bob@x")
    r = c.get("/api/internal/claude-grant", params={"project": "cours"},
              headers={"x-sokkan-router-secret": SECRET, "x-forwarded-for": "10.0.0.9"})
    assert r.status_code == 404 and KEY not in r.text


def test_models_labels_default_and_what_the_key_sees(inst, monkeypatch):
    import modelkeys
    assert claudetier.label("claude-opus-5-5") == "Claude Opus 5.5"
    assert claudetier.label("claude-fable-5-2") == "Claude Fable 5.2"
    assert claudetier.label("claude-sonnet-5") == "Claude Sonnet 5"
    assert claudetier.models() == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"]
    assert claudetier.default_model() == "claude-opus-5-5"
    claudetier.set_default_model("claude-sonnet-5-5")
    assert claudetier.default_model() == "claude-sonnet-5-5"
    with pytest.raises(ValueError):
        claudetier.set_default_model("gpt-9")
    modelkeys._update("instance", "anthropic", seen_models=["claude-opus-5-5", "claude-haiku-5-5"])
    assert claudetier.available_models() == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert claudetier.default_model() == "claude-opus-5-5"                  # sonnet not seen by the key
    monkeypatch.setenv("SOKKAN_CLAUDE_MODELS", "claude-haiku-5-5")
    assert [m["id"] for m in claudetier.gate("cours")["models"]] == ["claude-haiku-5-5"]


def test_start_with_claude_on_a_public_project_and_refusal_on_a_confidential_one(inst):
    import hitl_journal
    import simple
    c = inst["as"]("bob@x", "cours")
    h = c.get("/api/simple/home").json()
    assert h["claude"]["allowed"] is True and h["claude"]["default_model"] == "claude-opus-5-5"
    assert "Anthropic" in h["claude"]["destination"] and "Suisse" in h["claude"]["destination"]
    r = c.post("/api/simple/start", json={"ask": "Rédige le module 3", "tier": "claude",
                                          "claude_model": "claude-sonnet-5-5"})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert claudetier.work_tier(sid) == ("claude", "claude-sonnet-5-5")
    w = c.get(f"/api/simple/sessions/{sid}").json()
    assert w["engine"]["tier"] == "claude" and w["engine"]["label"] == "Claude Sonnet 5.5"
    env = claudetier.session_env(sid, "cours", {"ANTHROPIC_CUSTOM_HEADERS": "x-sokkan-user: P1"})
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-5-5"
    assert env["ANTHROPIC_CUSTOM_HEADERS"].split("\n") == ["x-sokkan-user: P1", "x-sokkan-tier: claude",
                                                         "x-sokkan-project: cours"]
    # the journal of validations has the gesture
    rows = [x for x in hitl_journal.query(project="cours", limit=50) if x["kind"] == "engine"]
    assert len(rows) == 1 and rows[0]["decision"] == "claude" and rows[0]["model"] == "claude-sonnet-5-5"
    assert rows[0]["actor"] == "bob@x"
    # a confidential project: refused, readable, nothing started
    c = inst["as"]("bob@x", "gestion")
    assert c.get("/api/simple/home").json()["claude"]["allowed"] is False
    n = len(inst["turns"])
    r = c.post("/api/simple/start", json={"ask": "Relance le devis", "tier": "claude"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "claude_level"
    assert "Confidentiel" in r.json()["detail"]["message"] and len(inst["turns"]) == n
    # an ordinary work of the same project: on the model of the machine, no header
    sid2 = c.post("/api/simple/start", json={"ask": "Relance le devis"}).json()["session_id"]
    assert claudetier.work_tier(sid2) == ("", "") and claudetier.session_env(sid2, "gestion", {}) == {}
    assert simple.get_work(sid2)["tier"] == ""


def test_a_claude_work_recalls_nothing_above_what_claude_may_receive(inst):
    c = inst["as"]("bob@x", "cours")
    sid = c.post("/api/simple/start", json={"ask": "x", "tier": "claude"}).json()["session_id"]
    from core import scope as sc
    capped = sc.caps(claudetier.cap_scope(sid, ("cours@4", "shared@3"))) or {}
    assert capped == {"cours": 1, "shared": 1}
    other = c.post("/api/simple/start", json={"ask": "y"}).json()["session_id"]
    assert claudetier.cap_scope(other, ("cours@4",)) == ("cours@4",)


def test_project_level_route_needs_a_project_admin_and_a_reason_to_lower(inst):
    import projects
    projects.grant("gestion", "user", "admin@x", "admin")
    c = inst["as"]("bob@x", "gestion")
    assert c.put("/api/projects/gestion/level", json={"level": "public", "reason": "x"}).status_code == 403
    c = inst["as"]("admin@x", "gestion")
    r = c.put("/api/projects/gestion/level", json={"level": "public"})
    assert r.status_code == 400
    r = c.put("/api/projects/gestion/level", json={"level": "team", "reason": "contenus seulement"})
    assert r.status_code == 200 and r.json()["level"] == "team" and r.json()["claude"]["allowed"] is True
    assert inst["audit"]("project.level")[0]["detail"].startswith("confidential → team")


def test_the_key_entered_for_claude_never_becomes_the_engine_of_every_session(inst):
    import llm
    c = inst["as"]("admin@x")
    r = c.put("/api/claude/key", json={"key": "sk-ant-api03-another-key-for-test-000000"})
    assert r.status_code == 200 and "another" not in r.text
    assert llm.load().get("engine") == "appliance"                         # sessions stay on the box
    assert r.json()["key"]["masked"].endswith("0000")


# ---------------------------------------------------------------- the badge

def test_every_answer_and_deliverable_names_the_model_that_answered(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_SHIP_MODEL", "qwen3.8-27b")
    writes = []
    v = plain.View(deliv_dir=tmp_path, on_write=lambda n, t, m="": writes.append((n, t, m)) or 1)
    (t,) = v.feed({"type": "text", "text": "Voici le module.", "model": "claude-opus-5-5"})
    assert t["engine"] == {"model": "claude-opus-5-5", "label": "Claude Opus 5.5", "where": "anthropic",
                           "place": "Anthropic"}
    (t,) = v.feed({"type": "text", "text": "Bonjour", "model": "qwen3.8-27b"})
    assert t["engine"]["where"] == "local" and t["engine"]["label"] == "Modèle local"
    (t,) = v.feed({"type": "text", "text": "sans modèle"})
    assert "engine" not in t                                               # nothing invented
    v.feed({"type": "tool_use", "id": "w1", "tool": "Write", "model": "claude-sonnet-5-5",
            "input": {"file_path": str(tmp_path / "Module 3.md"), "content": "# Module 3"}})
    out = v.feed({"type": "tool_result", "tool_use_id": "w1", "text": "ok"})
    card = [e for e in out if e["type"] == "deliverable"][0]
    assert card["engine"]["label"] == "Claude Sonnet 5.5" and writes == [("Module 3.md", "w1", "claude-sonnet-5-5")]


def test_a_refusal_of_the_router_is_a_notice_in_the_persons_words():
    v = plain.View(lang="fr")
    line = "API Error: 403 " + json.dumps(llmerrors.anthropic_body("claude_forbidden", "en"))
    (n,) = v.feed({"type": "text", "text": line})
    assert n["type"] == "notice" and n["code"] == "claude"
    assert n["title"] == "Claude n'est pas autorisé pour ce projet" and "Rien n'a été envoyé" in n["detail"]


def test_saves_validations_and_export_carry_the_model(inst):
    import hitl_journal
    import simple
    import work_export
    c = inst["as"]("bob@x", "cours")
    sid = c.post("/api/simple/start", json={"ask": "Module 3", "tier": "claude"}).json()["session_id"]
    d = simple.deliverable_dir("cours", sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Module 3.md").write_text("# Module 3\n", encoding="utf-8")
    simple.note_write(sid, "Module 3.md", "w1", "claude-opus-5-5")
    (dl,) = simple.list_deliverables("cours", sid)
    assert dl["model"] == "claude-opus-5-5" and dl["engine"]["label"] == "Claude Opus 5.5"
    hitl_journal.requested("p1", "permission", session_id=sid, tool="Write", model="claude-opus-5-5")
    row = [x for x in hitl_journal.query(project="cours", limit=50) if x["request_id"] == "p1"][0]
    assert row["model"] == "claude-opus-5-5" and "model" in hitl_journal.to_csv([row]).splitlines()[0]
    import iam
    out = work_export.build(iam.get_user("bob@x"))
    import io
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(out["zip"]))
    idx = json.loads(z.read("deliverables/cours/index.json"))
    assert idx[0]["model"] == "claude-opus-5-5" and idx[0]["engine"] == "claude"
    assert idx[0]["written_with"] == "Claude Opus 5.5"
    assert KEY.encode() not in out["zip"]
