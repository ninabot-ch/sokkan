"""End to end: an agent run through the REAL Claude Code CLI (bundled with
claude-agent-sdk) against a scripted Messages API (tests/mock_anthropic.py) — no
credit spent. Proves, on the real permission path:

* the run is an ordinary SDK session started by the scheduler, with the agent's
  mission + deliverable in the first message the model sees;
* a tool outside the agent's list (Write) is not offered to the model, and refused
  WITHOUT asking a human when it is called anyway;
* an auto-approved rule (`Bash(printenv:*)`) runs without asking;
* the vault secret is injected by NAME (the model's prompt never holds the value),
  reaches the shell, and is redacted from the stored deliverable and the board card;
* status, cost/tokens, the card in Review and the audit trail are recorded.

Runs by default when the bundled CLI is present (~10-20 s). Opt out:
SOKKAN_TEST_E2E_AGENTS=0.
"""
import asyncio
import os
import tempfile

import pytest

pytest.importorskip("claude_agent_sdk")
pytestmark = pytest.mark.skipif(os.environ.get("SOKKAN_TEST_E2E_AGENTS") == "0",
                                reason="SOKKAN_TEST_E2E_AGENTS=0")

import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from mock_anthropic import MockAnthropic, _text_of, all_text  # noqa: E402

SECRET = "tok_e2e_7f3a9c1d5b2e8f40"
DEV = {"email": "dev@x.ch", "role": "dev"}


def _bundled_cli() -> bool:
    import claude_agent_sdk

    d = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled")
    return os.path.exists(os.path.join(d, "claude"))


class AgentModel(MockAnthropic):
    """Scripted model for an agent run: try Write (must be refused by policy), then
    printenv the secret (auto-approved), then hand back the deliverable quoting what
    the shell printed — the test checks it is redacted when stored."""

    def answer(self, req, mid):
        msgs = req.get("messages") or []
        if not req.get("tools") or not msgs:
            return {"type": "text", "text": "ok"}, "end_turn"
        results = []
        for m in msgs:
            c = m.get("content")
            if isinstance(c, list):
                results += [b for b in c if isinstance(b, dict) and b.get("type") == "tool_result"]
        if len(results) == 0:
            return ({"type": "tool_use", "id": f"toolu_w{mid}", "name": "Write",
                     "input": {"file_path": "/tmp/should-not-exist.txt", "content": "x"}},
                    "tool_use")
        if len(results) == 1:
            return ({"type": "tool_use", "id": f"toolu_b{mid}", "name": "Bash",
                     "input": {"command": "printenv E2E_TOKEN", "description": "check token"}},
                    "tool_use")
        printed = _text_of(results[-1].get("content")).strip()
        return {"type": "text", "text": (
            "## Token check\n"
            f"The deploy token is present: {printed}\n"
            "Write was refused by policy, as expected.\n"
            "DELIVERY: done")}, "end_turn"


@pytest.mark.skipif(not _bundled_cli(), reason="claude-agent-sdk without bundled CLI")
def test_agent_run_through_the_real_cli(tmp_path, monkeypatch):
    import agents
    import agents_runtime
    import audit
    import board
    import notify
    import vault

    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(agents_runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(notify, "send", lambda *a, **k: {})
    vault.set_secret("E2E_TOKEN", SECRET)
    vault.set_secret("UNRELATED", "unrelated_secret_value_42")
    work = tempfile.mkdtemp(dir=tmp_path)

    with AgentModel() as mock:
        for k, v in {"ANTHROPIC_BASE_URL": mock.url, "ANTHROPIC_API_KEY": "sk-test",
                     "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"), "DISABLE_TELEMETRY": "1",
                     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                     "DISABLE_AUTOUPDATER": "1"}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        import agentchat
        monkeypatch.setattr(agentchat, "CWD", work)
        orig_goc = agentchat.get_or_create

        def goc(sid, **kw):
            s = orig_goc(sid, **kw)
            s.cwd = work
            return s
        monkeypatch.setattr(agentchat, "get_or_create", goc)

        a = agents.create(DEV, dict(
            name="e2e-token-check", purpose="Check that the deploy token is configured.",
            deliverable="One line saying whether the token is present.",
            done_criteria="the token presence is verified", trigger="manual",
            tools=["Read", "Bash"], auto_approve=["Bash(printenv:*)"],
            secrets=["E2E_TOKEN"], mcp=["sokkan-memory"], outputs=["card", "file"],
            budget_usd=1.0, max_minutes=3), activate=True)
        run = agents.request_run(DEV, a["id"])
        rt = agents_runtime.Runtime(recall=lambda q, sid: "")

        async def go():
            rt.start_queued()
            for _ in range(600):
                if not rt.tasks:
                    break
                await asyncio.sleep(0.1)
            assert not rt.tasks, "the run did not finish in 60 s"

        asyncio.new_event_loop().run_until_complete(go())

        done = agents.get_run(run["id"])
        assert done["status"] == "succeeded", done
        main = mock.main_requests()
        assert main, "the CLI never called the model"
        first = all_text({"messages": main[0]["messages"][:1]})
        assert 'SOKKAN agent "e2e-token-check"' in first
        assert "Check that the deploy token is configured." in first
        assert "$E2E_TOKEN" in first
        everything_the_model_saw_before_the_shell = all_text(main[0])
        assert SECRET not in everything_the_model_saw_before_the_shell
        # Write is not even offered to the model (disallowed_tools), and the call it
        # tries anyway is refused — no human involved, no file written
        offered = {t.get("name") for t in main[0].get("tools") or []}
        assert "Write" not in offered and "Edit" not in offered and "Bash" in offered
        results = [b for r in main for m in r["messages"]
                   if isinstance(m.get("content"), list)
                   for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        assert results[0].get("is_error") is True
        assert not os.path.exists("/tmp/should-not-exist.txt")
        # Bash(printenv:*) ran without approval and saw the secret from the vault
        assert any(SECRET in _text_of(b.get("content")) for b in results)
        # the stored deliverable is redacted, everywhere it was filed
        assert SECRET not in done["deliverable"]
        assert "[secret:E2E_TOKEN]" in done["deliverable"]
        card = board.get_card(done["outputs"]["card"])
        assert card["bucket"] == "Review" and SECRET not in card["description"]
        assert SECRET not in open(done["outputs"]["file"]).read()
        assert done["tokens_out"] > 0 and done["num_turns"] >= 1 and done["session_id"]
        assert done["waiting_approval"] is False
        acts = [e["action"] for e in audit.recent(50)]
        assert "agent.run.start" in acts and "agent.run.end" in acts
        assert SECRET not in str(audit.recent(50))
