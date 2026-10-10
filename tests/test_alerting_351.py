"""SOKKAN 3.5.1 — Operate › Alerts, what the first day on sokkan.ninabot.ch showed (10.10.2026).

A channel test that hangs, a rule deleted while it rings that leaves its alert unresolved, a
Teams card that loses its details after Ack, a sentence that ignores `host!="raspberrypi"`, a
rule saved with nobody to tell, a history without unit, Teams channels typed as raw ids."""
import json
import time

import pytest

import test_alerting_api as _api

_rule_body = _api._rule_body
world = _api.world      # the shared fixture (people, projects, fake HTTP)


def _hook(c, url="https://hooks.example/351"):
    return c.post("/api/alerting/channels", json={"kind": "webhook", "name": "hook",
                                                  "config": {"url": url}}).json()


def _fire(world, c, **kw):
    import audit
    from alerting import scheduler
    rid = c.post("/api/alerting/rules", json=_rule_body(c, **kw)).json()["id"]
    t = time.time()
    scheduler.tick(t, force=True)
    audit.log("bob@x", "board.card.create", "#7", "", project="default")
    scheduler.tick(t + 61, force=True)
    return rid, c.get("/api/alerting/alerts").json()["alerts"][0]


def _events(world, url="https://hooks.example/351"):
    return [json.loads(p["content"])["event"] for p in world["posted"] if p["url"] == url]


# ---- 7. nobody to tell ------------------------------------------------------------------------
def test_an_active_rule_without_channel_is_refused_unless_said_on_purpose(world):
    c = world["as"]("admin@x")
    body = _rule_body(c)
    body.pop("no_notification")
    r = c.post("/api/alerting/rules", json=body)
    assert r.status_code == 422 and "nobody would be told" in r.text
    assert c.post("/api/alerting/rules", json={**body, "no_notification": True}).status_code == 201
    assert c.post("/api/alerting/rules", json={**body, "channels": [_hook(c)["id"]]}).status_code == 201


# ---- 4. deleted / turned off while it rings -----------------------------------------------------
@pytest.mark.parametrize("how", ["delete", "disable"])
def test_a_rule_deleted_or_turned_off_while_firing_resolves_and_tells(world, how):
    c = world["as"]("admin@x")
    ch = _hook(c)
    rid, a = _fire(world, c, channels=[ch["id"]], no_notification=False)
    assert a["state"] == "firing" and _events(world) == ["firing"]
    if how == "delete":
        assert c.delete(f"/api/alerting/rules/{rid}").json() == {"ok": True}
    else:
        assert c.post(f"/api/alerting/rules/{rid}/disable").status_code == 200
        h = c.get(f"/api/alerting/rules/{rid}/history").json()["transitions"]
        assert h[0]["to"] == "resolved" and "turned off" in h[0]["note"]
    assert _events(world) == ["firing", "resolved"]
    assert c.get("/api/alerting/alerts").json()["counts"]["firing"] == 0


# ---- 2. a channel test never hangs --------------------------------------------------------------
def test_a_channel_test_answers_within_its_limit(world, monkeypatch):
    from alerting import channels
    c = world["as"]("admin@x")
    ch = _hook(c)
    monkeypatch.setattr(channels, "TEST_LIMIT_S", 0.3)
    monkeypatch.setattr(channels, "deliver", lambda *a, **k: time.sleep(2) or "ok")
    t = time.time()
    r = c.post(f"/api/alerting/channels/{ch['id']}/test").json()
    assert time.time() - t < 1.5
    assert r["ok"] is False and r["timed_out"] is True and r["detail"].startswith("no answer after 0.3 s")


# ---- 5. the Teams card keeps its details after Ack ----------------------------------------------
def test_the_teams_card_keeps_value_host_and_severity_after_ack(world, monkeypatch):
    from alerting import channels
    from teams import bot, botauth, connector, store as tstore
    c = world["as"]("bob@x")
    _rid, a = _fire(world, c)
    monkeypatch.setattr(tstore, "linked_email", lambda aad, tenant: "bob@x")
    monkeypatch.setattr(botauth, "tenant_of", lambda act: "t")
    updated = []
    monkeypatch.setattr(connector, "update", lambda su, conv, aid, payload: updated.append(aid))
    import teams
    monkeypatch.setattr(teams, "enabled", lambda: True)
    channels._remember_card(a["id"], 9, "https://smba.trafficmanager.net/ch/", "19:c@thread.tacv2", "act-1")
    out = json.dumps(bot._on_action({"from": {"aadObjectId": "oid"}},
                                    {"verb": "alert.ack", "data": {"sokkan": "alert", "alert": a["id"]}}))
    assert "Acknowledged by bob@x" in out
    assert "Severity" in out and "critical" in out and "Card created" in out
    assert '"Ack"' not in out and "Silence 1 h" in out        # acked: no second Ack button
    assert updated == ["act-1"]                                  # the posted card follows


def test_a_resolved_alert_turns_its_posted_teams_card_green(world, monkeypatch):
    from alerting import channels
    import teams
    from teams import connector
    monkeypatch.setattr(teams, "enabled", lambda: True)
    sent, updated = [], []
    monkeypatch.setattr(connector, "update", lambda su, conv, aid, payload: updated.append(payload))
    monkeypatch.setattr(connector, "send", lambda su, conv, payload: sent.append(payload) or {"id": "x"})
    channels._remember_card(41, 3, "https://smba.trafficmanager.net/ch/", "19:c@thread.tacv2", "act-9")
    alert = {"id": 41, "severity": "warning", "group": {}, "summary": "Disk: 86 % > 85 %",
             "state": "resolved", "link": "/?plane=operate&tab=alerts&alert=41"}
    rule = {"id": 7, "name": "Disk", "sentence": "Alert when disk used is above 85 %"}
    assert channels._teams({"channel_id": "19:c@thread.tacv2", "_cid": 3}, {},
                           channels.message("resolved", alert, rule), alert, rule) == "ok"
    assert sent == [] and len(updated) == 1
    assert "Resolved" in json.dumps(updated[0]) and '"good"' in json.dumps(updated[0])


# ---- 6. the sentence says the label filters ----------------------------------------------------
def test_the_sentence_says_the_label_filters_with_host_names(monkeypatch):
    from alerting import engine, humanize
    monkeypatch.setattr(humanize, "_resolve", lambda a: "rpi1" if a.startswith("100.76.30.90") else None)
    src = {"id": 1, "kind": "prometheus", "name": "Prometheus"}

    def s(raw, **kw):
        b = {"name": "r", "source_id": 1, "query": {"mode": "raw", "raw": raw}, "type": "threshold",
             "params": {"op": "<", "value": 1}, "every": "1m", "for": "2m"}
        b.update(kw)
        return engine.normalize(b, src)["sentence"]
    assert s('up{host!="raspberrypi"}') == "Alert when a target is down, except host raspberrypi, for 2 minutes"
    one = s('up{instance="100.76.30.90:9100"}')
    assert one == "Alert when a target is down, on rpi1, for 2 minutes"
    mem = s('(1 - node_memory_MemAvailable_bytes{instance="100.76.30.90:9100"} / node_memory_MemTotal_bytes) * 100',
            params={"op": ">", "value": 85})
    assert mem.startswith("Alert when memory used is above 85 %, on rpi1")
    for x in (one, mem):
        assert "100.76" not in x and "(" not in x and ")" not in x
    b = engine.normalize({"name": "r", "source_id": 1, "type": "threshold", "params": {"op": "<", "value": 1},
                          "query": {"mode": "builder", "builder": {"metric": "up", "filters": [
                              {"label": "instance", "op": "!=", "value": "100.76.30.90:9100"}]}}}, src)
    assert b["sentence"] == "Alert when a target is down, except rpi1"


# ---- 9. the history speaks with the unit --------------------------------------------------------
def test_the_history_reads_old_transitions_with_the_rules_unit():
    from alerting import alerts
    row = {"ts": 1.0, "grp": "{}", "group_key": "", "from_state": "ok", "to_state": "firing",
           "value": 70.5, "threshold": "50", "note": ""}
    rule = {"unit": "%", "type": "threshold", "params": {"op": ">", "value": 50}}
    assert alerts._transition_public(row, rule)["value_text"] == "70.5 % > 50 %"
    row2 = {**row, "threshold": json.dumps({"op": ">", "value": 50, "type": "threshold"})}
    assert alerts._transition_public(row2, rule)["value_text"] == "70.5 % > 50 %"


# ---- 3. the Teams channels of the project, to pick from ----------------------------------------
def test_the_teams_channels_of_the_project_are_listed(world, monkeypatch):
    import features
    from teams import store as tstore
    real = features.enabled
    monkeypatch.setattr(features, "enabled", lambda name: True if name == "teams" else real(name))
    monkeypatch.setattr(tstore, "channels", lambda: [
        {"channel_id": "19:a@thread.tacv2", "project": "default", "name": "General", "level": 2},
        {"channel_id": "19:b@thread.tacv2", "project": "radio", "name": "Radio", "level": 2}])
    out = world["as"]("admin@x").get("/api/alerting/teams-channels").json()
    assert out["teams"] is True and out["channels"] == [{"id": "19:a@thread.tacv2", "name": "General", "level": 2}]
    monkeypatch.setattr(features, "enabled", real)
    assert world["as"]("admin@x").get("/api/alerting/teams-channels").json()["channels"] == []
