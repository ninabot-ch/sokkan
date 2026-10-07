"""3.2 lot 8 end to end: the REAL Claude Code CLI (bundled with claude-agent-sdk) against a
scripted model (tests/mock_anthropic.py), on the real hook + permission path.

* an agent run of project `radio` tries to Read a file of project `tv` → refused by the
  sandbox hook; then `cat`s it through Bash (auto-approved by its `Bash(cat:*)` rule) →
  refused (hooks-only) or run inside bubblewrap where the file does not exist (bwrap);
  its own workspace file is read normally;
* a human session of `radio` in bypass mode: the CLI runs the command the hook rewrote
  (proves `updatedInput` is honoured), so `cat` of another project's file fails;
* the same session in `default` reads it: the default project keeps its behaviour.

Opt out: SOKKAN_TEST_E2E_AGENTS=0. bwrap cases need bubblewrap (or SOKKAN_TEST_BWRAP).
"""
import asyncio
import os
import shutil
import sys

import pytest

pytest.importorskip("claude_agent_sdk")
pytestmark = pytest.mark.skipif(os.environ.get("SOKKAN_TEST_E2E_AGENTS") == "0",
                                reason="SOKKAN_TEST_E2E_AGENTS=0")

sys.path.insert(0, os.path.dirname(__file__))
from mock_anthropic import MockAnthropic, _text_of  # noqa: E402

BW = os.environ.get("SOKKAN_TEST_BWRAP") or shutil.which("bwrap") or ""
DEV = {"email": "alice@x", "role": "dev", "project": "radio"}


def _bundled_cli() -> bool:
    import claude_agent_sdk
    d = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled")
    return os.path.exists(os.path.join(d, "claude"))


def _bwrap_works() -> bool:
    import sandbox
    return bool(BW) and sandbox._probe(BW) is None


class Scripted(MockAnthropic):
    """Plays a fixed list of tool calls, one per turn, then ends with DELIVERY: done."""

    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    def answer(self, req, mid):
        msgs = req.get("messages") or []
        if not req.get("tools") or not msgs:
            return {"type": "text", "text": "ok"}, "end_turn"
        n = sum(1 for m in msgs if isinstance(m.get("content"), list)
                for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
        if n < len(self.calls):
            name, inp = self.calls[n]
            return {"type": "tool_use", "id": f"toolu_{n}_{mid}", "name": name,
                    "input": inp}, "tool_use"
        return {"type": "text", "text": "Done.\nDELIVERY: done"}, "end_turn"


def _results(mock) -> list[dict]:
    last = mock.main_requests()[-1]
    return [b for m in last["messages"] if isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agents
    import agents_runtime
    import audit
    import board
    import notify
    import projects
    import sandbox
    import vault

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_SANDBOX", "1")
    monkeypatch.setenv("SOKKAN_AGENTS_USE_CLI_LOGIN", "1")
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(agents_runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(notify, "send", lambda *a, **k: {})
    projects.create("radio", "Radio", created_by="admin@x")
    projects.create("tv", "TV", created_by="admin@x")
    projects.grant("radio", "user", "alice@x", "dev")
    pr = tmp_path / "projects"
    for p in ("radio", "tv"):
        (pr / p / "work").mkdir(parents=True, exist_ok=True)
    (pr / "radio" / "work" / "mine.txt").write_text("radio-own-content\n")
    secret = pr / "tv" / "work" / "secret.txt"
    secret.write_text("tv-confidential\n")
    for k, v in {"ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
                 "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                 "DISABLE_AUTOUPDATER": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    sandbox.reset()
    yield {"tmp": tmp_path, "secret": str(secret)}
    sandbox.reset()


def _mode(monkeypatch, mode):
    import sandbox
    if mode == "bwrap":
        if not _bwrap_works():
            pytest.skip("bubblewrap not usable here")
        monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", BW)
    else:
        monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", "/nonexistent/bwrap")
    sandbox.reset()
    assert sandbox.mode() == mode


@pytest.mark.skipif(not _bundled_cli(), reason="claude-agent-sdk without bundled CLI")
@pytest.mark.parametrize("mode", ["hooks-only", "bwrap"])
def test_agent_run_of_a_project_cannot_read_another_project(env, monkeypatch, mode):
    import agents
    import agents_runtime
    _mode(monkeypatch, mode)
    calls = [("Read", {"file_path": env["secret"]}),
             ("Bash", {"command": f"cat {env['secret']}", "description": "read"}),
             ("Bash", {"command": "cat mine.txt", "description": "read own"})]
    with Scripted(calls) as mock:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.url)
        a = agents.create(DEV, dict(
            name="radio-reader", purpose="Read files.", deliverable="What you read.",
            done_criteria="files read", trigger="manual", tools=["Read", "Bash"],
            auto_approve=["Bash(cat:*)"], mcp=["sokkan-memory"], outputs=["file"],
            budget_usd=1.0, max_minutes=3), activate=True)
        assert a["project"] == "radio"
        run = agents.request_run(DEV, a["id"])
        rt = agents_runtime.Runtime(recall=lambda q, sid: "")

        async def go():
            rt.start_queued()
            for _ in range(900):
                if not rt.tasks:
                    break
                await asyncio.sleep(0.1)
            assert not rt.tasks, "the run did not finish in 90 s"

        asyncio.new_event_loop().run_until_complete(go())
        done = agents.get_run(run["id"])
        assert done["status"] == "succeeded", done
        res = _results(mock)
        assert len(res) == 3, res
        assert res[0].get("is_error") and "Sandbox" in _text_of(res[0].get("content"))
        if mode == "hooks-only":
            assert res[1].get("is_error") and "bubblewrap" in _text_of(res[1].get("content"))
            assert res[2].get("is_error")
        else:
            assert "radio-own-content" in _text_of(res[2].get("content"))
        everything = str(mock.requests)
        assert "tv-confidential" not in everything


@pytest.mark.skipif(not _bundled_cli(), reason="claude-agent-sdk without bundled CLI")
@pytest.mark.parametrize("project", ["radio", "default"])
def test_human_session_bash_is_wrapped_by_the_cli(env, monkeypatch, project):
    import agentchat
    import board
    _mode(monkeypatch, "bwrap")
    calls = [("Bash", {"command": f"cat {env['secret']}", "description": "read"})]
    with Scripted(calls) as mock:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.url)
        sid = agentchat.new_sid()
        board.add_sdk_session(sid, "t", project=project)
        work = env["tmp"] / "default-work"
        work.mkdir(exist_ok=True)
        cwd = str(work) if project == "default" else agentchat.project_cwd(sid)
        s = agentchat.AgentSession(sid, cwd=cwd, user="alice@x")
        s.mode = "bypassPermissions"     # nobody to click: the permission callback allows

        async def go():
            await asyncio.wait_for(s.handle_user("read it"), 90)
            await s.close()

        asyncio.new_event_loop().run_until_complete(go())
        res = _results(mock)
        assert len(res) == 1, res
        out = _text_of(res[0].get("content"))
        if project == "radio":
            assert s.sandboxed == "radio"
            assert "tv-confidential" not in out and "No such file" in out
        else:  # default: unchanged — the shell reads whatever the API user can read
            assert s.sandboxed is None
            assert "tv-confidential" in out
