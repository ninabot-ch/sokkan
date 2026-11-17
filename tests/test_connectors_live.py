"""4.0 connectors against REAL protocol servers in containers (tests/connectors_live/run.sh):
Greenmail (IMAP + SMTP), Radicale (CalDAV), WsgiDAV (WebDAV). Invented users, no real account,
nothing leaves the test network. Skipped unless SOKKAN_CONNECTORS_LIVE=1."""
import email
import email.policy
import imaplib
import os
import smtplib
import time
from email.message import EmailMessage

import httpx
import pytest

from connectors import ConnectorError, service, store
from simple_fixtures import world  # noqa: F401 — the fixture

pytestmark = pytest.mark.skipif(os.environ.get("SOKKAN_CONNECTORS_LIVE") != "1",
                                reason="needs the fake servers of tests/connectors_live/run.sh")
MAIL = os.environ.get("LIVE_MAIL_HOST", "")
CALDAV = os.environ.get("LIVE_CALDAV", "")
WEBDAV = os.environ.get("LIVE_WEBDAV", "")


@pytest.fixture()
def live(world, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SOKKAN_FEATURE_CONNECTORS", "1")
    monkeypatch.setenv("SOKKAN_CONNECTORS_MODEL_ZONE", "local")
    monkeypatch.setenv("SOKKAN_CONNECTORS_ALLOW_PLAIN", "1")      # private test network only
    store._ready = None
    r = world["as"]("admin@x").put("/api/connectors/settings", json={"providers": {
        "imap": {"enabled": True, "imap_host": MAIL, "imap_port": 3143, "smtp_host": MAIL, "smtp_port": 3025,
                 "security": "none"},
        "caldav": {"enabled": True, "url": CALDAV + "/{login}/work/"},
        "webdav": {"enabled": True, "url": WEBDAV}}})
    assert r.status_code == 200, r.text
    return world


def ctx(email="bob@x"):
    return service.Ctx(email, "", "default")


def _inbox(user, pw):
    m = imaplib.IMAP4(MAIL, 3143)
    m.login(user, pw)
    m.select("INBOX")
    typ, data = m.search(None, "ALL")
    out = []
    for n in (data[0] or b"").split():
        out.append(email.message_from_bytes(m.fetch(n, "(RFC822)")[1][0][1], policy=email.policy.default))
    m.logout()
    return out


def _deliver(to, subject, body, frm="client@exemple-client.test"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, to, subject
    msg["Message-ID"] = f"<{time.time_ns()}@exemple-client.test>"
    msg.set_content(body)
    with smtplib.SMTP(MAIL, 3025) as s:
        s.send_message(msg)
    return msg["Message-ID"]


def test_imap_wrong_password_stores_nothing(live):
    r = live["as"]("bob@x").post("/api/connectors/imap/connect", json={"login": "alice@exemple.test",
                                                                       "password": "faux"})
    assert r.status_code == 403 and store.account("bob@x", "imap") is None


def test_imap_read_draft_and_send_only_on_the_card(live):
    c = live["as"]("bob@x")
    assert c.post("/api/connectors/imap/connect", json={"login": "alice@exemple.test",
                                                        "password": "alicepw"}).status_code == 200
    _deliver("alice@exemple.test", "Demande d'offre Excel", "Trois jours, douze personnes ?")
    msgs = service.mail_list(ctx(), "inbox")["messages"]
    m = next(x for x in msgs if x["subject"] == "Demande d'offre Excel")
    assert service.mail_list(ctx(), query="Excel")["messages"]
    th = service.mail_thread(ctx(), m["id"])["messages"]
    assert "douze" in th[0]["body"]
    d = service.mail_draft(ctx(), "Bonjour, offre en pièce jointe demain.", reply_to=m["id"])
    assert d["draft_id"].startswith("Drafts:") and d["subject"].startswith("Re:")
    dr = service.mail_list(ctx(), "Drafts")["messages"]
    assert any(x["is_draft"] for x in dr)
    # the recipient in the test is bob@exemple.test: a draft to him, sent only on the card
    d2 = service.mail_draft(ctx(), "Point jeudi ?", to=["bob@exemple.test"], subject="Point jeudi")
    card = service.propose_send(ctx(), d2["draft_id"])
    time.sleep(1)
    assert not [x for x in _inbox("bob@exemple.test", "bobpw") if x["Subject"] == "Point jeudi"]
    r = c.post(f"/api/connectors/actions/{card['action_id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    for _ in range(20):
        got = [x for x in _inbox("bob@exemple.test", "bobpw") if x["Subject"] == "Point jeudi"]
        if got:
            break
        time.sleep(0.5)
    assert got and got[0]["From"] == "alice@exemple.test"
    with pytest.raises(ConnectorError):
        service.mail_list(ctx(), "inbox", mailbox="autre@exemple.test")       # not opened to the project


def test_caldav_events_slots_and_creation_on_a_card(live):
    with httpx.Client(auth=("alice", "alicepw")) as h:
        h.request("MKCOL", CALDAV + "/alice/", headers={})
        r = h.request("MKCALENDAR", CALDAV + "/alice/work/")
        assert r.status_code in (201, 405, 409), r.text
    c = live["as"]("bob@x")
    assert c.post("/api/connectors/caldav/connect", json={"login": "alice", "password": "alicepw"}).status_code == 200
    assert c.post("/api/connectors/caldav/connect", json={"login": "alice", "password": "x"}).status_code == 403
    card = service.propose_event(ctx(), "Revue qualité", "2030-01-07T09:00:00Z", "2030-01-07T10:00:00Z",
                                 ["marc@exemple.test"], location="Salle 2")
    win = ("2030-01-07T00:00:00Z", "2030-01-08T00:00:00Z")
    assert service.calendar_events(ctx(), *win)["events"] == []
    assert c.post(f"/api/connectors/actions/{card['action_id']}/approve").json()["status"] == "done"
    ev = service.calendar_events(ctx(), *win)["events"]
    assert [e["subject"] for e in ev] == ["Revue qualité"] and "marc@exemple.test" in ev[0]["attendees"]
    det = service.calendar_prepare(ctx(), ev[0]["id"])["event"]
    assert det["location"] == "Salle 2"
    slots = service.calendar_free_slots(ctx(), 60, *win)["slots"]
    assert slots and not any(s["start"] < "2030-01-07T09:30:00Z" < s["end"] for s in slots)


def test_webdav_read_write_and_ingest_mirror(live, tmp_path, monkeypatch):
    c = live["as"]("bob@x")
    assert c.post("/api/connectors/webdav/connect", json={"login": "alice", "password": "alicepw"}).status_code == 200
    names = {i["name"] for i in service.files_list(ctx(), "/")["items"]}
    assert {"Qualite", "Prive"} <= names
    assert "deux personnes" in service.files_read(ctx(), "/Qualite/procedure.txt")["text"]
    assert service.files_search(ctx(), "comite")["items"][0]["path"] == "/Qualite/2026/comite.md"
    # the admin restricts the project to /Qualite: /Prive disappears from reach
    live["as"]("admin@x").put("/api/connectors/settings", json={"scopes": {"default": {"folders": ["/Qualite"]}}})
    with pytest.raises(ConnectorError):
        service.files_read(ctx(), "/Prive/salaires.txt")
    assert all(i["path"].startswith("/Qualite") for i in service.files_search(ctx(), "a", "/Qualite")["items"])
    card = service.propose_write(ctx(), "/Qualite/livrable.md", "# Livrable")
    with httpx.Client(auth=("alice", "alicepw")) as h:
        assert h.get(WEBDAV + "/Qualite/livrable.md").status_code == 404
    c = live["as"]("bob@x")              # the cards are bob's: decided as bob, not as the admin
    assert c.post(f"/api/connectors/actions/{card['action_id']}/approve").json()["status"] == "done"
    card2 = service.propose_write(ctx(), "/Qualite/livrable.md", "# Livrable v2")
    assert c.post(f"/api/connectors/actions/{card2['action_id']}/approve").json()["status"] == "done"
    with httpx.Client(auth=("alice", "alicepw")) as h:
        assert h.get(WEBDAV + "/Qualite/livrable.md").text == "# Livrable"            # never overwritten
        assert h.get(WEBDAV + "/Qualite/livrable%20(1).md").text == "# Livrable v2"

    # doc_ingest source kind `webdav`: one-way mirror of the allow-listed directory only
    import vault
    from docingest import config as cfgmod
    from docingest import sources
    monkeypatch.setenv("SOKKAN_DOC_INGEST_DIR", str(tmp_path / "ingest"))
    vault.set_secret("INGEST_DAV_PW", "alicepw", project="default")
    cfg = cfgmod.parse_source({"id": "dav-qualite", "kind": "webdav", "allow": ["Qualite"],
                               "options": {"url": WEBDAV, "user": "alice", "password_secret": "INGEST_DAV_PW"}})
    src = sources.open_source(cfg)
    rep = src.prepare()
    assert rep["synced"] == ["Qualite"] and rep["fetched"] >= 2
    rels = sorted(f.rel for f in src.iter_files())
    assert "Qualite/procedure.txt" in rels and not any(r.startswith("Prive") for r in rels)
