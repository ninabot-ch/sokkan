"""4.0 lot 0c: a thread is read back from its ENGINE (`Harness.history`), whatever the engine.

The cockpit pane and the simple profile's thread are replayed from the session's ring buffer;
after a restart of the api the ring is filled again from `history()` — no longer from the
CLI's transcript file, read by the cockpit itself. The proof that this is engine-neutral: the
same conversation, kept by the CLI (its transcript file) and by the fake engine of the
conformance package (in memory), reads back to the SAME thread in both views.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness import contract  # noqa: E402

BRIEF = "<sokkan-brief>\nAnswer in French.\n</sokkan-brief>\n\n"
ASK = "Rédige le compte rendu."
SAID = "Le compte rendu est enregistré."


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agentchat
    import audit
    import board
    import projects
    import usage
    from harness import registry

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(usage, "DB", tmp_path / "usage.db")
    projects.create("atelier", "Atelier", created_by="admin@x")
    projects.grant("atelier", "user", "alice@x", "dev")
    work = tmp_path / "default-work"
    work.mkdir()
    monkeypatch.setattr(agentchat, "CWD", str(work))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("SOKKAN_AGENT_CWD", str(work))
    monkeypatch.delenv("SOKKAN_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SOKKAN_ENGINE_RUNTIME", raising=False)
    monkeypatch.setattr(agentchat, "_registry", {})
    # the fake engine, declared for the test only (an instance cannot select it)
    monkeypatch.setitem(registry._DECLARED, "reference", registry.Engine(
        name="reference", label="reference", module="harness.conformance.reference",
        factory="ReferenceHarness"))
    return tmp_path


def _plain(events):
    import plain
    view = plain.View(deliv_dir="/nowhere")
    return [o for ev in events for o in view.feed(ev)]


def _read_back(sid):
    """What a person sees of the thread once the api restarted: the ring rebuilt."""
    import agentchat
    s = agentchat.get_or_create(sid, user="alice@x")
    return s.events


def _shape_ws(events):
    """The cockpit pane's flow, without what engines legitimately differ on (tool names,
    ids, the exact input, the tool's own output text)."""
    out = []
    for e in events:
        t = e.get("type")
        if t == "user":
            out.append(("user", e["text"]))
        elif t == "text":
            out.append(("text", e["text"]))
        elif t == "tool_use":
            out.append(("tool_use", e.get("kind")))
        elif t == "tool_result":
            out.append(("tool_result", e.get("is_error")))
        elif t == "status":
            out.append(("status", e["state"]))
    return out


def _shape_plain(items):
    out = []
    for o in items:
        t = o.get("type")
        if t in ("user", "text"):
            out.append((t, o["text"]))
        elif t in ("activity", "activity_done"):
            out.append((t, o.get("kind") if t == "activity" else o.get("ok")))
        elif t == "status":
            out.append((t, o.get("state")))
    return out


def _on_reference(env, project):
    """The conversation played live on the fake engine through the cockpit, a person
    allowing the save; then forgotten as a restart forgets it."""
    import agentchat
    import board
    from harness.conformance import driver, reference

    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="CR", project=project, owner="alice@x")
    cwd = agentchat.project_cwd(sid)
    model = reference.ReferenceModel()
    model.script.append([driver.write("compte-rendu.md", "# CR\n"), driver.say(SAID)])

    async def go():
        s = agentchat.get_or_create(sid, user="alice@x")
        s.attach(reference.ReferenceHarness(model),
                 contract.RunSpec(run_id=sid, cwd=cwd, project=project, user="alice@x"))
        turn = asyncio.ensure_future(s.handle_user(BRIEF + ASK))
        for _ in range(300):
            if s.pending_gates():
                break
            await asyncio.sleep(0.01)
        (g,) = s.pending_gates()
        assert s.decide(g["id"], contract.Decision.allow(by="alice@x", via="rail")) == "ok"
        await asyncio.wait_for(turn, 10)
        live = list(s.events)
        await agentchat.drop(sid)
        return live

    live = asyncio.new_event_loop().run_until_complete(go())
    assert agentchat.peek(sid) is None and board.get_claude_session_id(sid).startswith("ref-")
    assert (Path(cwd) / "compte-rendu.md").exists()
    return sid, live


def _on_the_cli(env, project):
    """The same conversation as the CLI keeps it: its transcript file, where it files it."""
    import agentchat
    import board

    csid = "4d1c2b3a-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="CR", project=project, owner="alice@x")
    board.set_claude_session_id(sid, csid)
    cwd = agentchat.project_cwd(sid)
    d = env / "claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    d.mkdir(parents=True, exist_ok=True)
    path = os.path.join(cwd, "compte-rendu.md")
    lines = [
        {"type": "user", "sessionId": csid, "message": {"role": "user", "content":
            BRIEF + ASK + "\n<system-reminder>internal note of the CLI</system-reminder>"}},
        {"type": "assistant", "sessionId": csid, "message": {"role": "assistant",
            "model": "claude-sonnet-4-5", "content": [{"type": "tool_use", "id": "toolu_1",
                                                       "name": "Write", "input": {
                                                           "file_path": path, "content": "# CR\n"}}]}},
        {"type": "user", "sessionId": csid, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "File created"}]}},
        {"type": "user", "sessionId": csid, "isMeta": True,
         "message": {"role": "user", "content": "<command-name>/compact</command-name>"}},
        {"type": "assistant", "sessionId": csid, "message": {"role": "assistant",
            "model": "claude-sonnet-4-5", "content": [{"type": "text", "text": SAID}]}},
    ]
    (d / f"{csid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), "utf-8")
    return sid


@pytest.mark.parametrize("project", ["atelier", "default"])
def test_a_thread_reads_back_the_same_whatever_the_engine(env, project):
    sid_ref, live = _on_reference(env, project)
    ref_back = _read_back(sid_ref)
    cli_back = _read_back(_on_the_cli(env, project))
    # the CLI's own lines (its reminder, its /compact) are not part of the thread
    assert not any("system-reminder" in json.dumps(e) or "compact" in json.dumps(e)
                   for e in cli_back), cli_back
    expected = [("user", BRIEF + ASK), ("tool_use", "file_write"), ("tool_result", False),
                ("text", SAID), ("status", "idle")]
    assert _shape_ws(ref_back) == expected, ref_back
    assert _shape_ws(cli_back) == expected, cli_back
    # read back = what the pane showed live (the same translation)
    live_shape = [x for x in _shape_ws(live) if x[0] != "status"]
    assert live_shape == [x for x in expected if x[0] not in ("user", "status")], live
    # the simple profile's thread: the person's words without the brief, one activity, the
    # answer — the same on both engines although their tools have other names
    assert _shape_plain(_plain(ref_back)) == _shape_plain(_plain(cli_back)) == [
        ("user", ASK), ("activity", "write_file"), ("activity_done", True), ("text", SAID),
        ("status", "idle")]


def test_the_session_detail_and_the_history_route_read_the_engine(env):
    import agents
    import app

    sid_ref, _ = _on_reference(env, "atelier")
    sid_cli = _on_the_cli(env, "atelier")
    for sid in (sid_ref, sid_cli):
        h = app.session_history(sid)
        assert h["available"] is True and h["engine"] in ("reference", "legacy")
        kinds = [i["kind"] for i in h["items"]]
        assert kinds == ["prompt", "tool", "text"], h
        assert h["items"][1]["classification"] == "file_write" and h["items"][1]["ok"]
        d = app.session_detail(sid)
        assert [m["kind"] for m in d["messages"]] == ["text", "tool", "text"]
        assert d["exists"] and d["kind"] == "sdk"
    assert app.session_history(sid_ref)["engine"] == "reference"
    assert app.session_history(sid_cli)["engine"] == "legacy"
    # a value the session's env held is masked in what the route returns
    original = agents.session_secret_values
    try:
        agents.session_secret_values = lambda s: {"X": SAID} if s == sid_cli else {}
        assert SAID not in json.dumps(app.session_history(sid_cli))
    finally:
        agents.session_secret_values = original


def test_an_engine_that_cannot_read_a_thread_back_is_said_not_rebuilt(env, monkeypatch):
    """No history on the engine: the pane is told, the CLI's file (if any) is not read."""
    import dataclasses

    import agentchat
    import app
    import board
    from harness import registry
    from harness.conformance import reference

    class Blind(reference.ReferenceHarness):
        def capabilities(self):
            return dataclasses.replace(super().capabilities(), history=False)

        def history(self, ref, *, cwd=""):
            raise contract.Unsupported("no history")

    import types
    mod = types.ModuleType("blind_engine")
    mod.Blind = Blind
    monkeypatch.setitem(sys.modules, "blind_engine", mod)
    monkeypatch.setitem(registry._DECLARED, "blind", registry.Engine(
        name="blind", label="blind", module="blind_engine", factory="Blind"))
    sid = _on_the_cli(env, "atelier")                     # a transcript IS there…
    monkeypatch.setattr(agentchat, "engine_of", lambda s: "blind")   # …but not this engine's
    evs = _read_back(sid)
    assert evs == [{"type": "history", "available": False}]
    assert [o for o in _plain(evs)] == [{"type": "notice", "code": "no_history", "detail": ""}]
    h = app.session_history(sid)
    assert h["available"] is False and h["items"] == []
    assert board.get_claude_session_id(sid)


def test_the_engine_of_a_session_is_the_one_its_journal_names(env):
    import agentchat
    import board
    sid_ref, _ = _on_reference(env, "atelier")
    assert agentchat.engine_of(sid_ref) == "reference"
    other = agentchat.new_sid()
    board.add_sdk_session(other, "docs", title="old", project="atelier")
    assert agentchat.engine_of(other) == "legacy"         # before 4.0: the CLI


def test_an_unknown_reference_or_another_projects_thread_is_empty(env):
    import agentchat
    import board
    theirs = _on_the_cli(env, "atelier")
    csid = board.get_claude_session_id(theirs)
    mine = agentchat.new_sid()
    board.add_sdk_session(mine, "docs", title="Autre", project="default", owner="bob@x")
    assert agentchat.thread_history(mine, csid) == ("legacy", [])
    assert agentchat.thread_history(mine, "../../etc/passwd") == ("legacy", [])
