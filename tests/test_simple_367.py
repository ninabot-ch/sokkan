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


def test_a_document_approved_in_this_turn_is_not_saved_again_on_the_agent_s_own(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    s._approved_saves = set()
    path = str(tmp_path / "deliverables" / s.sid / "Quiz.md")

    async def turn():
        async def approve_first():
            while not s._perms:
                await asyncio.sleep(0)
            s.resolve_permission(next(iter(s._perms)), {"decision": "allow"})
        first = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": "# Quiz"}))
        await approve_first()
        r1 = await first
        r2 = await ask(s, "Write", {"file_path": path, "content": "# Quiz v2"})
        return r1, r2

    r1, r2 = asyncio.run(turn())
    assert r1.allowed
    assert r2.denied and "already approved this document" in r2.message
    # a single card was asked; the second save never reached the person
    assert [e["type"] for e in s.events].count("permission") == 1
    # the event of the decision says who decided what (a replayed thread keeps « Authorised »)
    assert {"type": "permission_resolved", "decision": "allow"}.items() <= next(
        e for e in s.events if e["type"] == "permission_resolved").items()


def test_a_refused_save_does_not_close_the_document(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    s._approved_saves = set()
    path = str(tmp_path / "deliverables" / s.sid / "Quiz.md")

    async def turn():
        fut = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": "# A"}))
        while not s._perms:
            await asyncio.sleep(0)
        s.resolve_permission(next(iter(s._perms)), {"decision": "deny"})
        await fut
        fut2 = asyncio.ensure_future(ask(s, "Write", {"file_path": path, "content": "# B"}))
        while not s._perms:
            await asyncio.sleep(0)
        return fut2

    async def main():
        fut2 = await turn()
        s.resolve_permission(next(iter(s._perms)), {"decision": "allow"})
        return await fut2
    assert asyncio.run(main()).allowed


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
