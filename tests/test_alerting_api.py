"""SOKKAN 3.5 — Operate › Alerts through the real request path (middleware → projectgate →
route), the evaluation loop end to end on SOKKAN's own events, the external receiver, the MCP
proposal and the Teams card actions.

People: bob@x instance dev (dev of default) · carol@x viewer · admin@x admin (maintainer of
every project) · alice@x dev of `radio` only (SSO group) · dave@x another dev of default."""
import json
import time

import httpx
import pytest


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import audit
    import auth
    import board
    import iam
    import observability
    import projects
    import vault

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.delenv("SOKKAN_PROM", raising=False)
    monkeypatch.delenv("SOKKAN_LOKI", raising=False)
    monkeypatch.setenv("SOKKAN_ALERTING_EVALUATOR", "0")
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    for e, r in (("bob@x", "dev"), ("dave@x", "dev"), ("carol@x", "viewer"), ("admin@x", "admin")):
        iam.upsert_user(e, r)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(observability, "DB", str(tmp_path / "incidents.db"))
    projects.create("radio", "Radio player", created_by="admin@x")
    projects.sync_sso_groups("alice@x", ["radio-devs"])
    projects.grant("radio", "team", "sso:radio-devs", "dev")
    who = {"email": "bob@x"}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    c = TestClient(a.app)

    def as_(email, project=None):
        who["email"] = email
        c.headers.pop("x-sokkan-project", None)
        if project:
            c.headers["x-sokkan-project"] = project
        return c
    posted = []

    def post(url, json=None, content=None, headers=None, timeout=None):
        posted.append({"url": url, "json": json, "content": content, "headers": headers or {}})
        return httpx.Response(200, request=httpx.Request("POST", url))
    from alerting import channels, scheduler
    monkeypatch.setattr(channels.httpx, "post", post)
    monkeypatch.setattr(scheduler, "_pool", type("P", (), {"submit": staticmethod(lambda f, *x: f(*x))})())
    return {"as": as_, "tmp": tmp_path, "posted": posted}


def _audit(action):
    import audit
    return [e for e in audit.recent(500) if e["action"] == action]


def _sokkan_source(c):
    return next(s for s in c.get("/api/alerting/sources").json()["sources"] if s["kind"] == "sokkan")


def _rule_body(c, **kw):
    b = {"name": "Card created", "source_id": _sokkan_source(c)["id"], "type": "any", "params": {},
         "query": {"mode": "builder", "builder": {"metric": "audit", "filters": [
             {"field": "action", "op": "=", "value": "board.card.create"}]}},
         "every": "1m", "for": "0s", "severity": "critical"}
    b.update(kw)
    return b


def test_feature_off_is_404(world, monkeypatch):
    c = world["as"]("bob@x")
    monkeypatch.setenv("SOKKAN_FEATURE_ALERTING", "0")
    assert c.get("/api/alerting/rules").status_code == 404
    monkeypatch.delenv("SOKKAN_FEATURE_ALERTING")
    assert c.get("/api/alerting/rules").status_code == 200


def test_features_flag_drives_the_tab(world, monkeypatch):
    """The front shows Operate › Alerts only when /api/features says `alerting` — the flag was
    missing at the merge of the two branches, so the tab stayed hidden on every instance."""
    c = world["as"]("bob@x")
    assert c.get("/api/features").json()["alerting"] is True
    monkeypatch.setenv("SOKKAN_FEATURE_ALERTING", "0")
    assert c.get("/api/features").json()["alerting"] is False


def test_roles_viewer_reads_dev_writes_maintainer_manages(world):
    c = world["as"]("bob@x")
    r = c.post("/api/alerting/rules", json=_rule_body(c))
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["sentence"] == ("Alert on every one of the matching events (action=board.card.create)")
    assert rule["query_compiled"] == "audit action=board.card.create" and rule["state"]["state"] == "ok"
    assert _audit("alerting.rule.create")[0]["user"] == "bob@x"
    c = world["as"]("carol@x")
    assert c.get("/api/alerting/rules").json()["rules"][0]["id"] == rule["id"]
    assert c.post("/api/alerting/rules", json=_rule_body(c)).status_code == 403
    assert c.post(f"/api/alerting/rules/{rule['id']}/disable").status_code == 403
    c = world["as"]("dave@x")                        # another dev: not their rule
    assert c.put(f"/api/alerting/rules/{rule['id']}", json=_rule_body(c, name="x")).status_code == 403
    assert c.post("/api/alerting/channels", json={"kind": "slack", "config": {
        "webhook_url": "https://hooks.slack.com/x"}}).status_code == 403
    c = world["as"]("admin@x")
    assert c.put(f"/api/alerting/rules/{rule['id']}", json=_rule_body(c, name="Renamed")).json()["name"] == "Renamed"
    ch = c.post("/api/alerting/channels", json={"kind": "slack", "name": "ops",
                                                "config": {"webhook_url": "https://hooks.slack.com/T/B/xyz"}})
    assert ch.status_code == 201 and ch.json()["config"] == {"webhook_url_set": True}
    assert "hooks.slack.com" not in c.get("/api/alerting/channels").text


def test_another_projects_rules_do_not_exist(world):
    c = world["as"]("bob@x")
    rid = c.post("/api/alerting/rules", json=_rule_body(c)).json()["id"]
    c = world["as"]("alice@x", "radio")
    assert c.get("/api/alerting/rules").json()["rules"] == []
    assert c.get(f"/api/alerting/rules/{rid}").status_code == 404
    assert c.delete(f"/api/alerting/rules/{rid}").status_code == 404
    assert world["as"]("alice@x", "default").get("/api/alerting/rules").status_code == 404
    c = world["as"]("alice@x", "radio")
    assert c.post("/api/alerting/rules", json=_rule_body(c, channels=[999])).status_code == 422


def test_preview_and_templates(world):
    import audit
    for _ in range(3):
        audit.log("x@y", "board.card.create", "#1", "", project="default")
    c = world["as"]("bob@x")
    p = c.post("/api/alerting/preview", json={"rule": _rule_body(c), "range": "1h"}).json()
    assert p["error"] is None and p["kind"] == "events" and p["fires"] == 1
    assert p["sample_events"][0]["fields"]["action"] == "board.card.create"
    bad = c.post("/api/alerting/preview", json={"rule": _rule_body(c, type="absence"), "range": "1h"}).json()
    assert "works on metrics" in bad["error"]
    t = {x["id"]: x for x in c.get("/api/alerting/templates").json()["templates"]}
    assert t["agent-run-failed"]["available"] and not t["host-cpu"]["available"]
    r = c.post("/api/alerting/templates/login-refused/apply", json={"values": {"count": 3}}).json()["rule"]
    assert r["params"]["count"] == 3
    assert c.post("/api/alerting/templates/host-cpu/apply", json={}).status_code == 409


def test_the_loop_end_to_end_notifies_opens_the_incident_and_resolves(world):
    import audit
    import observability
    from alerting import scheduler
    c = world["as"]("admin@x")
    ch = c.post("/api/alerting/channels", json={"kind": "webhook", "name": "hook",
                                                "config": {"url": "https://hooks.example/a"}}).json()
    rid = c.post("/api/alerting/rules", json=_rule_body(
        c, channels=[ch["id"]], actions={"incident": True})).json()["id"]
    t = time.time()
    assert scheduler.tick(t, force=True) == 1
    assert c.get(f"/api/alerting/rules/{rid}").json()["state"]["state"] == "ok"
    audit.log("bob@x", "board.card.create", "#7", "", project="default")
    scheduler.tick(t + 61, force=True)
    st = c.get(f"/api/alerting/rules/{rid}").json()["state"]
    assert st["state"] == "firing" and st["firing"] == 1
    al = c.get("/api/alerting/alerts").json()
    assert al["counts"]["firing"] == 1
    a = al["alerts"][0]
    assert a["rule_name"] == "Card created" and a["incident_id"]
    hook = [p for p in world["posted"] if p["url"] == "https://hooks.example/a"]
    assert len(hook) == 1 and json.loads(hook[0]["content"])["event"] == "firing"
    inc = [i for i in observability.incidents() if i["id"] == a["incident_id"]][0]
    assert inc["title"] == "Card created"
    assert _audit("alerting.alert.firing")
    scheduler.tick(t + 122, force=True)                     # no new event: the « any » alert ends
    assert c.get(f"/api/alerting/rules/{rid}").json()["state"]["state"] == "ok"
    assert json.loads([p for p in world["posted"] if p["url"] == "https://hooks.example/a"][-1]["content"])["event"] == "resolved"
    h = c.get(f"/api/alerting/rules/{rid}/history").json()["transitions"]
    assert [x["to"] for x in h][:2] == ["resolved", "firing"]


def test_a_broken_source_puts_the_rule_in_error_without_stopping_the_loop(world, monkeypatch):
    from alerting import scheduler, sources
    c = world["as"]("admin@x")
    ok = c.post("/api/alerting/rules", json=_rule_body(c)).json()["id"]
    s = c.post("/api/alerting/sources", json={"kind": "prometheus", "name": "dead",
                                              "url": "http://127.0.0.1:9"}).json()
    bad = c.post("/api/alerting/rules", json={"name": "cpu", "source_id": s["id"], "type": "threshold",
                                              "params": {"op": ">", "value": 1},
                                              "query": {"mode": "raw", "raw": "up"}}).json()["id"]

    def refuse(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(sources.httpx, "request", refuse)
    assert scheduler.tick(time.time(), force=True) == 2
    st = c.get(f"/api/alerting/rules/{bad}").json()["state"]
    assert st["state"] == "error" and "cannot reach http://127.0.0.1:9" in st["error"]
    assert c.get(f"/api/alerting/rules/{ok}").json()["state"]["state"] == "ok"
    assert _audit("alerting.rule.error")
    assert c.delete(f"/api/alerting/sources/{s['id']}").status_code == 409     # used by a rule


def test_ack_silence_incident_from_the_cockpit(world):
    import audit
    from alerting import scheduler
    c = world["as"]("bob@x")
    rid = c.post("/api/alerting/rules", json=_rule_body(c)).json()["id"]
    t = time.time()
    scheduler.tick(t, force=True)
    audit.log("bob@x", "board.card.create", "#7", "", project="default")
    scheduler.tick(t + 61, force=True)
    aid = c.get("/api/alerting/alerts").json()["alerts"][0]["id"]
    assert world["as"]("carol@x").post(f"/api/alerting/alerts/{aid}/ack").status_code == 403
    c = world["as"]("bob@x")
    assert c.post(f"/api/alerting/alerts/{aid}/ack").json()["acked_by"] == "bob@x"
    s = c.post(f"/api/alerting/alerts/{aid}/silence", json={"for": "2h", "reason": "deploy"}).json()
    assert s["silenced_until"] > t + 7000
    assert c.get("/api/alerting/silences").json()["silences"][0]["reason"] == "deploy"
    r = c.post(f"/api/alerting/alerts/{aid}/open-incident").json()
    assert r["incident_id"] and c.post(f"/api/alerting/alerts/{aid}/open-incident").json()["incident_id"] == r["incident_id"]
    assert {e["action"] for e in audit.recent(100)} >= {"alerting.alert.ack", "alerting.alert.silence", "incident.open"}
    assert c.post("/api/alerting/silences", json={"for": "1h"}).status_code == 403   # project-wide: maintainer
    assert world["as"]("admin@x").post("/api/alerting/silences", json={"for": "1h"}).status_code == 201
    assert c.delete(f"/api/alerting/rules/{rid}").json() == {"ok": True}


def test_external_alerts_show_in_alerts(world, monkeypatch):
    import app as a
    monkeypatch.setattr(a, "_OBS_ALERT_TOKEN", "tok")
    monkeypatch.setattr(a, "_spawn_sdk", lambda *x, **k: {"session_id": "s" * 32})
    c = world["as"]("bob@x")
    body = {"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull", "instance": "db1"},
                        "annotations": {"summary": "93 % used"}}]}
    r = c.post("/api/observability/alert", json=body, headers={"authorization": "Bearer tok"})
    assert r.status_code == 200
    al = c.get("/api/alerting/alerts").json()["alerts"]
    assert al[0]["rule_name"] == "DiskFull" and al[0]["state"] == "firing" and al[0]["group"]["instance"] == "db1"
    rule = c.get(f"/api/alerting/rules/{al[0]['rule_id']}").json()
    assert rule["external"] == "external:DiskFull"
    assert c.put(f"/api/alerting/rules/{rule['id']}", json=_rule_body(c)).status_code == 409
    body["alerts"][0]["status"] = "resolved"
    c.post("/api/observability/alert", json=body, headers={"authorization": "Bearer tok"})
    assert c.get("/api/alerting/alerts").json()["alerts"] == []


def test_a_session_proposes_a_rule_disabled_and_a_person_enables_it(world, monkeypatch):
    import observability_mcp as m
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "default")
    monkeypatch.setenv("SOKKAN_SESSION_USER", "bob@x")
    out = json.loads(m.alerting_propose_rule({"name": "Proposed", "source_kind": "sokkan", "type": "any",
                                              "query": {"mode": "raw", "raw": "audit action=auth.*"}},
                                             why="sign-ins"))
    assert out["ok"] and "proposed" in out["status"]
    c = world["as"]("bob@x")
    r = c.get(f"/api/alerting/rules/{out['id']}").json()
    assert r["enabled"] is False and r["labels"]["proposed_by"] == "bob@x" and r["state"]["state"] == "disabled"
    assert c.post(f"/api/alerting/rules/{out['id']}/enable").json()["enabled"] is True
    assert _audit("alerting.rule.propose")
    p = json.loads(m.alerting_preview({"name": "x", "source_kind": "sokkan", "type": "any",
                                       "query": {"mode": "raw", "raw": "audit"}}, "1h"))
    assert p["error"] is None
    assert json.loads(m.alerting_list_rules())[0]["name"] == "Proposed"


def test_teams_card_actions_act_as_the_person(world, monkeypatch):
    import audit
    from alerting import scheduler
    from teams import bot, botauth, store as tstore
    c = world["as"]("bob@x")
    c.post("/api/alerting/rules", json=_rule_body(c))
    t = time.time()
    scheduler.tick(t, force=True)
    audit.log("bob@x", "board.card.create", "#7", "", project="default")
    scheduler.tick(t + 61, force=True)
    aid = c.get("/api/alerting/alerts").json()["alerts"][0]["id"]
    who = {"aad": "bob@x"}
    monkeypatch.setattr(tstore, "linked_email", lambda aad, tenant: who["aad"])
    monkeypatch.setattr(botauth, "tenant_of", lambda act: "t")
    act = {"from": {"aadObjectId": "oid"}}
    out = bot._on_action(act, {"verb": "alert.ack", "data": {"sokkan": "alert", "alert": aid}})
    assert "Acknowledged by bob@x" in json.dumps(out)
    who["aad"] = "carol@x"
    out = bot._on_action(act, {"verb": "alert.silence", "data": {"sokkan": "alert", "alert": aid}})
    assert "developer role" in json.dumps(out)
    who["aad"] = "alice@x"
    out = bot._on_action(act, {"verb": "alert.incident", "data": {"sokkan": "alert", "alert": aid}})
    assert "does not exist for you" in json.dumps(out)
    who["aad"] = "bob@x"
    out = bot._on_action(act, {"verb": "alert.incident", "data": {"sokkan": "alert", "alert": aid}})
    assert "Incident #" in json.dumps(out)
    assert _audit("teams.alert.ack") and _audit("teams.alert.incident")


def test_status_and_sources(world, monkeypatch):
    c = world["as"]("admin@x")
    st = c.get("/api/alerting/status").json()
    assert st["enabled"] and st["defaults"] == {"prometheus": False, "loki": False}
    s = c.post("/api/alerting/sources", json={"kind": "elasticsearch", "name": "logs",
                                              "url": "https://es.example:9200",
                                              "auth": {"kind": "basic", "user": "elastic", "secret": "pw-Very-Secret"},
                                              "options": {"index": "logs-*"}}).json()
    assert s["auth"] == {"kind": "basic", "user": "elastic", "secret_set": True}
    assert s["options"]["time_field"] == "@timestamp"
    assert "pw-Very-Secret" not in c.get("/api/alerting/sources").text
    assert c.post("/api/alerting/sources", json={"kind": "mysql", "url": "x"}).status_code == 422
    assert world["as"]("bob@x").post("/api/alerting/sources", json={
        "kind": "loki", "url": "http://loki:3100"}).status_code == 403


def test_the_morning_brief_lists_firing_alerts(world):
    import audit
    import helm
    from alerting import scheduler
    c = world["as"]("bob@x")
    c.post("/api/alerting/rules", json=_rule_body(c))
    t = time.time()
    scheduler.tick(t, force=True)
    audit.log("bob@x", "board.card.create", "#7", "", project="default")
    scheduler.tick(t + 61, force=True)
    b = helm.morning_brief("default")
    assert b["alerts"][0]["rule"] == "Card created" and b["alerts"][0]["severity"] == "critical"
    assert "## Alerts firing" in b["markdown"] and "critical: Card created" in b["markdown"]
    assert helm.morning_brief("radio")["alerts"] == []
