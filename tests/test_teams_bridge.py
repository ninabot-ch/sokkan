"""SOKKAN 3.4.0 « Bridge » — Teams ready to plug in (no tenant): the app package, the
Adaptive Cards against the 1.5 schema, card refresh and the Action.Submit fallback, and the
PROACTIVE push of pending approvals to the project's channel (tests/teams_sim.py)."""
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import jsonschema
import pytest

from teams_sim import APP_ID, CHANNEL, SERVICE, TENANT
from test_teams import AAD, _execute, _say, make_tw

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "teams"
AC_SCHEMA = json.loads((FIX / "adaptive-card-1.5.schema.json").read_text())
MS_SCHEMA = json.loads((FIX / "MicrosoftTeams.v1.17.schema.json").read_text())


@pytest.fixture()
def tw(tmp_path, monkeypatch):
    return make_tw(tmp_path, monkeypatch)


def _ac_errors(card: dict) -> list[str]:
    c = {k: v for k, v in card.items() if k != "msteams"}   # Teams host property only
    return [e.message for e in jsonschema.Draft6Validator(AC_SCHEMA).iter_errors(c)]


# ---- Adaptive Cards 1.5 ---------------------------------------------------------------------
def test_every_card_conforms_to_adaptive_cards_1_5():
    from teams import cards
    all_cards = [cards.approval("Run the agent x?", [("Project", "radio")], "tok", "note",
                                "https://sokkan.example/?tab=crew", ["29:1"]),
                 cards.decided("Run the agent x?", "approve", "max@x", "run #3 queued"),
                 cards.decided("t", "closed", "", "expired"),
                 cards.notice("An approval waits", "level confidential", "https://s.example"),
                 cards.status("radio", {"Backlog": 1}, ["a · run #1 · done"], 2, "Project")]
    for c in all_cards:
        assert c["version"] == "1.5" and c["msteams"] == {"width": "Full"} and c["fallbackText"]
        assert _ac_errors(c) == [], c
    # the schema is strict: an unknown property is caught (the test does test something)
    bad = cards.approval("x", [], "tok")
    bad["body"][0]["colour"] = "red"
    assert _ac_errors(bad)


def test_approval_card_follows_the_universal_action_model():
    from teams import cards
    c = cards.approval("Run?", [("Project", "radio")], "TOK", open_url="https://s.example/x")
    assert "actions" not in c                        # every action inside an ActionSet
    sets = [e for e in c["body"] if e["type"] == "ActionSet"]
    assert len(sets) == 1
    ex = [a for a in sets[0]["actions"] if a["type"] == "Action.Execute"]
    assert [a["verb"] for a in ex] == ["approve", "refuse"]
    for a in ex:
        assert a["data"] == {"sokkan": "approval", "token": "TOK"}
        fb = a["fallback"]                            # older clients: Submit with the verb
        assert fb["type"] == "Action.Submit" and fb["data"]["verb"] == a["verb"]
        assert fb["data"]["token"] == "TOK"
    assert c["refresh"]["action"] == {"type": "Action.Execute", "verb": "refresh",
                                      "data": {"sokkan": "approval", "token": "TOK"}}
    assert "userIds" not in c["refresh"]              # ≤ 60 members: automatic; else manual
    many = cards.approval("Run?", [], "T", user_ids=[f"29:{i}" for i in range(80)])
    assert len(many["refresh"]["userIds"]) == 60


def test_submit_fallback_and_refresh(tw, monkeypatch):
    """An old client posts the Submit fallback as a message; refresh shows the current
    state to anyone looking at the card, without deciding anything."""
    from teams import signing
    from teams.cards import actions_of
    from test_teams import _agent
    a = _agent(tw)
    card = _say(tw, "carol@x", "run radio-check")[0]["attachments"][0]["content"]
    tok = actions_of(card)[0]["data"]["token"]
    sim = tw["sim"]
    # refresh before the decision: the same approval, same token
    r = _execute(tw, "aad-stranger", tok, verb="refresh")
    assert r["type"] == "application/vnd.microsoft.card.adaptive"
    assert actions_of(r["value"])[0]["data"]["token"] == tok
    assert "Run the agent radio-check?" in json.dumps(r["value"])
    # the Action.Submit fallback (message with value) by max
    fb = actions_of(card)[0]["fallback"]["data"]
    n = len(sim.sent)
    act = sim.activity("", AAD["max@x"], value=fb)
    assert tw["post"](act).status_code == 200
    assert "Approved by max@x" in sim.sent[n]["text"] or "Done" in sim.sent[n]["text"] \
        or "queued" in json.dumps(sim.sent[n])
    import agents
    assert agents.active_run(a["id"]) is not None
    # refresh after: the decided card, for everyone
    r = _execute(tw, "aad-stranger", tok, verb="refresh")
    assert "Approved by max@x" in json.dumps(r["value"])
    # an expired, unused approval refreshes as expired
    old = signing.issue("agent.run", str(a["id"]), "radio", "carol@x", ttl=-5,
                        card={"title": "Old", "facts": []})
    assert "expired" in json.dumps(_execute(tw, "max@x", old, verb="refresh")["value"])


# ---- the app package ------------------------------------------------------------------------
def test_manifest_validates_against_the_v1_17_schema_and_stays_minimal():
    from teams import manifest
    m = manifest.build(APP_ID, "https://sokkan.example.ch/")
    assert list(jsonschema.Draft4Validator(MS_SCHEMA).iter_errors(m)) == []
    assert m["validDomains"] == ["sokkan.example.ch"] and m["id"] == m["bots"][0]["botId"] == APP_ID
    assert m["bots"][0]["scopes"] == ["personal", "team", "groupChat"]
    assert "webApplicationInfo" not in m              # only with --sso
    s = manifest.build(APP_ID, "https://sokkan.example.ch", sso=True)
    assert s["webApplicationInfo"] == {"id": APP_ID, "resource": f"api://sokkan.example.ch/{APP_ID}"}
    assert list(jsonschema.Draft4Validator(MS_SCHEMA).iter_errors(s)) == []
    for bad_id, url in (("not-a-guid", "https://s.example.ch"), (APP_ID, "http://s.example.ch"),
                        (APP_ID, "https://localhost"), ("", "https://s.example.ch")):
        with pytest.raises(manifest.ManifestError):
            manifest.build(bad_id, url)


def _png_size(b: bytes) -> tuple[int, int]:
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    import struct
    return struct.unpack(">II", b[16:24])


def test_package_icons_and_the_script_share_one_source(tmp_path, tw):
    from teams import manifest
    z = zipfile.ZipFile(io.BytesIO(manifest.package(manifest.build(APP_ID, "https://s.example.ch"))))
    assert sorted(z.namelist()) == ["color.png", "manifest.json", "outline.png"]
    assert _png_size(z.read("color.png")) == (192, 192)
    assert _png_size(z.read("outline.png")) == (32, 32)
    # outline: white on transparent only (Teams' rule)
    import zlib
    raw = z.read("outline.png")
    data = zlib.decompress(raw[raw.index(b"IDAT") + 4:raw.index(b"IEND") - 8])
    px = {data[r * 129 + 1 + i * 4: r * 129 + 5 + i * 4] for r in range(32) for i in range(32)}
    assert px <= {b"\xff\xff\xff\xff", b"\x00\x00\x00\x00"} and len(px) == 2
    # the script writes the manifest the admin route serves
    env = {"PATH": "/usr/bin:/bin", "SOKKAN_DATA_DIR": str(tmp_path),
           "SOKKAN_TEAMS_APP_ID": APP_ID, "SOKKAN_PUBLIC_URL": "https://sokkan.example"}
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "teams-manifest.py"), "--check",
                          "--out", str(tmp_path / "pkg")], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    script_m = json.loads((tmp_path / "pkg" / "manifest.json").read_text())
    adm = tw["as"]("admin@x", "default")
    assert adm.get("/api/admin/teams/manifest").json() == script_m
    r = adm.get("/api/admin/teams/package")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("manifest.json")) == script_m
    assert tw["as"]("alice@x").get("/api/admin/teams/package").status_code == 403


def test_register_script_portal_mode_needs_no_azure(tmp_path):
    """Default: the Developer Portal path (a Business tenant has no Azure subscription)."""
    sh = ROOT / "scripts" / "teams-register.sh"
    env = {"PATH": "/usr/bin:/bin"}                   # no az anywhere
    out = subprocess.run(["bash", str(sh), "--public-url", "https://sokkan.example.ch", "--presence"],
                         capture_output=True, text=True, cwd=tmp_path, env=env)
    assert out.returncode == 0, out.stderr
    assert "dev.teams.microsoft.com" in out.stderr and "Bot management" in out.stderr
    assert "https://sokkan.example.ch/api/teams/messages" in out.stderr
    assert "Presence.Read.All" in out.stderr and "Calendars.Read" not in out.stderr
    assert out.stdout == ""                           # nothing to paste before the bot id
    out = subprocess.run(["bash", str(sh), "--public-url", "https://sokkan.example.ch",
                          "--app-id", APP_ID, "--tenant-id", TENANT, "--out", str(tmp_path / "p")],
                         capture_output=True, text=True, cwd=tmp_path,
                         env={**env, "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert f"SOKKAN_TEAMS_APP_ID={APP_ID}" in out.stdout and f"SOKKAN_TEAMS_TENANT_ID={TENANT}" in out.stdout
    assert "SOKKAN_TEAMS_APP_PASSWORD_FILE=" in out.stdout
    assert (tmp_path / "p" / "sokkan-teams-app.zip").exists()


def test_register_script_azure_dry_run_prints_and_changes_nothing(tmp_path):
    sh = ROOT / "scripts" / "teams-register.sh"
    out = subprocess.run(["bash", str(sh), "--mode", "azure", "--public-url", "https://sokkan.example.ch",
                          "-g", "rg-poc", "--calendar", "--dry-run",
                          "--secret-file", str(tmp_path / "secret")],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    err = out.stderr
    assert "--sign-in-audience AzureADMultipleOrgs" in err and "--app-type MultiTenant" in err   # 3.4.1: the Bot Framework issuer
    assert "https://sokkan.example.ch/api/teams/messages" in err
    assert "az bot msteams create" in err and "admin-consent" in err
    assert "Calendars.Read" in err and "Presence.Read.All" not in err
    assert "SOKKAN_TEAMS_APP_ID=<app-id>" in out.stdout
    assert not (tmp_path / "secret").exists()
    bad = subprocess.run(["bash", str(sh), "--mode", "azure", "--public-url", "http://x", "--dry-run"],
                         capture_output=True, text=True, cwd=tmp_path)
    assert bad.returncode == 2


# ---- proactive approvals ----------------------------------------------------------------------
@pytest.fixture()
def pro(tw, monkeypatch):
    from teams import proactive
    monkeypatch.setattr(proactive, "DEBOUNCE_S", 0)            # hooks sync inline
    # no verified activity seen yet in the channel: the configured serviceUrl
    monkeypatch.setenv("SOKKAN_TEAMS_SERVICE_URL", SERVICE)
    monkeypatch.setenv("SOKKAN_FEATURE_FOUR_EYES", "1")
    return tw


def _proposal(owner="carol@x", name="radio-nightly"):
    import agents
    import classification
    import projectgate
    pu = projectgate.project_user(classification.user_for(owner), "radio")
    return agents.create(pu, {"name": name, "purpose": "nightly check of the player",
                              "deliverable": "d", "trigger": "manual"}, proposal=True)


def _posts(sim, method="POST"):
    return [m for m in sim.sent if m.get("method") == method
            and m["path"].endswith(f"/v3/conversations/{CHANNEL}/activities")]


def test_a_new_proposal_is_posted_once_in_the_project_channel(pro):
    from teams import proactive
    from teams.cards import actions_of
    sim = pro["sim"]
    a = _proposal()                                  # agents.create → poke → sync
    posts = _posts(sim)
    assert len(posts) == 1
    card = posts[0]["attachments"][0]["content"]
    assert card["body"][0]["text"] == "Activate the agent radio-nightly?"
    assert _ac_errors(card) == []
    assert any(x["type"] == "Action.OpenUrl" and x["url"].endswith(f"agent={a['id']}")
               for x in actions_of(card))
    assert proactive.sync()["posted"] == 0            # idempotent
    assert len(_posts(sim)) == 1
    import sqlite3
    from teams import store
    row = sqlite3.connect(store.db_path()).execute(
        "SELECT item, state, activity_id FROM proactive").fetchone()
    assert row[0] == f"agent.activate:{a['id']}" and row[1] == "open" and row[2]


def test_decided_in_the_cockpit_the_card_is_replaced_and_its_token_spent(pro):
    import agents
    import classification
    import projectgate
    from teams.cards import actions_of
    sim = pro["sim"]
    a = _proposal()
    tok = actions_of(_posts(sim)[0]["attachments"][0]["content"])[0]["data"]["token"]
    pu = projectgate.project_user(classification.user_for("max@x"), "radio")
    agents.approve(pu, a["id"])                       # in the cockpit → poke → sync
    puts = [m for m in sim.sent if m.get("method") == "PUT"]
    assert len(puts) == 1 and puts[0]["path"].endswith(_posts(sim)[0].get("id", "") or "")
    assert "Approved by max@x" in json.dumps(puts[0]) and "decided in SOKKAN" in json.dumps(puts[0])
    r = _execute(pro, "alice@x", tok)                 # a late click on the old card
    assert "already" in r["value"]
    assert agents.get(a["id"])["status"] == "active"


def test_decided_in_teams_from_the_pushed_card(pro):
    import agents
    from teams.cards import actions_of
    sim = pro["sim"]
    a = _proposal()
    tok = actions_of(_posts(sim)[0]["attachments"][0]["content"])[0]["data"]["token"]
    assert "Refused for you" in _execute(pro, "carol@x", tok)["value"]   # proposer: four-eyes
    r = _execute(pro, "max@x", tok)
    assert "Approved by max@x" in json.dumps(r["value"])
    assert agents.get(a["id"])["status"] == "active"
    puts = [m for m in sim.sent if m.get("method") == "PUT"]           # everyone's copy follows
    assert len(puts) == 1 and "Approved by max@x" in json.dumps(puts[0])


def test_channel_without_approvals_or_unmapped_project_gets_nothing(pro):
    from teams import store
    store.map_channel(CHANNEL, "radio", 2, "radio · General", approvals=False)
    _proposal()
    assert _posts(pro["sim"]) == []
    store.unmap_channel(CHANNEL)
    store.map_channel("19:other@thread.tacv2", "default", 2)
    _proposal(name="radio-two")
    assert pro["sim"].sent == []


def test_service_url_remembered_from_the_channel_and_failures_retried(pro, monkeypatch):
    from teams import proactive
    sim = pro["sim"]
    # the configured fallback would fail (no such route in the simulator): the remembered
    # serviceUrl of the channel is used
    monkeypatch.setenv("SOKKAN_TEAMS_SERVICE_URL", "https://smba.trafficmanager.net/teams/")
    _say(pro, "alice@x", "status")                    # a verified activity of the channel
    from teams import store
    assert store.get_meta("last_inbound")["claims"] == sorted(["aud", "exp", "iss", "nbf",
                                                                "serviceurl"])
    sim.sent.clear()
    sim.fail_connector = True
    _proposal()
    assert _posts(sim) == [] and proactive.state()["errors"]
    sim.fail_connector = False
    assert proactive.sync()["posted"] == 1            # retried
    assert _posts(sim)[0]["path"].startswith("/emea/")   # SERVICE of the simulator
    assert SERVICE.endswith("/emea/")


def test_unknown_channel_uses_the_configured_service_url(pro, monkeypatch):
    monkeypatch.setenv("SOKKAN_TEAMS_SERVICE_URL", "https://evil.example/")
    from teams import proactive
    _proposal()                                       # not a Microsoft host: never sent
    assert pro["sim"].sent == [] and "not a Microsoft host" in " ".join(proactive.state()["errors"])


class _FakeSession:
    """A live agent-run session waiting on one tool approval (agentchat)."""

    def __init__(self):
        self._perms = {"perm-1": object()}
        self.events = [{"type": "permission", "id": "perm-1", "tool": "Bash",
                        "title": "Bash: rotate the HSM keys"}]
        self.resolved = []

    def resolve_permission(self, pid, decision):
        self.resolved.append((pid, decision))
        self._perms.pop(pid, None)


def _waiting_run(pro, monkeypatch, level=2):
    import agentchat
    import agents
    import agents_runtime
    import classification
    import projectgate
    from test_teams import _agent
    a = _agent(pro)                                   # four-eyes: pending, posted…
    agents.approve(projectgate.project_user(classification.user_for("max@x"), "radio"), a["id"])
    pro["sim"].sent.clear()                           # …and closed: only the run's card below
    r = agents.enqueue_run(a["id"], "manual", "carol@x")
    sess = _FakeSession()
    monkeypatch.setattr(agentchat, "peek", lambda sid: sess if sid == "sid-1" and sess._perms else None)
    monkeypatch.setattr(agents_runtime, "_run_level", lambda sid: level)
    agents.update_run(r["id"], status="running", session_id="sid-1")
    agents.update_run(r["id"], waiting_approval=1)   # → poke → sync
    return a, r, sess


def test_a_tool_call_waiting_in_a_run_is_posted_and_decided_by_the_owner(pro, monkeypatch):
    import agents
    from teams.cards import actions_of
    sim = pro["sim"]
    a, r, sess = _waiting_run(pro, monkeypatch)
    posts = _posts(sim)
    assert len(posts) == 1
    card = posts[0]["attachments"][0]["content"]
    assert "use Bash?" in card["body"][0]["text"] and "rotate the HSM keys" in json.dumps(card)
    tok = actions_of(card)[0]["data"]["token"]
    assert "Refused for you" in _execute(pro, "alice@x", tok)["value"]   # not the owner
    assert sess.resolved == []
    out = _execute(pro, "carol@x", tok)                                   # the owner
    assert "allowed" in json.dumps(out["value"])
    assert sess.resolved == [("perm-1", {"decision": "allow", "message": None})]
    agents.update_run(r["id"], waiting_approval=0)
    assert any(m.get("method") == "PUT" for m in sim.sent)


def test_an_approval_above_the_channel_level_is_announced_without_its_content(pro, monkeypatch):
    from teams.cards import actions_of
    sim = pro["sim"]
    _waiting_run(pro, monkeypatch, level=3)                # the run read confidential notes
    posts = _posts(sim)
    assert len(posts) == 1
    card = posts[0]["attachments"][0]["content"]
    text = json.dumps(card)
    assert "Confidential" in text and "HSM" not in text and "Bash" not in text
    assert [x["type"] for x in actions_of(card)] == ["Action.OpenUrl"]   # no decision here
    import sqlite3
    from teams import store
    assert sqlite3.connect(store.db_path()).execute(
        "SELECT nonce FROM proactive WHERE item LIKE 'run.tool:%'").fetchone()[0] == ""                # no token issued


def test_proactive_off_by_interval_zero_or_feature_off(pro, monkeypatch):
    import teams
    from teams import proactive
    monkeypatch.setenv("SOKKAN_TEAMS_PROACTIVE_S", "0")
    _proposal()
    assert pro["sim"].sent == [] and proactive.state()["on"] is False
    monkeypatch.delenv("SOKKAN_TEAMS_PROACTIVE_S")
    monkeypatch.setattr(teams, "enabled", lambda: False)
    assert proactive.sync()["skipped"] == "inactive"


def test_admin_state_exposes_last_inbound_and_proactive(pro):
    _say(pro, "alice@x", "status")
    st = pro["as"]("admin@x", "default").get("/api/admin/teams").json()
    assert st["last_inbound"]["service_url"] == SERVICE and "serviceurl" in st["last_inbound"]["claims"]
    assert st["proactive"]["on"] is True and st["tenant"] == TENANT


def test_a_tool_decision_from_a_worker_thread_lands_on_the_session_loop():
    """The bot runs in a worker thread; the session's future belongs to the API loop."""
    import asyncio
    import threading
    from teams import bot

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0, result=loop.create_future()),
                                               loop).result()
        seen = []

        class S:
            _perms = {"p": fut}

            def resolve_permission(self, pid, decision):
                seen.append(threading.current_thread() is t)
                fut.set_result(decision)

        bot._resolve_permission(S(), "p", {"decision": "allow"})
        assert asyncio.run_coroutine_threadsafe(asyncio.wait_for(asyncio.shield(fut), 2),
                                                loop).result() == {"decision": "allow"}
        assert seen == [True]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(2)


def test_client_secret_from_a_file(tw, monkeypatch, tmp_path):
    import teams
    from teams import connector, store
    monkeypatch.delenv("SOKKAN_TEAMS_APP_PASSWORD")
    assert "SOKKAN_TEAMS_APP_PASSWORD" in teams.configured()
    f = tmp_path / "secret"
    f.write_text("sim-secret\n")
    f.chmod(0o600)
    monkeypatch.setenv("SOKKAN_TEAMS_APP_PASSWORD_FILE", str(f))
    assert teams.configured() == []
    c = store.con()
    with c:
        c.execute("DELETE FROM token_cache")
    c.close()
    assert connector.app_token(connector.BOT_SCOPE).startswith("tok-")
    assert tw["sim"].token_calls[-1]["client_secret"] == "sim-secret"
