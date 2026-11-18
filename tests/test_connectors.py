"""4.0 connectors (feature `connectors`): mail, calendar and shared documents AS the person,
reads on demand, drafts in the person's own mailbox, and every action that leaves — sending,
creating an event, depositing a deliverable — only on the person's approval card.

Against the demo provider, a simulated Microsoft Graph on a real socket (tests/fake_graph.py)
and a real folder. No Microsoft account, no mail server, nothing leaves the machine."""
import inspect
import json
import os
import sqlite3
import time

import pytest

import audit
import hitl_journal
import projects
from connectors import ConnectorError, policy, service, store, tools
from fake_graph import USER, FakeGraph
from simple_fixtures import world  # noqa: F401 — the fixture


@pytest.fixture()
def conn(world, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SOKKAN_FEATURE_CONNECTORS", "1")
    monkeypatch.setenv("SOKKAN_CONNECTORS_MODEL_ZONE", "CH")
    for v in ("SOKKAN_CONNECTORS_DEMO", "SOKKAN_CONNECTORS_MAX_LEVEL_OFFSHORE", "SOKKAN_CONNECTORS_DB"):
        monkeypatch.delenv(v, raising=False)
    store._ready = None
    return world


def _enable(c, **providers):
    body = {"providers": {k: {"enabled": True, **v} for k, v in providers.items()}}
    r = c.put("/api/connectors/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def ctx(email="bob@x", project="default", zone=None):
    return service.Ctx(email, "", project, zone=zone)


# ---- the switch and the settings ------------------------------------------------------------------
def test_off_by_default_everything_is_404(world, monkeypatch):  # noqa: F811
    monkeypatch.delenv("SOKKAN_FEATURE_CONNECTORS", raising=False)
    c = world["as"]("bob@x")
    assert c.get("/api/connectors").status_code == 404
    assert c.post("/api/connectors/actions/x/approve").status_code == 404
    import agentchat
    assert "sokkan-connectors" not in agentchat.mcp_servers_for("s" * 32, "bob@x", project="default")
    with pytest.raises(ConnectorError) as e:
        service.mail_list(ctx())
    assert e.value.code == "off"


def test_server_and_read_tools_declared_to_both_engines(conn):
    import agentchat
    srv = agentchat.mcp_servers_for("s" * 32, "bob@x", project="default")
    assert srv["sokkan-connectors"]["env"]["SOKKAN_SESSION_USER"] == "bob@x"
    assert "mcp__sokkan-connectors__mail_list" in agentchat.SAFE_TOOLS
    assert "mcp__sokkan-connectors__mail_propose_send" in agentchat.SAFE_TOOLS
    # a draft writes in the mailbox: it keeps the engine's permission card
    assert "mcp__sokkan-connectors__mail_draft" not in agentchat.SAFE_TOOLS
    assert "sokkan-connectors__files_read" in tools.read_tools("kernel")
    assert set(tools.ALL) == {n for n in dir(__import__("connectors_mcp")) if n in tools.ALL}


def test_no_tool_lets_the_model_choose_whose_mailbox(conn):
    import connectors_mcp
    for name in tools.ALL:
        params = inspect.signature(getattr(connectors_mcp, name)).parameters
        assert not {"email", "user", "person", "owner", "as_user"} & set(params), name


def test_settings_are_for_instance_admins_and_never_serve_the_secret(conn):
    c = conn["as"]("bob@x")
    assert c.put("/api/connectors/settings", json={"allow_send": False}).status_code == 403
    a = conn["as"]("admin@x")
    r = a.put("/api/connectors/settings", json={"providers": {"graph": {"enabled": True, "tenant": "t1",
                                                                         "client_id": "c1"}},
                                                "graph_client_secret": "graph-secret-for-tests"})
    assert r.status_code == 200
    assert "graph-secret-for-tests" not in r.text and r.json()["graph_client_secret_set"] is True
    assert "graph-secret-for-tests" not in open(store.db_path(), "rb").read().decode("latin-1")
    assert conn["as"]("bob@x").get("/api/connectors").json().get("settings") is None
    assert conn["as"]("admin@x").put("/api/connectors/settings", json={"providers": {"folder": {"root": "relative/path"}}}).status_code == 400


def test_turning_a_provider_off_drops_every_connection(conn):
    a = conn["as"]("admin@x")
    _enable(a, demo={})
    assert conn["as"]("bob@x").post("/api/connectors/demo/connect", json={}).status_code == 200
    assert store.account("bob@x", "demo")
    conn["as"]("admin@x").put("/api/connectors/settings", json={"providers": {"demo": {"enabled": False}}})
    assert store.account("bob@x", "demo") is None


# ---- the demo mailbox, end to end ------------------------------------------------------------------
@pytest.fixture()
def demo(conn):
    _enable(conn["as"]("admin@x"), demo={})
    c = conn["as"]("bob@x")
    assert c.post("/api/connectors/demo/connect", json={}).status_code == 200
    return c


def test_read_draft_propose_approve_send(demo, conn):
    lst = service.mail_list(ctx())
    assert lst["provider"] == "demo" and lst["messages"][0]["subject"]
    m = next(x for x in lst["messages"] if "formation" in x["subject"])
    th = service.mail_thread(ctx(), m["thread_id"])
    assert "Excel" in th["messages"][0]["body"]
    d = service.mail_draft(ctx(), "Bonjour, voici notre offre.", reply_to=m["id"])
    assert d["subject"].startswith("Re:") and "Rien n'est envoyé" in d["note"]
    card = service.propose_send(ctx(), d["draft_id"])
    assert card["status"] == "pending"
    from connectors import demo as demo_mod
    st = demo_mod._State("bob@x", "").load()
    assert st["sent"] == []                                   # nothing left before the click
    # the card is the person's: another person, an admin, gets "not found"
    assert conn["as"]("dave@x").post(f"/api/connectors/actions/{card['action_id']}/approve").status_code == 404
    assert conn["as"]("admin@x").post(f"/api/connectors/actions/{card['action_id']}/approve").status_code == 404
    c = conn["as"]("bob@x")
    pend = c.get("/api/connectors/actions", params={"status": "pending"}).json()["actions"]
    assert [p["id"] for p in pend] == [card["action_id"]] and "payload_enc" not in pend[0]
    assert pend[0]["preview"]["body"].startswith("Bonjour")
    r = c.post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert len(demo_mod._State("bob@x", "").load()["sent"]) == 1
    # decided once
    assert c.post(f"/api/connectors/actions/{card['action_id']}/approve").status_code == 409
    rows = hitl_journal.query(limit=50)
    row = next(r for r in rows if r["request_id"] == f"conn:{card['action_id']}")
    assert row["decision"] == "allow" and row["actor"] == "bob@x" and row["kind"] == "connector"
    acts = [e["action"] for e in audit.recent(200)]
    for a in ("connectors.mail.draft", "connectors.mail.send.proposed", "connectors.mail.send.done"):
        assert a in acts


def test_a_draft_changed_after_the_card_is_not_sent(demo, conn):
    from connectors import demo as demo_mod
    d = service.mail_draft(ctx(), "Version 1", to=["x@exemple.test"], subject="Offre")
    card = service.propose_send(ctx(), d["draft_id"])
    st = demo_mod._State("bob@x", "")
    data = st.load()
    next(m for m in data["mail"] if m["id"] == d["draft_id"])["body"] = "Version 2, prix doublé"
    st.save(data)
    r = conn["as"]("bob@x").post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert r.status_code == 409 and "a changé" in r.text
    assert st.load()["sent"] == [] and store.get_action(card["action_id"])["status"] == "failed"


def test_reject_and_expiry(demo, conn):
    d = service.mail_draft(ctx(), "x", to=["x@exemple.test"], subject="s")
    c1 = service.propose_send(ctx(), d["draft_id"])
    r = conn["as"]("bob@x").post(f"/api/connectors/actions/{c1['action_id']}/reject", json={"reason": "pas encore"})
    assert r.json()["status"] == "rejected"
    row = next(r for r in hitl_journal.query(limit=50) if r["request_id"] == f"conn:{c1['action_id']}")
    assert row["decision"] == "deny"
    c2 = service.propose_send(ctx(), d["draft_id"])
    con = sqlite3.connect(store.db_path())
    con.execute("UPDATE actions SET expires_at=? WHERE id=?", (time.time() - 1, c2["action_id"]))
    con.commit()
    con.close()
    assert conn["as"]("bob@x").post(f"/api/connectors/actions/{c2['action_id']}/approve").status_code == 409


def test_sending_off_keeps_the_draft_and_refuses_the_card(demo, conn):
    conn["as"]("admin@x").put("/api/connectors/settings", json={"allow_send": False})
    d = service.mail_draft(ctx(), "x", to=["x@exemple.test"], subject="s")
    with pytest.raises(ConnectorError) as e:
        service.propose_send(ctx(), d["draft_id"])
    assert e.value.code == "send_off"


def test_calendar_free_slots_and_event_on_a_card(demo, conn):
    ev = service.calendar_events(ctx())
    assert any(e["subject"] == "Point équipe" for e in ev["events"])
    slots = service.calendar_free_slots(ctx(), 60, start="2030-01-07T00:00:00Z", end="2030-01-09T00:00:00Z")
    assert slots["slots"] and all(s["start"] < s["end"] for s in slots["slots"])
    prep = service.calendar_prepare(ctx(), "e2")
    assert prep["event"]["attachments"][0]["name"] == "programme-2026.pdf"
    assert any("formation" in m["subject"].lower() for m in prep["related_mail"])
    card = service.propose_event(ctx(), "Atelier offre", "2030-01-07T10:00:00Z", "2030-01-07T11:00:00Z",
                                 ["claire.dumont@exemple-client.ch"])
    assert not any(e["subject"] == "Atelier offre" for e in
                   service.calendar_events(ctx(), "2030-01-07T00:00:00Z", "2030-01-08T00:00:00Z")["events"])
    conn["as"]("bob@x").post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert any(e["subject"] == "Atelier offre" for e in
               service.calendar_events(ctx(), "2030-01-07T00:00:00Z", "2030-01-08T00:00:00Z")["events"])


def test_files_read_search_and_write_on_a_card(demo, conn):
    assert any(i["name"] == "Offres" for i in service.files_list(ctx(), "/")["items"])
    assert service.files_search(ctx(), "modele")["items"][0]["path"] == "/Offres/modele-offre.md"
    assert "1 800 CHF" in service.files_read(ctx(), "/Offres/modele-offre.md")["text"]
    card = service.propose_write(ctx(), "/Offres/modele-offre.md", "# Nouvelle offre")
    conn["as"]("bob@x").post(f"/api/connectors/actions/{card['action_id']}/approve")
    a = store.get_action(card["action_id"])
    assert a["status"] == "done" and "(1)" in json.loads(a["result"])["path"]   # never overwritten
    assert "1 800 CHF" in service.files_read(ctx(), "/Offres/modele-offre.md")["text"]


# ---- classification × zone, and what a project may reach ---------------------------------------------
@pytest.mark.parametrize("level,zone,ok", [
    (2, "US", True), (2, "", True), (3, "US", False), (3, "EU", False), (3, "", False),
    (3, "CH", True), (4, "local", True), (4, "US", False)])
def test_classification_decides_where_content_may_go(demo, level, zone, ok):
    projects.set_level("default", level)
    if ok:
        assert service.mail_list(ctx(zone=zone))["messages"]
    else:
        with pytest.raises(ConnectorError) as e:
            service.mail_list(ctx(zone=zone))
        assert e.value.code == "classification" and e.value.status == 403
        for fn, a in ((service.files_read, ("/Offres/modele-offre.md",)), (service.calendar_events, ()),
                      (service.mail_thread, ("conv-salle",))):
            with pytest.raises(ConnectorError):
                fn(ctx(zone=zone), *a)


def test_offshore_ceiling_can_only_be_lowered(demo, monkeypatch):
    monkeypatch.setenv("SOKKAN_CONNECTORS_MAX_LEVEL_OFFSHORE", "team")
    projects.set_level("default", 2)
    with pytest.raises(ConnectorError):
        service.mail_list(ctx(zone="US"))
    monkeypatch.setenv("SOKKAN_CONNECTORS_MAX_LEVEL_OFFSHORE", "restricted")
    assert policy.max_level_offshore() == 2


def test_zone_of_a_write_with_claude_work_is_the_united_states(conn, monkeypatch):
    monkeypatch.delenv("SOKKAN_CONNECTORS_MODEL_ZONE")
    import claudetier
    monkeypatch.setattr(claudetier, "work_tier", lambda sid: ("claude", "claude-opus-5-5"))
    assert policy.session_zone("x" * 32, "default") == "US"
    monkeypatch.setattr(claudetier, "work_tier", lambda sid: ("", ""))
    import llm
    monkeypatch.setattr(llm, "load", lambda: {"engine": "appliance"})
    assert policy.session_zone("x" * 32, "default") == "local"


def test_shared_mailbox_only_when_opened_to_the_project(demo, conn):
    with pytest.raises(ConnectorError) as e:
        service.mail_list(ctx(), mailbox="direction@exemple-interne.ch")
    assert e.value.code == "mailbox_scope"
    conn["as"]("admin@x").put("/api/connectors/settings", json={"scopes": {"default": {"shared_mailboxes": [
        "direction@exemple-interne.ch"]}}})
    service.mail_list(ctx(), mailbox="direction@exemple-interne.ch")
    conn["as"]("alice@x", "radio").post("/api/connectors/demo/connect", json={})
    assert service.mail_list(ctx(project="radio", email="alice@x"))["messages"]
    with pytest.raises(ConnectorError) as e:
        service.mail_list(ctx(project="radio", email="alice@x"), mailbox="direction@exemple-interne.ch")
    assert e.value.code == "mailbox_scope"


def test_a_session_without_a_person_reads_nothing(conn, monkeypatch):
    monkeypatch.setenv("SOKKAN_SESSION_USER", "")
    import connectors_mcp
    assert connectors_mcp.mail_list()["code"] == "no_person"


def test_the_mcp_server_acts_as_the_session_person(demo, monkeypatch):
    import connectors_mcp
    monkeypatch.setenv("SOKKAN_SESSION_USER", "bob@x")
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "default")
    assert connectors_mcp.mail_list()["messages"]
    monkeypatch.setenv("SOKKAN_SESSION_USER", "dave@x")      # dave never connected anything
    assert connectors_mcp.mail_list()["code"] == "not_connected"


# ---- a network folder ---------------------------------------------------------------------------------
def test_folder_stays_inside_its_root_and_writes_only_where_allowed(conn, tmp_path):
    share, out = tmp_path / "share", tmp_path / "out"
    (share / "Qualite").mkdir(parents=True)
    (share / "Qualite" / "proc.txt").write_text("Procédure qualité")
    (tmp_path / "secret.txt").write_text("hors partage")
    os.symlink(tmp_path / "secret.txt", share / "Qualite" / "lien.txt")
    a = conn["as"]("admin@x")
    _enable(a, folder={"root": str(share)})
    with pytest.raises(ConnectorError) as e:               # no folder opened to the project
        service.files_list(ctx(), "/Qualite")
    assert e.value.code == "path_scope"
    conn["as"]("admin@x").put("/api/connectors/settings", json={"scopes": {"default": {"folders": ["/Qualite"]}}})
    names = [i["name"] for i in service.files_list(ctx(), "/Qualite")["items"]]
    assert names == ["proc.txt"]
    assert "qualité" in service.files_read(ctx(), "/Qualite/proc.txt")["text"]
    for bad in ("/Qualite/../secret.txt", "/Qualite/lien.txt", "/Autre/x.txt"):
        with pytest.raises(ConnectorError):
            service.files_read(ctx(), bad)
    card = service.propose_write(ctx(), "/Qualite/livrable.md", "# Livrable")
    r = conn["as"]("bob@x").post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert r.status_code == 409 and "lecture seule" in r.text           # no write_root: read only
    conn["as"]("admin@x").put("/api/connectors/settings", json={"providers": {"folder": {"write_root": str(out)}}})
    card = service.propose_write(ctx(), "/Qualite/livrable.md", "# Livrable")
    conn["as"]("bob@x").post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert (out / "Qualite" / "livrable.md").read_text() == "# Livrable"
    assert not (share / "Qualite" / "livrable.md").exists()


# ---- Microsoft 365 through a simulated Graph ----------------------------------------------------------
@pytest.fixture()
def graph(conn, monkeypatch):
    g = FakeGraph()
    monkeypatch.setenv("SOKKAN_CONNECTORS_GRAPH_URL", g.url + "/v1.0")
    monkeypatch.setenv("SOKKAN_CONNECTORS_LOGIN_URL", g.url)
    monkeypatch.setenv("SOKKAN_PUBLIC_URL", "https://sokkan.example.test")
    conn["as"]("admin@x").put("/api/connectors/settings", json={
        "providers": {"graph": {"enabled": True, "tenant": "tenant-1", "client_id": "client-1"}},
        "graph_client_secret": "graph-secret-for-tests", "graph_sites": {"finance": "site-fin"}})
    yield g
    g.close()


def _consent(conn, g, email="bob@x", user=USER):
    c = conn["as"](email)
    url = c.get("/api/connectors/graph/start").json()["url"]
    assert "code_challenge_method=S256" in url and "Mail.Send" in url
    state, code = g.consent(url, user)
    return c.get("/api/connectors/graph/callback", params={"state": state, "code": code},
                 follow_redirects=False)


def test_graph_delegated_consent_pkce_and_encrypted_tokens(conn, graph):
    r = _consent(conn, graph)
    assert r.status_code == 302 and "connected" in r.headers["location"]
    acc = store.account("bob@x", "graph")
    assert acc["login"] == USER
    raw = open(store.db_path(), "rb").read().decode("latin-1")
    assert not any(t in raw for t in graph.refresh) and not any(t in raw for t in graph.access)
    # replaying the callback: the state was single use
    r2 = conn["as"]("bob@x").get("/api/connectors/graph/callback", params={"state": "x", "code": "y"},
                                 follow_redirects=False)
    assert "error" in r2.headers["location"]


def test_graph_consent_started_by_someone_else_is_not_redeemed(conn, graph):
    c = conn["as"]("bob@x")
    url = c.get("/api/connectors/graph/start").json()["url"]
    state, code = graph.consent(url)
    r = conn["as"]("dave@x").get("/api/connectors/graph/callback", params={"state": state, "code": code},
                                 follow_redirects=False)
    assert "mismatch" in r.headers["location"]
    assert store.account("bob@x", "graph") is None and store.account("dave@x", "graph") is None
    assert code in graph.codes                                  # the code was never redeemed


def test_graph_without_sending_does_not_ask_mail_send(conn, graph):
    conn["as"]("admin@x").put("/api/connectors/settings", json={"allow_send": False})
    url = conn["as"]("bob@x").get("/api/connectors/graph/start").json()["url"]
    assert "Mail.Send" not in url and "Mail.ReadWrite" in url


def test_graph_mail_end_to_end(conn, graph):
    _consent(conn, graph)
    lst = service.mail_list(ctx())
    assert lst["provider"] == "graph" and lst["messages"][0]["subject"] == "Demande de devis"
    th = service.mail_thread(ctx(), "c1")
    assert th["messages"][0]["body"] == "Pourriez-vous nous envoyer un devis ?"     # html stripped
    d = service.mail_draft(ctx(), "Voici le devis.", reply_to=lst["messages"][0]["id"])
    assert d["to"] == ["client@fabrikam.test"]
    card = service.propose_send(ctx(), d["draft_id"])
    assert graph.sent == []
    conn["as"]("bob@x").post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert graph.sent and graph.sent[0]["body"]["content"] == "Voici le devis."
    # every Graph call carried a bearer of THIS person — no application token anywhere
    assert all(s["auth"].startswith("Bearer at-") for s in graph.seen if "/oauth2/" not in s["path"])


def test_graph_shared_mailbox_and_token_refresh(conn, graph):
    graph.token_ttl = 1                      # expires immediately (2 minutes of margin)
    _consent(conn, graph)
    conn["as"]("admin@x").put("/api/connectors/settings", json={"scopes": {"*": {"shared_mailboxes": ["info@contoso.test"]}}})
    n = len([s for s in graph.seen if s["path"].endswith("/token")])
    assert service.mail_list(ctx(), mailbox="info@contoso.test")["messages"]
    assert any("/users/info@contoso.test/" in s["path"] for s in graph.seen)
    assert len([s for s in graph.seen if s["path"].endswith("/token")]) == n + 1        # refreshed


def test_graph_calendar_and_files(conn, graph):
    _consent(conn, graph)
    ev = service.calendar_events(ctx(), "2030-01-07T00:00:00Z", "2030-01-08T00:00:00Z")["events"]
    assert ev[0]["subject"] == "Revue budget"
    prep = service.calendar_prepare(ctx(), "ev1")
    assert prep["event"]["body"].startswith("Ordre du jour") and prep["event"]["attachments"][0]["name"] == "budget.xlsx"
    slots = service.calendar_free_slots(ctx(), 60, "2030-01-07T00:00:00Z", "2030-01-08T00:00:00Z",
                                        attendees=["bob@contoso.test", "ext@other.test"])
    assert slots["attendees_checked"] == ["bob@contoso.test"] and slots["attendees_unknown"] == ["ext@other.test"]
    assert not any(s["start"] < "2030-01-07T14:00:00Z" < s["end"] for s in slots["slots"])
    card = service.propose_event(ctx(), "Point devis", "2030-01-08T09:00:00Z", "2030-01-08T09:30:00Z",
                                 ["client@fabrikam.test"])
    conn["as"]("bob@x").post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert any(e.get("subject") == "Point devis" for e in graph.events[USER])
    assert [i["name"] for i in service.files_list(ctx(), "/me")["items"]] == ["Rapports", "notes.md"]
    assert service.files_read(ctx(), "/me/Rapports/q4.txt")["text"] == "Chiffre Q4 : 1,2 MCHF"
    assert service.files_read(ctx(), "/finance/Shared/charte.txt")["text"] == "Charte financiere"
    assert service.files_search(ctx(), "q4", "/me")["items"][0]["path"] == "/me/Rapports/q4.txt"
    card = service.propose_write(ctx(), "/me/Livrables/offre.md", "# Offre")
    assert "Livrables/offre.md" not in graph.files["/me/drive"]
    conn["as"]("bob@x").post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert graph.files["/me/drive"]["Livrables/offre.md"] == b"# Offre"


def test_revoke_drops_the_tokens(conn, graph):
    _consent(conn, graph)
    assert conn["as"]("bob@x").delete("/api/connectors/graph").json()["revoked"] is True
    with pytest.raises(ConnectorError) as e:
        service.mail_list(ctx(), provider="graph")
    assert e.value.code == "not_connected"


def test_reencrypt_after_rotation_keeps_everything_readable(demo, conn):
    d = service.mail_draft(ctx(), "x", to=["x@exemple.test"], subject="s")
    card = service.propose_send(ctx(), d["draft_id"])
    store.put_instance_secret("graph_client_secret", "s3cret")
    assert store.reencrypt() >= 2
    assert store.action_payload(card["action_id"])["draft_id"] == d["draft_id"]
    assert store.instance_secret("graph_client_secret") == "s3cret"
