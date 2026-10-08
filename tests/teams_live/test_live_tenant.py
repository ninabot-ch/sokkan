"""Live checks against a real Microsoft 365 tenant (docs/enterprise/TEAMS.md § 6 and § 8).

Automatic (SOKKAN_TEAMS_LIVE=1): Bot Framework metadata, app tokens (Bot Connector, Graph),
Graph calendar / presence, the manifest, the instance's door, a proactive post + update in
the mapped channel. Guided (SOKKAN_TEAMS_LIVE_MANUAL=1, marker ``manual``): a person acts in
Teams, the test waits for the journal event of the instance.
"""
import base64
import datetime as dt
import json
import os
import time
from pathlib import Path

import pytest

from _live import need

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "teams"
TIMEOUT = int(os.environ.get("SOKKAN_TEAMS_LIVE_TIMEOUT_S", "300"))


def _claims(jwt: str) -> dict:
    body = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


# ---- Microsoft's side ------------------------------------------------------------------------
def test_bot_framework_openid_metadata_and_keys(live):
    import httpx
    import teams
    meta = httpx.get(teams.openid_url(), timeout=10).json()
    assert meta["issuer"] == teams.BOT_ISSUER
    keys = httpx.get(meta["jwks_uri"], timeout=10).json()["keys"]
    assert keys and all(k.get("kid") for k in keys)
    assert any("msteams" in (k.get("endorsements") or []) for k in keys), \
        "no key endorsed for msteams: botauth would refuse every Teams request"


def test_app_token_for_the_bot_connector(live):
    from teams import connector
    c = _claims(connector.app_token(connector.BOT_SCOPE))
    assert c["aud"] == "https://api.botframework.com"
    assert c["tid"] == live["SOKKAN_TEAMS_TENANT_ID"], "single-tenant bot: token of the tenant"
    assert c.get("appid", c.get("azp")) == live["SOKKAN_TEAMS_APP_ID"]


def test_app_token_for_graph_carries_only_the_granted_roles(live):
    from teams import connector
    roles = set(_claims(connector.app_token(connector.GRAPH_SCOPE)).get("roles") or [])
    allowed = {"Calendars.Read", "Presence.Read.All"}
    assert roles <= allowed, f"more Graph permissions than the doc asks for: {roles - allowed}"
    want = {r.strip() for r in os.environ.get("SOKKAN_TEAMS_LIVE_EXPECT_ROLES", "").split(",") if r.strip()}
    assert want <= roles, f"granted roles {roles}, expected {want} (admin consent?)"


def test_graph_calendar_of_a_test_user(live):
    v = need("SOKKAN_TEAMS_LIVE_USER")
    from teams import connector, graph
    if "Calendars.Read" not in (_claims(connector.app_token(connector.GRAPH_SCOPE)).get("roles") or []):
        pytest.skip("Calendars.Read not granted (optional)")
    ev = graph.GraphCalendar().events(v["SOKKAN_TEAMS_LIVE_USER"],
                                      dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0),
                                      dt.datetime.now(dt.timezone.utc).replace(hour=23, minute=59))
    assert isinstance(ev, list)
    print(f"calendarView: {len(ev)} event(s) today for the test user")


def test_graph_presence_of_a_test_user(live):
    v = need("SOKKAN_TEAMS_LIVE_USER_OID")
    from teams import connector, graph
    if "Presence.Read.All" not in (_claims(connector.app_token(connector.GRAPH_SCOPE)).get("roles") or []):
        pytest.skip("Presence.Read.All not granted (optional)")
    assert graph.presence(v["SOKKAN_TEAMS_LIVE_USER_OID"])


def test_manifest_of_this_app_is_valid(live):
    import jsonschema
    from teams import manifest
    v = need("SOKKAN_TEAMS_LIVE_INSTANCE")
    m = manifest.build(live["SOKKAN_TEAMS_APP_ID"], v["SOKKAN_TEAMS_LIVE_INSTANCE"])
    schema = json.loads((FIX / f"MicrosoftTeams.v{manifest.MANIFEST_VERSION}.schema.json").read_text())
    assert list(jsonschema.Draft4Validator(schema).iter_errors(m)) == []


# ---- the instance ----------------------------------------------------------------------------
def test_instance_refuses_an_unsigned_activity(instance):
    r = instance.post("/api/teams/messages", json={"type": "message", "channelId": "msteams"})
    assert r.status_code == 401


def test_instance_admin_state_is_ready(instance):
    need("SOKKAN_TEAMS_LIVE_ADMIN_COOKIE")
    st = instance.get("/api/admin/teams").json()
    assert st["enabled"] and st["missing"] == [], st
    assert st["channels"], "map a channel first (Setup › Organization › Teams)"
    print("last inbound activity:", json.dumps(st.get("last_inbound")))


def _service_url(instance) -> str:
    if os.environ.get("SOKKAN_TEAMS_LIVE_SERVICE_URL"):
        return os.environ["SOKKAN_TEAMS_LIVE_SERVICE_URL"]
    if os.environ.get("SOKKAN_TEAMS_LIVE_ADMIN_COOKIE"):
        last = (instance.get("/api/admin/teams").json().get("last_inbound") or {})
        if last.get("service_url"):
            return last["service_url"]
    from teams import proactive
    return proactive.DEFAULT_SERVICE_URL


def test_proactive_post_and_update_in_the_channel(live, instance):
    """What teams.proactive does: a new thread in the channel, then the message replaced."""
    v = need("SOKKAN_TEAMS_LIVE_CHANNEL")
    from teams import cards, connector
    surl, ch = _service_url(instance), v["SOKKAN_TEAMS_LIVE_CHANNEL"]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    res = connector.send(surl, ch, connector.card(
        cards.notice("SOKKAN live check", f"Proactive post at {stamp} — this card is replaced in "
                     "a few seconds."), "SOKKAN live check"))
    assert res.get("id"), res
    time.sleep(3)
    connector.update(surl, ch, res["id"], connector.card(
        cards.decided("SOKKAN live check", "closed", "", f"updated at {stamp} (PUT activity)"),
        "SOKKAN live check"))


# ---- guided: a person in Teams, the journal of the instance as the witness --------------------
def _wait_event(instance, action: str, since: float, what: str) -> dict:
    need("SOKKAN_TEAMS_LIVE_ADMIN_COOKIE")
    print(f"\n>>> {what}\n    waiting up to {TIMEOUT}s for « {action} » in the journal…", flush=True)
    end = time.time() + TIMEOUT
    while time.time() < end:
        for e in instance.get("/api/audit", params={"q": action, "limit": 20}).json():
            if e["action"] == action and e["ts"] >= since:
                return e
        time.sleep(3)
    pytest.fail(f"no « {action} » within {TIMEOUT}s")


@pytest.mark.manual
def test_manual_mention_status_in_the_channel(instance):
    t0 = time.time()
    e = _wait_event(instance, "teams.status", t0,
                    "In the mapped channel, write « @Nina status » (Teams desktop).")
    assert e["user"]                               # the sender's SOKKAN account
    last = instance.get("/api/admin/teams").json()["last_inbound"]
    print("claim names sent by Microsoft:", last["claims"])  # serviceurl casing (§ 7)
    assert {"aud", "iss", "exp"} <= set(last["claims"])


@pytest.mark.manual
def test_manual_approve_on_desktop_four_eyes(instance):
    t0 = time.time()
    e = _wait_event(instance, "teams.approval.approve", t0,
                    "Person A: « @Nina run <agent> ». Person A clicks Approve (must be refused: "
                    "four-eyes), then person B clicks Approve on Teams DESKTOP.")
    assert e["user"]


@pytest.mark.manual
def test_manual_approve_on_mobile(instance):
    t0 = time.time()
    _wait_event(instance, "teams.approval.approve", t0,
                "Again with a new « @Nina run <agent> », approved from Teams MOBILE (iOS/Android).")


@pytest.mark.manual
def test_manual_decision_capture(instance):
    t0 = time.time()
    e = _wait_event(instance, "teams.decision", t0,
                    "Reply in a thread: « @Nina note la décision : live check, ignore » — then "
                    "open the note in Control › CortHeXis and follow its thread link.")
    assert e["resource"]


@pytest.mark.manual
def test_manual_proactive_approval_from_the_cockpit(instance):
    t0 = time.time()
    _wait_event(instance, "teams.approval.post", t0,
                "In SOKKAN, propose an agent in the mapped project (Crew → + New agent, four-eyes "
                "on): its approval card must appear in the channel within a minute.")
    _wait_event(instance, "agent.approve", t0,
                "Approve it in the COCKPIT: the card in Teams must turn into « Approved by … ».")


# ---- 3.4.1 « Nina asks for help »: a real @mention in the mapped channel -----------------------
def test_outreach_message_with_a_real_mention_in_the_channel(live, instance):
    """What `teams.outreach.send` posts: a text message with a `mention` entity (Entra object id
    of SOKKAN_TEAMS_LIVE_USER_OID, name SOKKAN_TEAMS_LIVE_USER_NAME or the UPN's local part).
    Watch the channel: the name must render as a blue @mention that notifies the person."""
    v = need("SOKKAN_TEAMS_LIVE_CHANNEL", "SOKKAN_TEAMS_LIVE_USER_OID", "SOKKAN_TEAMS_LIVE_USER")
    from teams import outreach
    surl, ch = _service_url(instance), v["SOKKAN_TEAMS_LIVE_CHANNEL"]
    name = os.environ.get("SOKKAN_TEAMS_LIVE_USER_NAME") or v["SOKKAN_TEAMS_LIVE_USER"].split("@")[0]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = (f"<at>{name}</at>, could you help the SOKKAN live check with “outreach {stamp}”? "
            "(a test message of tests/teams_live — nothing to do)")
    res = outreach.post_message(surl, ch, text, {"id": v["SOKKAN_TEAMS_LIVE_USER_OID"], "name": name})
    assert res.get("id"), res
    link = outreach.thread_link(ch, res["id"])
    print("posted with a mention:", link)
    assert link.startswith("https://teams.microsoft.com/l/message/")
