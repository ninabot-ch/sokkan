"""3.6.3: the conversation of a work is still readable after the api restarts.

The thread a person reads is replayed from the session's ring buffer, which lives in the api
process. After a restart it is rebuilt from the transcript the Claude Code CLI keeps on disk —
and the CLI files a transcript under the session's OWN working directory. A work of any project
other than `default` runs in `$SOKKAN_DATA_DIR/projects/<slug>/work`, so its transcript was
looked for in the wrong folder and the thread opened empty (deliverables and validations, kept
elsewhere, were still there).

* with the REAL CLI (bundled with claude-agent-sdk) against a scripted model: a turn in a
  project, the registry forgotten as a restart does, the session reopened → the exchange is
  back, and the plain view of the simple profile shows the person's words without the brief;
* without a CLI: the same with a transcript written where the CLI writes it, including a
  working directory whose name holds characters the CLI replaces.

Opt out of the CLI case: SOKKAN_TEST_E2E_AGENTS=0.
"""
import asyncio
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _bundled_cli() -> bool:
    try:
        import claude_agent_sdk
    except ImportError:
        return False
    d = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled")
    return os.path.exists(os.path.join(d, "claude"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agentchat
    import audit
    import board
    import projects

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    projects.create("atelier", "Atelier", created_by="admin@x")
    projects.grant("atelier", "user", "alice@x", "dev")
    work = tmp_path / "default-work"
    work.mkdir()
    monkeypatch.setattr(agentchat, "CWD", str(work))
    for k, v in {"ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
                 "SOKKAN_AGENT_CWD": str(work), "DISABLE_TELEMETRY": "1",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("SOKKAN_PROJECT_DIR", raising=False)
    monkeypatch.setattr(agentchat, "_registry", {})
    return {"tmp": tmp_path}


def _plain(events: list[dict]) -> list[dict]:
    import plain
    view = plain.View()
    return [o for ev in events for o in view.feed(ev)]


BRIEF = "<sokkan-brief>\nAnswer in French.\n</sokkan-brief>\n\n"


@pytest.mark.skipif(os.environ.get("SOKKAN_TEST_E2E_AGENTS") == "0", reason="SOKKAN_TEST_E2E_AGENTS=0")
@pytest.mark.skipif(not _bundled_cli(), reason="claude-agent-sdk without bundled CLI")
@pytest.mark.parametrize("project", ["atelier", "default"])
def test_thread_is_readable_after_a_restart_real_cli(env, monkeypatch, project):
    import agentchat
    import board
    from mock_anthropic import MockAnthropic

    class Says(MockAnthropic):
        def answer(self, req, mid):
            return {"type": "text", "text": "Voici le plan en trois points."}, "end_turn"

    with Says() as mock:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.url)
        sid = agentchat.new_sid()
        board.add_sdk_session(sid, "docs", title="Plan", project=project, owner="alice@x")
        s = agentchat.get_or_create(sid, user="alice@x")

        async def turn():
            await asyncio.wait_for(s.handle_user(BRIEF + "Prépare un plan de cours."), 90)
            await agentchat.drop(sid)          # what a restart leaves: no session in memory

        asyncio.new_event_loop().run_until_complete(turn())
    assert any(e.get("type") == "text" for e in s.events), s.events
    assert agentchat.peek(sid) is None and board.get_claude_session_id(sid)

    again = agentchat.get_or_create(sid, user="alice@x")
    shown = _plain(again.events)
    assert {"type": "user", "text": "Prépare un plan de cours.", "documents": []} in shown, shown
    texts = [e for e in shown if e.get("type") == "text"]
    assert [e["text"] for e in texts] == ["Voici le plan en trois points."], shown
    # 3.6.5: the model that answered survives the restart too (read from the transcript)
    assert texts[0].get("engine", {}).get("model"), shown


def _write_transcript(config_dir, cwd: str, csid: str) -> None:
    """A transcript where the CLI files it: projects/<cwd, every non-alphanumeric → « - »>/."""
    d = config_dir / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "sessionId": csid, "cwd": cwd, "uuid": "u1",
         "message": {"role": "user", "content": BRIEF + "Relance le devis de mars."}},
        {"type": "assistant", "sessionId": csid, "cwd": cwd, "uuid": "a1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Brouillon prêt."}]}},
    ]
    (d / f"{csid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


@pytest.mark.parametrize("project", ["atelier", "default"])
def test_thread_is_rebuilt_from_the_transcript_of_the_session_directory(env, project):
    import agentchat
    import board

    csid = "0b5c1a52-6f0e-4c56-9d1f-3a1c5a8f7e10"
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="Relance", project=project, owner="alice@x")
    board.set_claude_session_id(sid, csid)
    _write_transcript(env["tmp"] / "claude", agentchat.project_cwd(sid), csid)

    shown = _plain(agentchat.get_or_create(sid, user="alice@x").events)
    assert {"type": "user", "text": "Relance le devis de mars.", "documents": []} in shown, shown
    assert {"type": "text", "text": "Brouillon prêt."} in shown, shown


def test_transcript_found_when_the_directory_name_has_other_characters(env, monkeypatch):
    """A data folder like « ~/.local/share/sokkan » or a project « rh_2026 »: the CLI replaces
    the dot and the underscore too."""
    import agentchat
    import board

    csid = "7e2f9d10-1111-4222-8333-944455556666"
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="Note", project="atelier", owner="alice@x")
    board.set_claude_session_id(sid, csid)
    odd = env["tmp"] / "data.dir" / "rh_2026" / "work"
    odd.mkdir(parents=True)
    monkeypatch.setattr(agentchat, "project_cwd", lambda _sid: str(odd))
    _write_transcript(env["tmp"] / "claude", str(odd), csid)

    assert agentchat._transcript_path(csid, str(odd)).exists()
    shown = _plain(agentchat.get_or_create(sid, user="alice@x").events)
    assert {"type": "text", "text": "Brouillon prêt."} in shown, shown


def test_no_transcript_is_an_empty_thread_not_an_error(env):
    import agentchat
    import board

    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="Vide", project="atelier", owner="alice@x")
    board.set_claude_session_id(sid, "ffffffff-0000-4000-8000-000000000000")
    assert agentchat.get_or_create(sid, user="alice@x").events == []


def test_a_session_never_reads_the_transcript_of_another_project(env):
    """The session id to resume can come from the browser: only the session's own folder is
    searched, so a work of `default` cannot be handed the conversation of `atelier`."""
    import agentchat
    import board

    csid = "11112222-3333-4444-8555-666677778888"
    theirs = agentchat.new_sid()
    board.add_sdk_session(theirs, "docs", title="Devis", project="atelier", owner="alice@x")
    _write_transcript(env["tmp"] / "claude", agentchat.project_cwd(theirs), csid)
    mine = agentchat.new_sid()
    board.add_sdk_session(mine, "docs", title="Autre", project="default", owner="bob@x")
    assert agentchat.get_or_create(mine, resume=csid, user="bob@x").events == []


def test_study_export_reads_the_transcript_of_a_project_session(env, monkeypatch):
    """Same folder, same cause: the consented content of a work done in a project was exported
    as « no transcript on this instance »."""
    import agentchat
    import board
    import study_export

    csid = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="Relance", project="atelier", owner="alice@x")
    board.set_claude_session_id(sid, csid)
    _write_transcript(env["tmp"] / "claude", agentchat.project_cwd(sid), csid)
    monkeypatch.setattr(study_export, "_session_secrets", lambda _sid: [])
    out = study_export._content({"session_id": sid, "pseudonym": "P1", "scenario": "s1"}, {})
    assert "note" not in out, out
    assert [m.get("text") for m in out["messages"] if m["role"] == "assistant"] == ["Brouillon prêt."]
