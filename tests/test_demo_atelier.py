"""3.6.7 — SOKKAN Atelier on the public read-only demo (feature `demo_atelier`).

* the seed (backend/demo_atelier.py + the shipped backend/demo_atelier.json): fictional only,
  2-3 projects with notes, documents, finished works (thread, versions, validation);
  idempotent; never takes over a project it did not make;
* the visitor (a viewer) reads every screen of the Atelier — home, works, a work, its
  deliverable (preview, Word), documents, the replayed thread — and writes NOTHING: every
  write answers 403 « read-only demo », the shared identity's preferences included;
* off (or a missing requirement), nothing of it applies.
"""
import copy
import json
import re
from pathlib import Path

import pytest

import simple_fixtures as _fx

world = _fx.world
ROOT = Path(__file__).resolve().parent.parent
CONTENT = json.loads((ROOT / "backend" / "demo_atelier.json").read_text(encoding="utf-8"))
VISITOR = "demo@sokkan.ch"


@pytest.fixture()
def demo(world, monkeypatch, tmp_path):
    import iam
    monkeypatch.setenv("SOKKAN_OWNER_EMAIL", VISITOR)
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "1")
    monkeypatch.setenv("SOKKAN_DEMO_ATELIER", "1")
    monkeypatch.setenv("SOKKAN_PROJECT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("SOKKAN_DEMO_BOOK_URL", "https://example.org/book")
    monkeypatch.delenv("SOKKAN_DEMO_INSTALL_URL", raising=False)
    iam.upsert_user(VISITOR, "viewer")
    import demo_atelier
    report = demo_atelier.seed(copy.deepcopy(CONTENT))
    return {**world, "report": report}


def _sids(report, slug):
    p = next(x for x in report["projects"] if x["slug"] == slug)
    return {w["title"]: w["sid"] for w in p["works"]}


# ---- the content ----------------------------------------------------------------------------
def test_the_shipped_content_is_valid_and_fictional():
    import demo_atelier
    n = demo_atelier.check(copy.deepcopy(CONTENT))
    assert n["projects"] == 3 and n["works"] >= 6 and n["notes"] >= 6 and n["documents"] >= 3
    text = json.dumps(CONTENT, ensure_ascii=False)
    emails = set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text))
    assert emails and all(e.endswith("@example.com") for e in emails), emails
    assert not re.search(r"https?://", text)              # no link, internal or not
    assert {p["lang"] for p in CONTENT["projects"]} == {"fr", "en"}
    # French content addresses the reader as « vous », never « tu »
    fr = " ".join(json.dumps(p, ensure_ascii=False) for p in CONTENT["projects"] if p["lang"] == "fr")
    assert not re.search(r"\b(tu|toi|tes)\b|\bt'", fr, re.I)   # « ton » is also a noun (the tone)


@pytest.mark.parametrize("mutate, word", [
    (lambda s: s["projects"].append(copy.deepcopy(s["projects"][0])), "2 or 3 projects"),
    (lambda s: s["projects"][0]["works"][0].__setitem__("validated_by", "someone@acme-corp.ch"), "fictional"),
    (lambda s: s["projects"][0]["works"][0]["turns"][0]["steps"].append({"memory_get": "nope"}), "not in the project"),
    (lambda s: s["projects"][0].__setitem__("slug", "default"), "project slug"),
    (lambda s: s["projects"][0]["works"][0]["deliverable"].__setitem__("file", "../x.md"), "deliverable file"),
])
def test_check_refuses_a_bad_seed(mutate, word):
    import demo_atelier
    spec = copy.deepcopy(CONTENT)
    mutate(spec)
    with pytest.raises(demo_atelier.SeedError, match=word):
        demo_atelier.check(spec)


# ---- the seed -------------------------------------------------------------------------------
def test_the_seed_writes_finished_work_and_is_idempotent(demo):
    import board
    import demo_atelier
    import documents
    import simple
    import store_backend
    rep = demo["report"]
    assert rep["visitor"] == VISITOR and "warning" not in rep
    slugs = [p["slug"] for p in CONTENT["projects"]]

    def state():
        sessions = sorted(s["session_id"] for s in board.list_sessions() if s.get("project") in slugs)
        docs = sum(len(documents.list_project(s)) for s in slugs)
        notes = sorted(f.name for s in slugs for f in store_backend.memory_dir_for(s).glob("*.md"))
        return sessions, docs, notes

    first = state()
    assert len(first[0]) == sum(len(p["works"]) for p in CONTENT["projects"])
    assert first[1] == sum(len(p["documents"]) for p in CONTENT["projects"])
    assert len(first[2]) == sum(len(p["notes"]) for p in CONTENT["projects"])
    demo_atelier.seed(copy.deepcopy(CONTENT))
    assert state() == first                       # same works, no twin document or note
    # a work: its versions, its validation, its thread on disk
    sid = _sids(rep, "demo-formation")["Plan de formation — Excel pour la comptabilité"]
    d = simple.list_deliverables("demo-formation", sid)
    assert len(d) == 1 and d[0]["version"] == 2 and d[0]["validated"] is True
    assert d[0]["validated_by"].endswith("@example.com")
    assert demo_atelier.project_slugs() == sorted(slugs)


def test_the_seed_never_takes_over_a_project_it_did_not_make(world, monkeypatch, tmp_path):
    import demo_atelier
    import projects
    monkeypatch.setenv("SOKKAN_PROJECT_DIR", str(tmp_path / "transcripts"))
    projects.create("demo-support", "Real support", created_by="admin@x")
    with pytest.raises(demo_atelier.SeedError, match="not made by"):
        demo_atelier.seed(copy.deepcopy(CONTENT))


def test_the_command_line_refuses_outside_the_demo(monkeypatch, capsys):
    import demo_atelier
    monkeypatch.delenv("SOKKAN_DEMO_ATELIER", raising=False)
    monkeypatch.delenv("SOKKAN_FEATURE_DEMO_ATELIER", raising=False)
    assert demo_atelier.main(["seed"]) == 1
    assert "refused" in capsys.readouterr().err
    assert demo_atelier.main(["check"]) == 0


# ---- the visitor reads ----------------------------------------------------------------------
def test_the_visitor_reads_every_screen_of_the_atelier(demo):
    c = demo["as"](VISITOR, "demo-formation")
    f = c.get("/api/features").json()
    assert f["demo_atelier"] is True
    assert f["demo_cta"] == {"book_url": "https://example.org/book", "install_url": ""}
    assert set(f["demo_atelier_projects"]) == {"demo-formation", "demo-offres", "demo-support"}
    h = c.get("/api/simple/home?lang=fr").json()
    assert h["can_start"] is False and h["first_name"] == "" and h["project_name"] == "Formation interne"
    assert len(h["recents"]) == 3 and all(r["last_line"] for r in h["recents"])
    assert {r["validated"] for r in h["recents"]} == {True, False}      # one still to validate
    works = c.get("/api/simple/works").json()["works"]
    assert all(w["deliverables"] == 1 for w in works)
    sid = _sids(demo["report"], "demo-formation")["Plan de formation — Excel pour la comptabilité"]
    w = c.get(f"/api/simple/sessions/{sid}").json()
    assert w["can_drive"] is False and [d["name"] for d in w["documents"]] == ["Besoins équipe comptabilité.md"]
    name = w["deliverables"][0]["name"]
    one = c.get(f"/api/simple/sessions/{sid}/deliverables/{name}").json()
    assert one["text"].startswith("# Plan de formation") and "tableaux croisés" in one["text"]
    r = c.get(f"/api/simple/sessions/{sid}/deliverables/{name}/download", params={"format": "docx"})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    docs = c.get("/api/documents").json()["documents"]
    assert [d["name"] for d in docs] == ["Besoins équipe comptabilité.md"]
    assert c.get(f"/api/documents/{docs[0]['id']}/content").status_code == 200
    # the English project
    en = demo["as"](VISITOR, "demo-support").get("/api/simple/home?lang=en").json()
    assert en["project_name"] == "Customer support" and len(en["recents"]) == 2


def test_the_thread_of_a_finished_work_is_replayed_in_plain_words(demo):
    c = demo["as"](VISITOR, "demo-formation")
    sid = _sids(demo["report"], "demo-formation")["Plan de formation — Excel pour la comptabilité"]
    got = []
    with c.websocket_connect(f"/api/agent/ws/{sid}?view=simple") as ws:
        for _ in range(60):
            ev = ws.receive_json()
            got.append(ev)
            if ev.get("type") == "status" and ev.get("state") == "idle":
                break
        ws.send_json({"type": "user", "text": "hello"})    # a viewer is told, nothing runs
        refused = ws.receive_json()
    kinds = [e["type"] for e in got]
    users = [e["text"] for e in got if e["type"] == "user"]
    assert users[0].startswith("À partir du document joint") and "sokkan-brief" not in users[0]
    assert users[1].startswith("Ajoutez une demi-journée")
    acts = [e["kind"] for e in got if e["type"] == "activity"]
    assert acts[:3] == ["search_memory", "read_memory", "read_document"]
    cards = [e for e in got if e["type"] == "deliverable"]
    assert [x["version"] for x in cards] == [1, 2]
    assert cards[-1]["title"] == "Plan de formation — Excel pour l'équipe comptabilité"
    assert kinds.count("text") == 3 and not any("/" in e.get("subject", "") for e in got if e["type"] == "activity")
    assert refused["type"] == "error" and "Lecture seule" in refused["message"]
    assert demo["turns"] == []
    # 4.0: the thread comes back through the session contract, read by the engine that keeps it
    import agentchat
    engine, items = agentchat.thread_history(sid)
    assert engine == "legacy" and items and items[0].kind == "prompt"


# ---- the visitor writes nothing -------------------------------------------------------------
def test_the_visitor_writes_nothing(demo):
    import simple
    import uiprefs
    c = demo["as"](VISITOR, "demo-formation")
    sid = _sids(demo["report"], "demo-formation")["Annonce de la session de mars"]
    name = c.get(f"/api/simple/sessions/{sid}").json()["deliverables"][0]["name"]
    doc = c.get("/api/documents").json()["documents"][0]["id"]
    before = (json.dumps(simple.list_deliverables("demo-formation", sid)), uiprefs.get(VISITOR))
    for method, url, body in (
            ("POST", "/api/simple/start", {"ask": "Un plan de formation"}),
            ("POST", f"/api/simple/sessions/{sid}/message", {"text": "Ajoute une date"}),
            ("POST", f"/api/simple/sessions/{sid}/deliverables", {"title": "x", "text": "# x\n\ny"}),
            ("POST", f"/api/simple/sessions/{sid}/deliverables/{name}/validate", None),
            ("PUT", "/api/me/prefs", {"lang": "en", "profile": "technical"}),
            ("DELETE", f"/api/documents/{doc}", None),
            ("POST", f"/api/documents/{doc}/attach", {"session": sid})):
        r = c.request(method, url, json=body)
        assert r.status_code == 403 and r.json()["detail"] == "read-only demo", (method, url, r.text)
    r = c.put("/api/documents", params={"name": "x.txt"}, content=b"x\n",
              headers={"content-type": "application/octet-stream"})
    assert r.status_code == 403
    assert (json.dumps(simple.list_deliverables("demo-formation", sid)), uiprefs.get(VISITOR)) == before
    assert demo["turns"] == []
    # the demo's operator still works on it
    assert demo["as"]("admin@x").put("/api/me/prefs", json={"lang": "fr"}).status_code == 200


def test_off_or_without_its_requirements_nothing_applies(demo, monkeypatch):
    import demo_atelier
    c = demo["as"](VISITOR, "demo-formation")
    monkeypatch.setenv("SOKKAN_FEATURE_SIMPLE_PROFILE", "0")
    assert demo_atelier.enabled() is False
    monkeypatch.setenv("SOKKAN_FEATURE_SIMPLE_PROFILE", "1")
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "0")
    assert demo_atelier.enabled() is False
    assert c.get("/api/features").json()["demo_cta"] is None
    # off: a viewer's own preferences are theirs again (the regular rule)
    assert c.put("/api/me/prefs", json={"theme": "dark"}).status_code == 200
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "1")
    assert demo_atelier.enabled() is True


@pytest.mark.parametrize("value, kept", [
    ("https://sokkan.example/demo", True), ("http://localhost:3000/x", True),
    ("javascript:alert(1)", False), ("//evil.example", False), ("https://a b", False), ("", False)])
def test_the_call_to_action_takes_http_links_only(monkeypatch, value, kept):
    import demo_atelier
    monkeypatch.setenv("SOKKAN_DEMO_INSTALL_URL", value)
    assert demo_atelier.cta()["install_url"] == (value if kept else "")


def test_english_content_uses_english_quotes():
    en = json.dumps([p for p in CONTENT["projects"] if p["lang"] == "en"], ensure_ascii=False)
    assert "«" not in en and "»" not in en
