"""SOKKAN 3.4 « teams » — against the Graph / Bot Framework simulator (tests/teams_sim.py),
no app registration, no network.

The `radio` instance of test_classification (alice: dev cleared confidential; carol: dev;
max: maintainer cleared restricted), with the Teams users linked to these accounts the way
an Entra ID sign-in links them (oid → email)."""
import datetime as dt

import pytest

from teams_sim import APP_ID, CHANNEL, GRAPH, LOGIN, OPENID, TENANT, Sim
from test_classification import build_world

AAD = {"alice@x": "aad-alice", "carol@x": "aad-carol", "max@x": "aad-max"}


@pytest.fixture()
def tw(tmp_path, monkeypatch):
    import agents
    import teams
    from teams import botauth, store
    w = build_world(tmp_path, monkeypatch)
    sim = Sim()
    for k, v in {"FEATURE_TEAMS": "1", "FEATURE_ASSISTANT": "1", "TEAMS_APP_ID": APP_ID,
                 "TEAMS_APP_PASSWORD": "sim-secret", "TEAMS_TENANT_ID": TENANT,
                 "TEAMS_OPENID_URL": OPENID, "TEAMS_LOGIN_URL": LOGIN, "TEAMS_GRAPH_URL": GRAPH,
                 "TEAMS_PUBLIC_URL": "https://sokkan.example"}.items():
        monkeypatch.setenv(f"SOKKAN_{k}", v)
    monkeypatch.setenv("SOKKAN_TEAMS_DB", str(tmp_path / "teams.db"))
    monkeypatch.setenv("SOKKAN_TEAMS_KEY_FILE", str(tmp_path / "teams.key"))
    monkeypatch.setattr(teams, "enabled", lambda: True)
    monkeypatch.setattr(teams, "TRANSPORT", sim.transport())
    monkeypatch.setattr(agents, "_require_credentials", lambda: None)
    botauth.reset_cache()
    for email, aad in AAD.items():
        store.link_user(aad, TENANT, email)
    store.map_channel(CHANNEL, "radio", 2, "radio · General", "admin@x")
    # Nina: the system prompt is echoed so the tests see what she was given
    import assistant
    monkeypatch.setattr(assistant, "_llm_config", lambda: {"url": "x"})
    monkeypatch.setattr(assistant, "_fallback_config", lambda: None)
    monkeypatch.setattr(assistant, "_dossier", lambda: "")
    monkeypatch.setattr(assistant, "_persist", lambda *x: None)
    monkeypatch.setattr(assistant, "history", lambda *x, **k: [])

    class _C:
        def execute(self, *a):
            class R:
                def fetchone(self):
                    return (0,)
            return R()

        def close(self):
            pass
    monkeypatch.setattr(assistant, "_con", lambda: _C())
    monkeypatch.setattr(assistant, "_ask_with_fallback", lambda cfg, fb, system, msgs, user, state=None: (
        "notes: " + ",".join(n for n in ("radio-keys-rotation", "radio-runbook",
                                         "radio-incident-root-cause") if n in system), "primary"))
    c = w["c"]

    def post(activity, token=None, raw_auth=None):
        auth = raw_auth if raw_auth is not None else f"Bearer {token or sim.token()}"
        return c.post("/api/teams/messages", json=activity, headers={"authorization": auth})
    return {**w, "sim": sim, "post": post}


def _say(tw, who, text, **kw):
    sim = tw["sim"]
    n = len(sim.sent)
    r = tw["post"](sim.activity(text, AAD.get(who, who), **kw))
    assert r.status_code == 200, r.text
    return sim.sent[n:]


# ---- the door: every request is verified before anything is read --------------------------
@pytest.mark.parametrize("case", ["no-token", "other-key", "audience", "issuer", "expired",
                                  "tenant", "service-claim", "service-host", "channel",
                                  "unknown-kid"])
def test_requests_that_are_not_from_our_bot_in_our_tenant_are_refused(tw, case, monkeypatch):
    sim, post = tw["sim"], tw["post"]
    act = sim.activity("status", "aad-alice")
    tok = None
    if case == "no-token":
        r = post(act, raw_auth="")
    else:
        if case == "other-key":
            tok = sim.token(other_key=True)
        elif case == "audience":
            tok = sim.token(aud="another-app")
        elif case == "issuer":
            tok = sim.token(iss="https://evil.example")
        elif case == "expired":
            tok = sim.token(exp_in=-1000)
        elif case == "tenant":
            act = sim.activity("status", "aad-alice", tenant="ffffffff-0000-0000-0000-000000000000")
        elif case == "service-claim":
            tok = sim.token(service="https://smba.sim.botframework.com/other/")
        elif case == "service-host":
            act = sim.activity("status", "aad-alice", service="https://evil.example/")
            tok = sim.token(service="https://evil.example/")
        elif case == "channel":
            act["channelId"] = "slack"
        elif case == "unknown-kid":
            tok = sim.token(kid="nope")
        r = post(act, token=tok)
    assert r.status_code == 401
    assert sim.sent == []                         # nothing answered, nothing read


def test_feature_off_means_no_endpoint(tw, monkeypatch):
    import teams
    monkeypatch.setattr(teams, "enabled", lambda: False)
    assert tw["post"](tw["sim"].activity("status", "aad-alice")).status_code == 404


def test_an_unlinked_teams_user_gets_nothing_but_the_way_to_link(tw):
    out = _say(tw, "aad-stranger", "what is the HSM PIN rotation plan?")
    assert "Sign in once to https://sokkan.example" in out[0]["text"]
    assert "notes:" not in out[0]["text"]                # Nina was never asked


# ---- Nina answers as the identified person, capped by the channel ---------------------------
def test_nina_in_a_one_to_one_chat_answers_as_the_person(tw):
    q = "in radio: how do we do the key rotation plan?"
    a = _say(tw, "alice@x", q, conv_type="personal")[0]["text"]
    c = _say(tw, "carol@x", q, conv_type="personal")[0]["text"]
    assert a != c
    assert "radio-keys-rotation" in a and "classification: Confidential" in a
    assert "radio-keys-rotation" not in c and "radio-runbook" in c
    assert "radio-incident-root-cause" not in a
    assert ("teams", "alice@x", "radio-keys-rotation") in tw["st"].access     # audited


def test_in_a_channel_nina_never_says_more_than_the_channel_may_hear(tw):
    a = _say(tw, "alice@x", "how do we do the key rotation plan?")[0]["text"]
    assert "radio-keys-rotation" not in a                       # channel level = project
    from teams import store
    store.map_channel(CHANNEL, "radio", 3, "radio · Security")
    a = _say(tw, "alice@x", "how do we do the key rotation plan?")[0]["text"]
    assert "radio-keys-rotation" in a
    c = _say(tw, "carol@x", "how do we do the key rotation plan?")[0]["text"]
    assert "radio-keys-rotation" not in c                       # carol's own clearance


def test_unmapped_channel_and_no_role(tw):
    out = _say(tw, "alice@x", "status", channel="19:other@thread.tacv2")
    assert "not linked to a SOKKAN project" in out[0]["text"]
    import classification
    import iam
    iam.upsert_user("zed@x", "viewer")
    from teams import store
    store.link_user("aad-zed", TENANT, "zed@x")
    assert classification.clearance(iam.get_user("zed@x"), "radio") is None
    out = _say(tw, "aad-zed", "status")
    assert "no access to the project" in out[0]["text"]


def test_status_card_within_the_clearance(tw):
    card = _say(tw, "alice@x", "status")[0]["attachments"][0]["content"]
    facts = {f["title"]: f["value"] for f in card["body"][1]["facts"]}
    assert facts["Backlog"] == "1"                     # the confidential card is not counted
    from teams import store
    store.map_channel(CHANNEL, "radio", 3)
    card = _say(tw, "alice@x", "status")[0]["attachments"][0]["content"]
    assert {f["title"]: f["value"] for f in card["body"][1]["facts"]}["Backlog"] == "2"


def test_create_a_card_and_capture_a_decision(tw):
    import board
    from teams import store
    store.map_channel(CHANNEL, "radio", 3, "radio · Security")
    out = _say(tw, "carol@x", "card: rotate the HSM keys before Friday")[0]["text"]
    assert "created" in out
    c = board.search_cards("HSM", project="radio")[0]
    assert c["level"] == 3                             # inherited from the channel
    out = _say(tw, "alice@x", "note la décision : on garde Postgres 16 jusqu'en mars")[0]["text"]
    assert "Decision noted" in out
    d = tw["tmp"] / "projects" / "radio" / "memory"
    f = next(d.glob("decision_*.md"))
    text = f.read_text()
    assert "classification: confidential" in text and "type: decision" in text
    assert 'author: "alice@x"' in text and "teams.microsoft.com/l/message/" in text
    assert "on garde Postgres 16 jusqu'en mars" in text
    assert tw["st"].levels_set[-1][2] == 3             # floor: an edit cannot lower it


# ---- approvals: signed, single use, bound to the right person -----------------------------
def _agent(tw, owner="carol@x"):
    import agents
    import projectgate
    import classification
    pu = projectgate.project_user(classification.user_for(owner), "radio")
    return agents.create(pu, {"name": "radio-check", "purpose": "check the player", "deliverable": "d",
                              "trigger": "manual"}, activate=True)


def _execute(tw, who, token, verb="approve"):
    sim = tw["sim"]
    act = sim.activity("", AAD.get(who, who), kind="invoke", name="adaptiveCard/action",
                       value={"action": {"type": "Action.Execute", "verb": verb,
                                         "data": {"sokkan": "approval", "token": token}}})
    r = tw["post"](act)
    assert r.status_code == 200
    return r.json()


def test_run_proposal_approval_is_single_use_and_four_eyes(tw, monkeypatch):
    import agents
    a = _agent(tw)                                     # active (owner approval)
    monkeypatch.setenv("SOKKAN_FEATURE_FOUR_EYES", "1")
    card = _say(tw, "carol@x", "run radio-check")[0]["attachments"][0]["content"]
    tok = card["actions"][0]["data"]["token"]
    assert [x["verb"] for x in card["actions"]] == ["approve", "refuse"]
    r = _execute(tw, "carol@x", tok)                   # the requester cannot approve
    assert "another person" in r["value"]
    r = _execute(tw, "max@x", tok)
    assert r["type"] == "application/vnd.microsoft.card.adaptive"
    assert "Approved by max@x" in str(r["value"])
    assert agents.active_run(a["id"]) is not None
    r = _execute(tw, "alice@x", tok)                   # single use
    assert "already approve by max@x" in r["value"]


def test_tampered_expired_or_misaddressed_approvals_are_refused(tw):
    from teams import signing
    a = _agent(tw)
    tok = signing.issue("agent.run", str(a["id"]), "radio", "carol@x")
    body, sig = tok.split(".")
    assert "bad signature" in _execute(tw, "max@x", body + "." + sig[::-1])["value"]
    old = signing.issue("agent.run", str(a["id"]), "radio", "carol@x", ttl=-5)
    assert "expired" in _execute(tw, "max@x", old)["value"]
    bound = signing.issue("agent.run", str(a["id"]), "radio", "carol@x", approver_aad="aad-max")
    assert "someone else" in _execute(tw, "alice@x", bound)["value"]
    assert "Approved" in str(_execute(tw, "max@x", bound)["value"])
    assert "Sign in" in _execute(tw, "aad-stranger", tok)["value"]


def test_pending_agent_approvals_as_cards(tw, monkeypatch):
    import agents
    import classification
    import projectgate
    monkeypatch.setenv("SOKKAN_FEATURE_FOUR_EYES", "1")
    pu = projectgate.project_user(classification.user_for("carol@x"), "radio")
    a = agents.create(pu, {"name": "radio-nightly", "purpose": "p", "deliverable": "d",
                           "trigger": "manual"}, proposal=True)
    assert agents.get(a["id"])["status"] == "pending"
    mine = _say(tw, "carol@x", "approvals")[0]["attachments"][0]["content"]
    t0 = mine["actions"][0]["data"]["token"]
    assert "Refused for you" in _execute(tw, "carol@x", t0)["value"]   # proposer: four-eyes
    assert "Approved by max@x" in str(_execute(tw, "max@x", t0)["value"])  # token given back
    assert agents.get(a["id"])["status"] == "active"


# ---- outbound: tokens encrypted at rest, Graph calendar behind the `calendars` interface ----
def test_outbound_tokens_are_cached_encrypted(tw):
    _say(tw, "alice@x", "status")
    import sqlite3
    from teams import store
    rows = sqlite3.connect(store.db_path()).execute("SELECT value_enc FROM token_cache").fetchall()
    assert rows and all(not r[0].startswith("tok-") for r in rows)
    calls = len(tw["sim"].token_calls)
    _say(tw, "alice@x", "status")
    assert len(tw["sim"].token_calls) == calls        # cached
    assert tw["sim"].token_calls[0]["scope"] == "https://api.botframework.com/.default"


def test_calendar_through_graph_for_the_brief(tw, monkeypatch):
    import calendars
    tw["sim"].events["alice@x"] = [{"subject": "Release review", "showAs": "busy",
                                    "start": {"dateTime": "2026-10-08T07:30:00.0000000"},
                                    "end": {"dateTime": "2026-10-08T08:00:00.0000000"},
                                    "isOnlineMeeting": True,
                                    "organizer": {"emailAddress": {"address": "max@x"}}}]
    ev = calendars.events_for("alice@x", dt.date(2026, 10, 8))
    assert [(e.subject, e.start, e.online, e.organizer) for e in ev] == [
        ("Release review", "2026-10-08T07:30:00Z", True, "max@x")]
    assert "calendarView" in tw["sim"].graph_calls[-1] and "startDateTime=2026-10-08" in \
        tw["sim"].graph_calls[-1]
    from teams import graph
    assert graph.presence("aad-alice") == "Busy"
    import teams
    monkeypatch.setattr(teams, "enabled", lambda: False)
    assert calendars.provider() is None and calendars.events_for("alice@x") == []


def test_admin_maps_channels_and_gets_the_manifest(tw):
    adm = tw["as"]("admin@x", "default")
    r = adm.put("/api/admin/teams/channels", json={"channel_id": "19:sec@thread.tacv2",
                                                    "project": "radio", "level": "confidential"})
    assert r.status_code == 200
    assert any(c["channel_id"] == "19:sec@thread.tacv2" and c["level"] == 3
               for c in r.json()["channels"])
    assert adm.put("/api/admin/teams/channels", json={"channel_id": "x", "project": "nope"}
                   ).status_code == 400
    m = adm.get("/api/admin/teams/manifest").json()
    assert m["bots"][0]["botId"] == APP_ID and m["validDomains"] == ["sokkan.example"]
    st = adm.get("/api/admin/teams").json()
    assert st["missing"] == [] and st["tenant"] == TENANT
    assert tw["as"]("alice@x").get("/api/admin/teams").status_code == 403
