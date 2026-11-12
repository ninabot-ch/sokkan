"""3.6.4 — what the simple profile showed on a real install of 3.6.3.

* one document saved five times in a turn (the agent writes it a section at a time, each save
  approved): the thread showed one card per save, and once replayed (a new connection, the api
  restarted) every card said « version 5 ». The version of a save is now its own rank, live and
  replayed alike; the thread keeps ONE card per document (tests/nav/simple_thread_test.ts);
* a further message whose answer was lost was announced as failed, and sent twice on the second
  click: one id per message, taken once by the server;
* the technical cockpit looked for the transcript of a chat session in the instance's folder
  only (sessions list « active », transcript view);
* the title of a memory note: a slug has no accent, the note's own words have.
"""
import json
import re

import pytest

import simple_fixtures as _fx
from simple_fixtures import DEFAULT_SID, RADIO_SID

world = _fx.world

CSID = "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _transcript_dir(world, monkeypatch, sid):
    """Where the CLI files the transcript of `sid` (its own working directory)."""
    import agentchat
    claude = world["tmp"] / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.delenv("SOKKAN_PROJECT_DIR", raising=False)
    d = claude / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", agentchat.project_cwd(sid))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _five_saves(deliv):
    """One Write then four Edits of the same document, one refused Edit in between."""
    path = str(deliv / "Plan de cours.md")
    lines = [{"type": "user", "uuid": "u0", "message": {"role": "user", "content": "Prépare le plan de cours."}}]
    ids = ["toolu_w1", "toolu_e2", "toolu_x", "toolu_e3", "toolu_e4", "toolu_e5"]
    for i, tid in enumerate(ids):
        tool = "Write" if i == 0 else "Edit"
        inp = {"file_path": path, "content": "# Plan de cours\n"} if tool == "Write" else \
            {"file_path": path, "old_string": "x", "new_string": f"partie {i}"}
        lines.append({"type": "assistant", "uuid": f"a{i}", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": tool, "input": inp}]}})
        lines.append({"type": "user", "uuid": f"r{i}", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "ok" if tid != "toolu_x" else "refused",
             "is_error": tid == "toolu_x"}]}})
    lines.append({"type": "assistant", "uuid": "z", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Le plan est prêt."}]}})
    return lines


def _versions(sid):
    """The deliverable cards a browser receives when it opens the work (the WS replay)."""
    import agentchat
    import simple_api
    view = simple_api.make_view(sid)
    s = agentchat.get_or_create(sid, user="alice@x")
    return [(o["name"], o["version"]) for ev in list(s.events) for o in view.feed(ev)
            if o["type"] == "deliverable"]


@pytest.mark.parametrize("seen_live", [True, False])
def test_replayed_deliverables_keep_the_version_of_each_save_after_a_restart(world, monkeypatch, seen_live):
    import agentchat
    import board
    import simple

    deliv = simple.deliverable_dir("radio", RADIO_SID)
    deliv.mkdir(parents=True, exist_ok=True)
    (deliv / "Plan de cours.md").write_text("# Plan de cours\n", encoding="utf-8")
    board.set_claude_session_id(RADIO_SID, CSID)
    lines = _five_saves(deliv)
    if seen_live:
        # the turn as the person followed it: the live view registers each save as it lands
        import simple_api
        live = simple_api.make_view(RADIO_SID)
        from transcript import parse_lines
        got = []
        for m in parse_lines(json.dumps(x) for x in lines)["messages"]:
            if m.get("kind") == "tool":
                for o in live.feed({"type": "tool_use", "id": m["id"], "tool": m["tool"], "input": m["input"]}) + \
                        live.feed({"type": "tool_result", "tool_use_id": m["id"], **m["result"]}):
                    if o["type"] == "deliverable":
                        got.append(o["version"])
        assert got == [1, 2, 3, 4, 5]
    d = _transcript_dir(world, monkeypatch, RADIO_SID)
    (d / f"{CSID}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    monkeypatch.setattr(agentchat, "_registry", {})          # what a restart leaves

    first = _versions(RADIO_SID)
    assert first == [("Plan de cours.md", n) for n in (1, 2, 3, 4, 5)], first
    # opened again (another tab, a reconnection): the same numbers, nothing counted twice
    assert _versions(RADIO_SID) == first
    item, = simple.list_deliverables("radio", RADIO_SID)
    assert item["version"] == 5


def test_a_follow_up_sent_twice_with_the_same_id_is_taken_once(world):
    import agentchat
    c = world["as"]("bob@x")
    url = f"/api/simple/sessions/{DEFAULT_SID}/message"
    body = {"text": "Ajoute une pause à 10 h.", "request_id": "cd" * 16}
    first = c.post(url, json=body)
    assert first.status_code == 200 and first.json() == {"ok": True}, first.text
    # the agent is working on it — which is exactly when the browser asks again
    agentchat.get_or_create(DEFAULT_SID)._busy = True
    again = c.post(url, json=body)
    assert again.status_code == 200 and again.json() == {"ok": True, "replayed": True}, again.text
    assert len([1 for s, _ in world["turns"] if s == DEFAULT_SID]) == 1
    assert len(world["audit"]("session.send")) == 1
    # another message while it works is still refused as busy, as before
    other = c.post(url, json={"text": "Et une autre chose.", "request_id": "ef" * 16})
    assert other.status_code == 409 and other.json()["detail"]["code"] == "busy"
    agentchat.get_or_create(DEFAULT_SID)._busy = False
    # … and taken once it is free: a refused message did not burn its id
    assert c.post(url, json={"text": "Et une autre chose.", "request_id": "ef" * 16}).json() == {"ok": True}
    # no id, or one that is not an id: every message is sent, as before
    assert c.post(url, json={"text": "Encore."}).json() == {"ok": True}
    assert c.post(url, json={"text": "Encore.", "request_id": "../x"}).json() == {"ok": True}
    assert len(world["audit"]("session.send")) == 4
    # the id belongs to the work: the same one on another work is another message
    alice = world["as"]("alice@x", "radio")
    assert alice.post(f"/api/simple/sessions/{RADIO_SID}/message", json=body).json() == {"ok": True}


def test_the_cockpit_finds_the_transcript_of_a_project_session(world, monkeypatch):
    import board
    board.set_claude_session_id(RADIO_SID, CSID)
    d = _transcript_dir(world, monkeypatch, RADIO_SID)
    (d / f"{CSID}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [
        {"type": "user", "uuid": "u", "message": {"role": "user", "content": "Relance le devis de mars."}},
        {"type": "assistant", "uuid": "a", "message": {"role": "assistant",
                                                       "content": [{"type": "text", "text": "Brouillon prêt."}]}},
    ]), encoding="utf-8")
    c = world["as"]("alice@x", "radio")
    row = next(s for s in c.get("/api/sessions").json() if s["session_id"] == RADIO_SID)
    assert row["exists"] is True and row["active"] is True       # the transcript just changed
    det = c.get(f"/api/sessions/{RADIO_SID}").json()
    assert det["exists"] is True and [m["text"] for m in det["messages"]] == \
        ["Relance le devis de mars.", "Brouillon prêt."]


def test_the_cockpit_never_reads_the_transcript_of_another_project(world, monkeypatch):
    """The default work names a transcript filed under the radio project's folder: not found."""
    import board
    board.set_claude_session_id(DEFAULT_SID, CSID)
    d = _transcript_dir(world, monkeypatch, RADIO_SID)
    (d / f"{CSID}.jsonl").write_text(json.dumps(
        {"type": "user", "uuid": "u", "message": {"role": "user", "content": "secret radio"}}) + "\n")
    c = world["as"]("bob@x")
    assert c.get(f"/api/sessions/{DEFAULT_SID}").json()["messages"] == []
    row = next(s for s in c.get("/api/sessions").json() if s["session_id"] == DEFAULT_SID)
    assert row["exists"] is False


# ---- the title of a memory note --------------------------------------------------------------
def _note(name, description, body="", extra=""):
    return (f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n{extra}"
            f"metadata:\n  type: reference\n---\n\n{body}")


@pytest.mark.parametrize("text,title", [
    (_note("evaluation-des-acquis", "Évaluation des acquis à l'Atelier : trois questions par module"),
     "Évaluation des acquis à l'Atelier"),
    (_note("relance-des-devis-sans-reponse", "Relance des devis sans réponse après dix jours"),
     "Relance des devis sans réponse après dix jours"),
    (_note("faq-clients", "FAQ clients : les questions qui reviennent", "# Questions fréquentes\n\nTexte."),
     "Questions fréquentes"),
    (_note("charte", "Charte éditoriale : vouvoiement", "# Autre\n", extra="title: \"Charte éditoriale de l'Atelier\"\n"),
     "Charte éditoriale de l'Atelier"),
    (_note("procedure-de-devis", "x" * 200), "Procedure de devis"),
])
def test_a_note_is_titled_by_its_own_words(tmp_path, text, title):
    from core import notes
    p = tmp_path / "n.md"
    p.write_text(text, encoding="utf-8")
    parsed = notes.parse_note(text, "n.md")
    assert notes.title_of_file(p, parsed.name, parsed.description) == title
    assert notes.title_of_file(tmp_path / "missing.md", "faq-clients", "FAQ clients : les questions") == "FAQ clients"


def test_the_card_of_a_note_to_retain_says_its_title_from_the_description():
    import plain
    tool = "mcp__sokkan-memory__memory_write"
    c = plain.View().classify(tool, {"name": "evaluation-des-acquis", "body": "Trois questions par module.",
                                     "description": "Évaluation des acquis à l'Atelier : trois questions"})
    assert c == {"kind": "save_memory", "subject": "Évaluation des acquis à l'Atelier"}


def test_the_memory_list_carries_the_title(tmp_path, monkeypatch):
    """The 2.x index (memory.db): every row of /api/memory/notes says its title, so the Memory
    page no longer reads forty bodies to find them."""
    import sqlite3

    import memorykb
    f = tmp_path / "evaluation_des_acquis.md"
    f.write_text(_note("evaluation-des-acquis", "Évaluation des acquis à l'Atelier : trois questions"), encoding="utf-8")
    db = tmp_path / "memory.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE notes (name TEXT, description TEXT, type TEXT, mtime REAL, source_path TEXT,"
                      " priority INTEGER); CREATE TABLE chunks (note_name TEXT); CREATE TABLE links (src TEXT, dst TEXT);")
    con.execute("INSERT INTO notes VALUES (?,?,?,?,?,0)", ("evaluation-des-acquis",
                "Évaluation des acquis à l'Atelier : trois questions", "reference", 1.0, str(f)))
    con.commit()
    con.close()
    monkeypatch.setattr(memorykb, "MEM_DB", db)
    monkeypatch.setattr(memorykb, "_store", lambda: None)
    row, = memorykb.list_notes()
    assert row["title"] == "Évaluation des acquis à l'Atelier"
