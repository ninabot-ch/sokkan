"""3.6.7 « Icebreaker » finishes of SOKKAN Atelier (the simple interface): a refusal is not a
failure, one approval per document asked for, one title per document, the dash of a title,
the project a person last worked in. The UI half is tests/nav/simple_thread_test.ts."""
import asyncio
import json
import os

import plain
import pytest
import simple
import uiprefs
from harness_helpers import ask, feed


def _view(tmp_path):
    deliv = tmp_path / "deliverables" / ("c" * 32)
    deliv.mkdir(parents=True)
    return plain.View(docs_dir=tmp_path / "documents", deliv_dir=deliv,
                      on_write=lambda name, tool_id, model="": 1), deliv


def _feed(v, evs):
    out = []
    for e in evs:
        out += v.feed(e)
    return out


# ---- 2. a refusal is not a failure -----------------------------------------------------------
def test_the_person_s_no_is_told_apart_from_a_failure(tmp_path):
    v, deliv = _view(tmp_path)
    path = str(deliv / "Quiz.md")
    inp = {"file_path": path, "content": "# Quiz\n"}
    out = _feed(v, [
        {"type": "tool_use", "id": "t1", "tool": "Write", "input": inp},
        {"type": "permission", "id": "p1", "tool": "Write", "input": inp},
        {"type": "permission_resolved", "id": "p1", "decision": "deny"},
        # whatever the CLI writes back: the View knows the person said no to THIS call
        {"type": "tool_result", "tool_use_id": "t1", "text": "Refusé", "is_error": True},
    ])
    ap = next(e for e in out if e["type"] == "approval")
    assert ap["tool_id"] == "t1"
    assert next(e for e in out if e["type"] == "approval_resolved")["decision"] == "deny"
    done = next(e for e in out if e["type"] == "activity_done")
    assert done["ok"] is False and done["reason"] == "denied"


def test_a_replayed_refusal_is_recognised_by_what_the_agent_was_told(tmp_path):
    v, _ = _view(tmp_path)
    out = _feed(v, [
        {"type": "tool_use", "id": "t1", "tool": "Write", "input": {"file_path": "/x/y.md"}},
        {"type": "tool_result", "tool_use_id": "t1", "text": "Denied by the user", "is_error": True},
    ])
    assert out[-1]["reason"] == "denied"


@pytest.mark.parametrize("said", [
    "No shell in this conversation: the person is not a developer. The folder …",
    "The person already approved this document in this turn: it is saved. …",
    "The person declined this document in this turn: it is not saved. …",
])
def test_a_rule_of_the_space_is_a_policy_refusal_not_an_orange_failure(tmp_path, said):
    v, _ = _view(tmp_path)
    out = _feed(v, [
        {"type": "tool_use", "id": "t1", "tool": "Bash", "input": {"command": "mkdir -p x"}},
        {"type": "tool_result", "tool_use_id": "t1", "text": said, "is_error": True},
    ])
    assert out[-1]["reason"] == "policy"


def test_a_real_failure_stays_a_failure(tmp_path):
    v, _ = _view(tmp_path)
    out = _feed(v, [
        {"type": "tool_use", "id": "t1", "tool": "Read", "input": {"file_path": "/nope"}},
        {"type": "tool_result", "tool_use_id": "t1", "text": "File does not exist.", "is_error": True},
    ])
    assert out[-1]["ok"] is False and "reason" not in out[-1]


# ---- 5. one title per document, its dash kept -------------------------------------------------
def test_the_card_in_the_thread_has_the_document_s_own_title(tmp_path):
    v, deliv = _view(tmp_path)
    (deliv / "quiz-positionnement.md").write_text("# Quiz de positionnement — Module 1\n\nQ1…\n")
    inp = {"file_path": str(deliv / "quiz-positionnement.md"), "content": "x"}
    out = _feed(v, [{"type": "tool_use", "id": "t1", "tool": "Write", "input": inp},
                    {"type": "tool_result", "tool_use_id": "t1", "text": "ok", "is_error": False}])
    card = next(e for e in out if e["type"] == "deliverable")
    assert card["title"] == "Quiz de positionnement — Module 1"
    # « Saved … » says the same title (Write carrying the content, else the file's own title)
    out2 = _feed(v, [{"type": "tool_use", "id": "t2", "tool": "Write",
                      "input": {**inp, "content": "# Quiz de positionnement — Module 1\n"}},
                     {"type": "tool_use", "id": "t3", "tool": "Edit", "input": {"file_path": inp["file_path"]}}])
    assert [e["subject"] for e in out2] == ["Quiz de positionnement — Module 1"] * 2
    # the same title as the list of deliverables (simple.list_deliverables)
    assert card["title"] == plain.title_from_markdown((deliv / "quiz-positionnement.md").read_text())


def test_a_spaced_dash_in_a_file_name_stays_a_dash():
    assert plain.human_title("Module 4 - Animer un atelier.md") == "Module 4 — Animer un atelier"
    assert plain.human_title("plan-de-formation_excel.md") == "Plan de formation excel"
    assert plain.human_title("Quiz – Module 2 v2.md") == "Quiz — Module 2"


# ---- 3. one approval per document asked for ---------------------------------------------------
def test_the_brief_asks_for_one_save_and_no_unasked_version(tmp_path, monkeypatch):
    import documents
    monkeypatch.setattr(documents, "project_root", lambda project: tmp_path)
    b = simple.brief(ask="Prépare un quiz.", lang="fr", project="p", sid="d" * 32)
    assert "ONE save" in b and "« v2 »" in b


def _session(monkeypatch, tmp_path):
    import agentchat
    s = agentchat.AgentSession.__new__(agentchat.AgentSession)
    s.sid, s.user, s.mode, s.policy, s.sandboxed, s.cwd = "e" * 32, "a@x", "default", None, None, str(tmp_path)
    s._perms, s._questions, s.redact_values, s._turn_model = {}, {}, [], ""
    s._pending = {}                 # 4.0: the gates waiting for a person (agentchat.decide)
    s._simple_work = True
    s._emit = lambda ev: s.events.append(ev)
    s.events = []
    monkeypatch.setattr(agentchat.hitl_journal, "requested", lambda *a, **k: None)
    monkeypatch.setattr(agentchat.hitl_journal, "decided", lambda *a, **k: None)
    monkeypatch.setattr(agentchat.hitl_journal, "automatic", lambda *a, **k: None)
    monkeypatch.setattr(s, "_arm_hitl_notify", lambda *a: None, raising=False)
    monkeypatch.setattr(s, "_set_waiting", lambda *a: None, raising=False)
    monkeypatch.setattr(s, "_has_documents", lambda: False, raising=False)
    return s


def _approve_next(s, decision="allow"):
    async def go():
        while not s._perms:
            await asyncio.sleep(0)
        s.resolve_permission(next(iter(s._perms)), {"decision": decision})
    return go()


def _journal(monkeypatch, s):
    """The rows the rule writes, and the person who decided the cards."""
    import agentchat
    rows = []
    monkeypatch.setattr(agentchat.hitl_journal, "automatic", lambda *a, **k: rows.append(k))
    monkeypatch.setattr(agentchat.hitl_journal, "peek_attribution", lambda pid: ("ana@x", "owner"))
    return rows


def test_later_saves_of_an_approved_document_go_through_without_a_card(monkeypatch, tmp_path):
    """4.0 (changes 3.6.7): asked to add a question, the model saves the question, then its line
    in the answer key, then the count. 3.6.7 refused every save after the first and left the
    quiz half-changed (parity bench n°4). Once the person approved the first save of a document
    in a turn, the later saves of THAT document in THAT turn are approved by rule, journaled
    with the person who approved the first."""
    s = _session(monkeypatch, tmp_path)
    rows = _journal(monkeypatch, s)
    s._approved_saves, s._declined_saves = {}, {}
    path = str(tmp_path / "deliverables" / s.sid / "Quiz.md")

    async def turn():
        first = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": "# Quiz"}))
        await _approve_next(s)
        r1 = await first
        r2 = await ask(s, "Edit", {"file_path": path, "old_string": "a", "new_string": "b"})
        r3 = await ask(s, "Edit", {"file_path": path, "old_string": "c", "new_string": "d"})
        return r1, r2, r3

    r1, r2, r3 = asyncio.run(turn())
    assert r1.allowed and r2.allowed and r3.allowed
    assert (r2.by, r2.via) == (r3.by, r3.via) == ("ana@x", "auto_rule")
    # one card for the person, nothing more in the thread
    assert [e["type"] for e in s.events].count("permission") == 1
    assert [(r["allowed"], r["reason"], r["actor"]) for r in rows] == [
        (True, "policy:simple_same_document_same_turn", "ana@x")] * 2
    # a new turn: the document asks again
    s._approved_saves, s._declined_saves = {}, {}

    async def next_turn():
        fut = asyncio.ensure_future(ask(s, "Edit", {"file_path": path, "old_string": "b",
                                                    "new_string": "e"}))
        await _approve_next(s)
        return await fut
    assert asyncio.run(next_turn()).allowed
    assert [e["type"] for e in s.events].count("permission") == 2


def test_a_refused_document_is_not_saved_again_in_the_turn(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    rows = _journal(monkeypatch, s)
    s._approved_saves, s._declined_saves = {}, {}
    path = str(tmp_path / "deliverables" / s.sid / "Quiz.md")

    async def turn():
        fut = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": "# A"}))
        await _approve_next(s, "deny")
        r1 = await fut
        r2 = await ask(s, "Edit", {"file_path": path, "old_string": "A", "new_string": "B"})
        return r1, r2

    r1, r2 = asyncio.run(turn())
    assert r1.denied and r2.denied
    assert "declined this document in this turn" in r2.message
    assert any(m in r2.message for m in plain.POLICY_DENIED)      # discreet in the thread
    assert [e["type"] for e in s.events].count("permission") == 1
    assert [(r["allowed"], r["reason"], r["actor"]) for r in rows] == [
        (False, "policy:simple_same_document_same_turn", "ana@x")]


def test_another_document_in_the_same_turn_has_its_own_card(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    _journal(monkeypatch, s)
    s._approved_saves, s._declined_saves = {}, {}
    quiz = str(tmp_path / "deliverables" / s.sid / "Quiz.md")
    memo = str(tmp_path / "deliverables" / s.sid / "Fiche mémo.md")

    async def turn():
        f1 = asyncio.ensure_future(ask(s, "Write", {"file_path": quiz, "content": "# Q"}))
        await _approve_next(s)
        await f1
        f2 = asyncio.ensure_future(ask(s, "Write", {"file_path": memo, "content": "# M"}))
        await _approve_next(s, "deny")
        r2 = await f2
        r3 = await ask(s, "Edit", {"file_path": quiz, "old_string": "Q", "new_string": "R"})
        return r2, r3

    r2, r3 = asyncio.run(turn())
    assert r2.denied and r3.allowed
    assert [e["type"] for e in s.events].count("permission") == 2


def test_the_thread_shows_one_version_per_document_per_turn(tmp_path):
    """The later saves of the turn are the same version: one card, the document as it stands
    at the end of the turn (the preview reads the file); the next turn is a new version."""
    deliv = tmp_path / "deliverables" / ("c" * 32)
    deliv.mkdir(parents=True)
    saved = []
    v = plain.View(docs_dir=tmp_path / "documents", deliv_dir=deliv,
                   on_write=lambda name, tool_id, model="": saved.append(tool_id) or len(saved))
    p = str(deliv / "Quiz.md")

    def save(tid):
        return _feed(v, [{"type": "tool_use", "id": tid, "tool": "Edit", "input": {"file_path": p}},
                         {"type": "tool_result", "tool_use_id": tid, "text": "ok"}])

    cards = [c for tid in ("t1", "t2", "t3") for c in save(tid) if c["type"] == "deliverable"]
    assert [c["version"] for c in cards] == [1, 1, 1] and saved == ["t1"]
    _feed(v, [{"type": "user", "text": "Ajoute une question"}])
    cards = [c for tid in ("t4", "t5") for c in save(tid) if c["type"] == "deliverable"]
    assert [c["version"] for c in cards] == [2, 2] and saved == ["t1", "t4"]


# ---- 8. the project a person last worked in --------------------------------------------------
def test_the_last_project_is_kept_per_person(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_UIPREFS_STORE", str(tmp_path / "uiprefs.json"))
    assert uiprefs.get("a@x")["project"] == ""
    uiprefs.set_prefs("a@x", project="contenus")
    uiprefs.set_prefs("b@x", project="atelier")
    assert uiprefs.get("A@x ")["project"] == "contenus"
    assert uiprefs.get("b@x")["project"] == "atelier"
    with pytest.raises(ValueError):
        uiprefs.set_prefs("a@x", project="../etc")
    assert json.loads((tmp_path / "uiprefs.json").read_text())["a@x"]["project"] == "contenus"
    assert os.stat(tmp_path / "uiprefs.json").st_size > 0


# ---- seen on the real 27B (3.6.7 end-to-end pass) ---------------------------------------------
def test_a_loaded_skill_s_page_is_not_the_agent_speaking(tmp_path, monkeypatch):
    """The CLI hands a loaded skill's body back as a USER message: it read as the answer."""
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    import agentchat
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, UserMessage
    s = agentchat.AgentSession("f" * 32)
    feed(s, UserMessage(content=[TextBlock(text="Base directory for this skill: /tmp/x\n# Data"),
                                      ToolResultBlock(tool_use_id="t1", content="ok", is_error=False)]))
    feed(s, AssistantMessage(content=[TextBlock(text="Voici le plan.")], model="m"))
    texts = [e["text"] for e in s.events if e["type"] == "text"]
    assert texts == ["Voici le plan."]
    assert any(e["type"] == "tool_result" for e in s.events)


def test_the_model_thinking_aloud_in_english_is_not_shown_to_a_french_person(tmp_path):
    aloud = ("The user is asking me to write a training plan in three parts. This contradicts the "
             "sokkan-brief which says to write the whole document in one save. I should explain that.")
    assert plain.reasoning_aloud(aloud, "fr")
    assert not plain.reasoning_aloud(aloud, "en")
    assert not plain.reasoning_aloud("Le plan est enregistré. Il contient les trois parties.", "fr")
    assert not plain.reasoning_aloud("OK, c'est fait : la partie 2 est ajoutée au document.", "fr")
    v, _ = _view(tmp_path)
    v.lang = "fr"
    assert v.feed({"type": "text", "text": aloud}) == []
    assert v.feed({"type": "text", "text": "Voici le plan."})[0]["text"] == "Voici le plan."


def test_the_english_dictionary_uses_english_quotes():
    """3.6.7: « Write with Claude » showed in English Settings — French guillemets belong to
    the French dictionary only."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "frontend/lib/simple/dict.ts").read_text(encoding="utf-8")
    en = src[src.index("export const en"):]
    bad = [ln.strip()[:80] for ln in en.splitlines() if "«" in ln or "»" in ln]
    assert not bad, bad
