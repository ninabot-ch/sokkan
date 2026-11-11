"""Simple profile (feature `simple_profile`) through the real request path: the per-person
setting, the task-first home, starting a work with documents, the plain thread over the
session WebSocket, the approval mode lock, deliverables (preview, download, validation)."""
import io
import zipfile

import pytest

import simple_fixtures as _fx
from simple_fixtures import DEFAULT_SID, RADIO_SID, docx, put

world = _fx.world      # the shared fixture (people, projects, no model)


def _simple(c):
    r = c.put("/api/me/prefs", json={"profile": "simple"})
    assert r.status_code == 200, r.text
    return r.json()


# ---- the setting -----------------------------------------------------------------------------
def test_nobody_changes_profile_until_they_choose(world):
    c = world["as"]("bob@x")
    p = c.get("/api/me/prefs").json()
    assert p == {"profile": "technical", "chosen": False, "lang": "auto", "theme": "light",
                 "available": True, "locked": False}
    assert _simple(c)["profile"] == "simple"
    assert c.get("/api/me/prefs").json()["chosen"] is True
    assert world["as"]("dave@x").get("/api/me/prefs").json()["profile"] == "technical"   # per person
    c = world["as"]("bob@x")
    assert c.put("/api/me/prefs", json={"lang": "en", "theme": "dark"}).json()["lang"] == "en"
    assert c.put("/api/me/prefs", json={"profile": "expert"}).status_code == 400
    assert c.put("/api/me/prefs", json={"theme": "sepia"}).status_code == 400
    assert world["audit"]("profile.set")[0]["detail"] == "simple"


def test_instance_default_and_lock(world, monkeypatch):
    monkeypatch.setenv("SOKKAN_PROFILE_DEFAULT", "simple")
    monkeypatch.setenv("SOKKAN_PROFILE_LOCKED", "1")
    c = world["as"]("bob@x")
    assert c.get("/api/me/prefs").json()["profile"] == "simple"
    assert c.put("/api/me/prefs", json={"profile": "technical"}).status_code == 403
    assert c.put("/api/me/prefs", json={"lang": "fr"}).status_code == 200      # language stays theirs
    assert c.put("/api/admin/prefs/dave@x", json={"profile": "technical"}).status_code == 403
    admin = world["as"]("admin@x")
    assert admin.put("/api/admin/prefs/bob@x", json={"profile": "technical"}).json()["profile"] == "technical"
    assert world["as"]("bob@x").get("/api/me/prefs").json()["profile"] == "technical"


def test_feature_off_everything_is_technical_and_routes_are_gone(world, monkeypatch):
    _simple(world["as"]("bob@x"))
    monkeypatch.setenv("SOKKAN_FEATURE_SIMPLE_PROFILE", "0")
    c = world["as"]("bob@x")
    p = c.get("/api/me/prefs").json()
    assert p["profile"] == "technical" and p["available"] is False
    for method, path in (("get", "/api/simple/home"), ("get", "/api/simple/works"),
                         ("get", f"/api/simple/sessions/{DEFAULT_SID}")):
        assert getattr(c, method)(path).status_code == 404
    assert c.post("/api/simple/start", json={"ask": "x"}).status_code == 404
    assert c.put("/api/me/prefs", json={"profile": "simple"}).status_code == 404
    assert c.get("/api/features").json()["simple_profile"] is False


def test_simple_profile_needs_the_document_drop(world, monkeypatch):
    import features
    monkeypatch.setenv("SOKKAN_FEATURE_DOCUMENTS", "0")
    st = features.resolve()["simple_profile"]
    assert st.enabled is False and "requires `documents`" in st.reason and st.problem is True
    assert world["as"]("bob@x").get("/api/simple/home").status_code == 404


# ---- home ------------------------------------------------------------------------------------
def test_home_greets_by_first_name_with_four_business_templates(world):
    c = world["as"]("bob@x")
    h = c.get("/api/simple/home", params={"lang": "fr"}).json()
    assert h["first_name"] == "Bob" and h["can_start"] is True and h["project"] == "default"
    assert [p["label"] for p in h["playbooks"]] == [
        "Plan de formation", "Synthèse", "Proposition client", "Quiz d'évaluation"]
    assert all(p["hint"] and p["description"] and "prompt" not in p for p in h["playbooks"])
    en = c.get("/api/simple/home", params={"lang": "en"}).json()
    assert en["playbooks"][0]["label"] == "Training plan"
    assert h["limits"]["extensions"] == ["pdf", "docx", "pptx", "xlsx", "txt", "md"]
    assert h["usage"] == {"pct": None, "basis": "none"}
    assert [r["sid"] for r in h["recents"]] == [DEFAULT_SID]          # bob's own work only
    # someone without a name is not greeted by their address
    assert world["as"]("dave@x").get("/api/simple/home").json()["first_name"] == ""
    assert world["as"]("carol@x").get("/api/simple/home").json()["can_start"] is False
    # the technical spawn form keeps its eight playbooks, none of the business ones
    tech = world["as"]("bob@x").get("/api/playbooks").json()
    assert len(tech) == 8 and "training-plan" not in [p["id"] for p in tech]


def test_usage_gauge_follows_the_project_budget(world, monkeypatch):
    import budgets
    import simple
    monkeypatch.setattr(simple, "_usage_cache", {})
    monkeypatch.setattr(budgets, "status", lambda project, fresh=False: {"enabled": True, "pct_day": 38.04})
    assert world["as"]("bob@x").get("/api/simple/home").json()["usage"] == {"pct": 38.04, "basis": "project"}


# ---- start -----------------------------------------------------------------------------------
def test_start_a_work_with_documents_and_a_template(world):
    c = world["as"]("bob@x")
    d1 = put(c, "Brief client.docx", docx(("Douze assistantes, deux jours.",))).json()
    d2 = put(c, "Notes.txt", "Salle B, vidéoprojecteur.\n".encode()).json()
    r = c.post("/api/simple/start", json={"ask": "Formation Excel intermédiaire, 2 jours",
                                          "playbook": "training-plan", "lang": "fr",
                                          "documents": [d1["id"], d2["id"]]})
    assert r.status_code == 200, r.text
    sid, title = r.json()["session_id"], r.json()["title"]
    assert title == "Plan de formation — Formation Excel intermédiaire, 2 jours"
    # what the agent got: the brief, the mission, the files to read, the save folder — then the ask
    (turn_sid, seed), = world["turns"]
    assert turn_sid == sid and seed.startswith("<sokkan-brief>")
    assert seed.rstrip().endswith("Formation Excel intermédiaire, 2 jours")
    assert "Write to them in French" in seed and "Write a training plan" in seed
    assert f"documents/{d1['id']}/Brief client.docx.txt" in seed and f"documents/{d2['id']}/Notes.txt" in seed
    assert f"deliverables/{sid}" in seed
    assert (world["workspace"] / "deliverables" / sid).is_dir()
    import plain
    assert plain.strip_brief(seed) == "Formation Excel intermédiaire, 2 jours"
    # what the person sees
    w = c.get(f"/api/simple/sessions/{sid}").json()
    assert w["title"] == title and w["can_drive"] is True
    assert sorted(x["name"] for x in w["documents"]) == ["Brief client.docx", "Notes.txt"]
    assert all("path" not in x for x in w["documents"])
    h = c.get("/api/simple/home").json()
    assert sid in [x["sid"] for x in h["recents"]] and h["works_total"] == 2
    ev = world["audit"]("session.spawn")[0]
    assert ev["user"] == "bob@x" and "training-plan" in ev["detail"] and "2 document(s)" in ev["detail"]
    # the thread, replayed over the WebSocket in plain view: the ask, never the brief
    with c.websocket_connect(f"/api/agent/ws/{sid}?view=simple") as ws:
        first = ws.receive_json()
    assert first["type"] == "user" and first["text"] == "Formation Excel intermédiaire, 2 jours"
    assert [x["name"] for x in first["documents"]] == ["Brief client.docx", "Notes.txt"]
    # the technical pane of the same session does not get the echo (it shows its own input)
    import agentchat
    agentchat.peek(sid)._emit({"type": "status", "state": "idle"})
    with c.websocket_connect(f"/api/agent/ws/{sid}") as ws:
        assert ws.receive_json() == {"type": "status", "state": "idle"}


def test_start_refusals(world):
    c = world["as"]("bob@x")
    assert c.post("/api/simple/start", json={"ask": "  "}).status_code == 400
    assert c.post("/api/simple/start", json={"ask": "x", "playbook": "refactor"}).status_code == 400
    assert c.post("/api/simple/start", json={"ask": "x", "playbook": "nope"}).status_code == 400
    assert c.post("/api/simple/start", json={"ask": "x", "documents": ["f" * 16]}).status_code == 404
    # a template alone needs at least a document
    assert c.post("/api/simple/start", json={"playbook": "document-summary"}).status_code == 400
    d = put(c, "a.txt", b"a\n").json()
    r = c.post("/api/simple/start", json={"playbook": "document-summary", "documents": [d["id"]], "lang": "fr"})
    assert r.status_code == 200 and r.json()["title"] == "Synthèse"
    # a document of another project cannot be slipped in
    theirs = put(world["as"]("alice@x", "radio"), "radio.txt", b"r\n").json()
    assert world["as"]("bob@x").post("/api/simple/start",
                                     json={"ask": "x", "documents": [theirs["id"]]}).status_code == 404
    assert world["as"]("carol@x").post("/api/simple/start", json={"ask": "x"}).status_code == 403
    assert world["as"]("bob@x", "radio").post("/api/simple/start", json={"ask": "x"}).status_code == 404
    assert len(world["turns"]) == 1


def test_follow_up_message_announces_new_documents_inside_the_brief(world):
    c = world["as"]("bob@x")
    d = put(c, "Module 3.txt", b"Tableaux\n").json()
    r = c.post(f"/api/simple/sessions/{DEFAULT_SID}/message",
               json={"text": "Ajoute le module 3", "documents": [d["id"]]})
    assert r.status_code == 200, r.text
    (_sid, text), = world["turns"]
    assert text.startswith("<sokkan-brief>") and "Module 3.txt" in text and text.endswith("Ajoute le module 3")
    with c.websocket_connect(f"/api/agent/ws/{DEFAULT_SID}?view=simple") as ws:
        ev = ws.receive_json()
    assert ev == {"type": "user", "text": "Ajoute le module 3", "documents": [{"id": d["id"], "name": "Module 3.txt"}]}
    # not the owner: dave cannot talk into bob's work; a viewer neither; another project: 404
    assert world["as"]("dave@x").post(f"/api/simple/sessions/{DEFAULT_SID}/message", json={"text": "x"}).status_code == 403
    assert world["as"]("carol@x").post(f"/api/simple/sessions/{DEFAULT_SID}/message", json={"text": "x"}).status_code == 403
    assert world["as"]("bob@x").post(f"/api/simple/sessions/{RADIO_SID}/message", json={"text": "x"}).status_code == 404
    assert world["as"]("bob@x").get(f"/api/simple/sessions/{RADIO_SID}").status_code == 404
    assert world["as"]("bob@x").post(f"/api/simple/sessions/{DEFAULT_SID}/message", json={"text": " "}).status_code == 400


# ---- the approval mode -----------------------------------------------------------------------
def test_approval_mode_cannot_be_changed_from_the_simple_profile(world):
    import agentchat
    c = world["as"]("bob@x")
    _simple(c)
    s = agentchat.get_or_create(DEFAULT_SID, user="bob@x")
    with c.websocket_connect(f"/api/agent/ws/{DEFAULT_SID}?view=simple") as ws:
        ws.send_json({"type": "mode", "mode": "bypassPermissions"})
        ev = ws.receive_json()
    assert ev["type"] == "notice" and ev["code"] == "mode_locked"
    assert s.mode == "default"
    # the technical socket of the same person is refused too: the lock is the profile, not the view
    with c.websocket_connect(f"/api/agent/ws/{DEFAULT_SID}") as ws:
        ws.send_json({"type": "mode", "mode": "bypassPermissions"})
        assert ws.receive_json()["type"] == "error"
    assert s.mode == "default"
    # back on the technical profile, the mode cycles as before
    c.put("/api/me/prefs", json={"profile": "technical"})
    with c.websocket_connect(f"/api/agent/ws/{DEFAULT_SID}") as ws:
        ws.send_json({"type": "mode", "mode": "acceptEdits"})
        assert ws.receive_json() == {"type": "perm_mode", "mode": "acceptEdits"}
    assert s.mode == "acceptEdits"


def test_the_plain_socket_carries_no_tool_name(world):
    import agentchat
    import json
    c = world["as"]("bob@x")
    s = agentchat.get_or_create(DEFAULT_SID, user="bob@x")
    target = world["workspace"] / "deliverables" / DEFAULT_SID / "Plan.md"
    s._emit({"type": "tool_use", "id": "t1", "tool": "mcp__sokkan-memory__memory_search",
             "title": "excel", "input": {"query": "excel"}})
    s._emit({"type": "permission", "id": "p1", "tool": "Write", "title": str(target),
             "input": {"file_path": str(target), "content": "# Plan de formation\n"}})
    with c.websocket_connect(f"/api/agent/ws/{DEFAULT_SID}?view=simple") as ws:
        a, b = ws.receive_json(), ws.receive_json()
    assert (a["type"], a["kind"], a["subject"]) == ("activity", "search_memory", "excel")
    assert (b["type"], b["kind"], b["title"]) == ("approval", "write_draft", "Plan de formation")
    shown = json.dumps([{k: v for k, v in x.items() if k != "detail"} for x in (a, b)])
    assert "mcp__" not in shown and "Write" not in shown and str(world["workspace"]) not in shown


# ---- deliverables ----------------------------------------------------------------------------
def test_deliverable_preview_download_and_validation(world):
    c = world["as"]("bob@x")
    folder = world["workspace"] / "deliverables" / DEFAULT_SID
    folder.mkdir(parents=True)
    (folder / "Plan de formation Excel.md").write_text("# Plan de formation Excel\n\n- Filtrer\n", encoding="utf-8")
    (folder / ".cache").write_text("x")
    (folder / "binaire.exe").write_bytes(b"MZ")
    base = f"/api/simple/sessions/{DEFAULT_SID}/deliverables"
    items = c.get(base).json()["deliverables"]
    assert [i["name"] for i in items] == ["Plan de formation Excel.md"]
    it = items[0]
    assert it["title"] == "Plan de formation Excel" and it["validated"] is False and it["previewable"]
    name = "Plan de formation Excel.md"
    assert c.get(f"{base}/{name}").json()["text"].startswith("# Plan de formation Excel")
    md = c.get(f"{base}/{name}/download")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    word = c.get(f"{base}/{name}/download", params={"format": "docx"})
    assert word.status_code == 200 and word.content[:4] == b"PK\x03\x04"
    assert "word/document.xml" in zipfile.ZipFile(io.BytesIO(word.content)).namelist()
    assert "Plan%20de%20formation%20Excel.docx" in word.headers["content-disposition"]
    # validation: who, when, exactly which content
    assert world["as"]("dave@x").post(f"{base}/{name}/validate").status_code == 403
    assert world["as"]("carol@x").post(f"{base}/{name}/validate").status_code == 403
    r = world["as"]("bob@x").post(f"{base}/{name}/validate")
    assert r.status_code == 200
    it = r.json()["deliverables"][0]
    assert it["validated"] is True and it["validated_by"] == "bob@x" and it["validated_at"]
    ev = world["audit"]("deliverable.validate")[0]
    assert ev["user"] == "bob@x" and name in ev["detail"] and "sha256" in ev["detail"]
    assert c.get("/api/simple/home").json()["recents"][0]["validated"] is True
    # the agent changes it afterwards: the validation no longer covers what is on disk
    (folder / name).write_text("# Plan de formation Excel\n\n- Filtrer\n- Trier\n", encoding="utf-8")
    it = c.get(base).json()["deliverables"][0]
    assert it["validated"] is False and it["changed_since_validation"] is True
    works = c.get("/api/simple/works").json()["works"]
    assert works[0]["items"][0]["name"] == name and works[0]["to_validate"] == 1


@pytest.mark.parametrize("bad", ["..%2F..%2Fsecret.txt", "%2e%2e", ".cache", "binaire.exe",
                                 "..%5Csecret.txt", "nope.md", "sub%2Fx.md"])
def test_deliverable_names_are_not_paths(world, bad):
    c = world["as"]("bob@x")
    folder = world["workspace"] / "deliverables" / DEFAULT_SID
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "x.md").write_text("nested")
    (folder / ".cache").write_text("hidden")
    (folder / "binaire.exe").write_bytes(b"MZ")
    (world["workspace"] / "secret.txt").write_text("workspace secret")
    base = f"/api/simple/sessions/{DEFAULT_SID}/deliverables"
    for suffix, method in (("", "get"), ("/download", "get"), ("/validate", "post")):
        r = getattr(c, method)(f"{base}/{bad}{suffix}")
        assert r.status_code in (404, 405), (bad, suffix, r.status_code)
        assert "workspace secret" not in r.text and "hidden" not in r.text and "nested" not in r.text


def test_a_symlink_in_the_folder_is_not_served(world):
    c = world["as"]("bob@x")
    folder = world["workspace"] / "deliverables" / DEFAULT_SID
    folder.mkdir(parents=True)
    (world["workspace"] / "secret.md").write_text("workspace secret")
    (folder / "lien.md").symlink_to(world["workspace"] / "secret.md")
    base = f"/api/simple/sessions/{DEFAULT_SID}/deliverables"
    assert c.get(base).json()["deliverables"] == []
    assert c.get(f"{base}/lien.md").status_code == 404
    assert c.get(f"{base}/lien.md/download").status_code == 404


def test_keep_an_answer_as_a_deliverable(world):
    c = world["as"]("bob@x")
    base = f"/api/simple/sessions/{DEFAULT_SID}/deliverables"
    r = c.post(base, json={"title": "../../Plan: v1?", "text": "Jour 1 : formules.\n"})
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    assert name == "Plan v1.md" and (world["workspace"] / "deliverables" / DEFAULT_SID / name).is_file()
    assert c.get(f"{base}/{name}").json()["text"].startswith("# Plan v1")
    assert c.post(base, json={"title": "Plan: v1", "text": "autre"}).json()["name"] == "Plan v1 (2).md"
    assert c.post(base, json={"title": "x", "text": "  "}).status_code == 400
    assert world["as"]("dave@x").post(base, json={"title": "x", "text": "y"}).status_code == 403
    assert world["audit"]("deliverable.save")


def test_nina_speaks_to_the_simple_profile_in_its_own_words(world):
    import assistant
    _simple(world["as"]("bob@x"))
    guide = assistant._simple_profile_directive("bob@x")
    assert "PROFIL SIMPLE" in guide and "Joindre des documents" in guide and "DevOps" in guide.split("pas une")[1][:40]
    assert assistant._simple_profile_directive("dave@x") == ""        # technical profile: Nina unchanged
