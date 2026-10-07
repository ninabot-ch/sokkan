"""End to end: an agent run drives the board through the REAL Claude Code CLI
(bundled with claude-agent-sdk) and the REAL sokkan-board MCP server (a child
process of the CLI), against a scripted Messages API — no credit spent. Proves:

* the board tools are offered to a run whose agent was granted `sokkan-board`;
* reads (search_cards, get_card) run without asking anyone;
* a write in the agent's `auto_approve` (comment_card) runs without asking;
* a write outside it (close_card) waits for a human on the normal gate, and runs
  once approved;
* every action is signed by the agent, with the run's session and run id — the
  identity comes from the env the API sets, not from the model;
* the deliverable card the run files is linked to the run and the agent.

Opt out: SOKKAN_TEST_E2E_AGENTS=0 (same switch as the Crew e2e).
"""
import asyncio
import os
import sys
import tempfile

import pytest

pytest.importorskip("claude_agent_sdk")
pytestmark = pytest.mark.skipif(os.environ.get("SOKKAN_TEST_E2E_AGENTS") == "0",
                                reason="SOKKAN_TEST_E2E_AGENTS=0")

sys.path.insert(0, os.path.dirname(__file__))
from mock_anthropic import MockAnthropic, _text_of  # noqa: E402
from test_agents_e2e_cli import _bundled_cli  # noqa: E402

DEV = {"email": "dev@x.ch", "role": "dev"}
B = "mcp__sokkan-board__"


class BoardModel(MockAnthropic):
    """search → get → comment (auto-approved) → close (gated) → deliverable."""

    def __init__(self, card_id: int):
        super().__init__()
        self.card_id = card_id
        self.waited = 0

    def answer(self, req, mid):
        msgs = req.get("messages") or []
        if not req.get("tools") or not msgs:
            return {"type": "text", "text": "ok"}, "end_turn"
        names = {}
        for m in msgs:
            for b in m.get("content") if isinstance(m.get("content"), list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
        done = [b for m in msgs if isinstance(m.get("content"), list) for b in m["content"]
                if isinstance(b, dict) and b.get("type") == "tool_result"
                and names.get(b.get("tool_use_id"), "").startswith(B)]
        offered = {t.get("name") for t in req.get("tools") or []}
        if B + "get_card" not in offered and "WaitForMcpServers" in offered:
            # the CLI starts before its MCP servers are connected: wait like a model would
            self.waited += 1
            if self.waited > 20:
                return {"type": "text", "text": "board never connected\nDELIVERY: incomplete"}, "end_turn"
            return ({"type": "tool_use", "id": f"toolu_wait{mid}", "name": "WaitForMcpServers",
                     "input": {}}, "tool_use")
        steps = [
            (B + "search_cards", {"query": "flaky checkout"}),
            (B + "get_card", {"card_id": self.card_id}),
            (B + "comment_card", {"card_id": self.card_id,
                                  "body": "Re-ran the suite 20 times: green. Root cause was the clock mock."}),
            (B + "close_card", {"card_id": self.card_id, "resolution": "fixed by pinning the clock"}),
        ]
        n = len(done)
        if n < len(steps):
            name, inp = steps[n]
            return {"type": "tool_use", "id": f"toolu_{n}_{mid}", "name": name, "input": inp}, "tool_use"
        return {"type": "text", "text": "## Flaky test\nClosed the card.\nDELIVERY: done"}, "end_turn"


@pytest.mark.skipif(not _bundled_cli(), reason="claude-agent-sdk without bundled CLI")
def test_agent_run_drives_the_board_through_the_real_cli(tmp_path, monkeypatch):
    import agents
    import agents_runtime
    import audit
    import board
    import iam
    import notify

    # the MCP server is a child process: it finds the stores through the env
    for k, v in {"SOKKAN_DATA_DIR": str(tmp_path), "SOKKAN_BOARD_DB": str(tmp_path / "board.db"),
                 "SOKKAN_AGENTS_DB": str(tmp_path / "agents.db"),
                 "SOKKAN_AUDIT_DB": str(tmp_path / "audit.db"),
                 "SOKKAN_IAM_DB": str(tmp_path / "iam.db")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    iam.upsert_user("dev@x.ch", "dev")
    monkeypatch.setattr(agents_runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(notify, "send", lambda *a, **k: {})
    for k in ("SOKKAN_SESSION_ID", "SOKKAN_SESSION_USER", "SOKKAN_AGENT_RUN", "TMUX_PANE"):
        monkeypatch.delenv(k, raising=False)
    work = tempfile.mkdtemp(dir=tmp_path)
    card = board.add_card("Flaky checkout test", description="test_checkout fails 1 run in 10",
                          tag="bugfix", bucket="Doing", user="dev@x.ch")

    with BoardModel(card["id"]) as mock:
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
            name="flaky-hunter", purpose="Find flaky tests on the board and close the fixed ones.",
            deliverable="One line per card handled.", trigger="manual", tools=["Read", B + "comment_card"],
            mcp=["sokkan-memory", "sokkan-board"], auto_approve=[B + "comment_card"],
            outputs=["card"], budget_usd=1.0, max_minutes=3), activate=True)
        run = agents.request_run(DEV, a["id"])
        rt = agents_runtime.Runtime(recall=lambda q, sid: "")
        asked: list[str] = []

        async def go():
            rt.start_queued()
            for _ in range(900):
                if not rt.tasks:
                    break
                for sess in list(rt.sessions.values()):
                    for ev in list(sess.events):
                        if ev.get("type") == "permission" and ev["id"] in sess._perms \
                                and ev["id"] not in asked:
                            asked.append(ev["id"])
                            asked.append(ev["tool"])
                            sess.resolve_permission(ev["id"], {"decision": "allow"})
                await asyncio.sleep(0.1)
            assert not rt.tasks, "the run did not finish in 90 s"

        asyncio.new_event_loop().run_until_complete(go())

    done = agents.get_run(run["id"])
    assert done["status"] == "succeeded", done
    main = mock.main_requests()
    # MCP servers may still be connecting on the first request: union over the run
    offered = {t.get("name") for r in main for t in r.get("tools") or []}
    assert {B + "get_card", B + "search_cards", B + "comment_card", B + "close_card",
            B + "update_card", B + "link_card"} <= offered
    last = main[-1]["messages"]
    names = {b["id"]: b["name"] for m in last if isinstance(m.get("content"), list)
             for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
    results = [b for m in last if isinstance(m.get("content"), list) for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"
               and names.get(b.get("tool_use_id"), "").startswith(B)]
    assert len(results) == 4 and not any(b.get("is_error") for b in results), results
    assert "Flaky checkout test" in _text_of(results[0].get("content"))  # search found it
    # only the write outside auto_approve asked a human
    assert [x for x in asked if x.startswith("mcp__")] == [B + "close_card"]

    sid = done["session_id"]
    d = board.card_detail(card["id"])
    assert d["bucket"] == "Done" and d["closed_by"] == "agent:flaky-hunter"
    com = d["comments"][0]
    assert com["author"] == "agent:flaky-hunter" and com["session_id"] == sid
    assert com["via"] == f"agent-run #{run['id']}" and "clock mock" in com["body"]
    mine = [e for e in d["events"] if e["action"] in ("commented", "closed")]
    assert len(mine) == 2 and all(e["user"] == "agent:flaky-hunter" and e["session_id"] == sid
                                  for e in mine)
    acts = {e["action"] for e in audit.recent(50)}
    assert {"board.card.comment", "board.card.close"} <= acts
    # the deliverable card knows its run and its agent
    out = board.card_detail(done["outputs"]["card"])
    kinds = {(li["kind"], li["ref"]) for li in out["links"]}
    assert ("run", str(run["id"])) in kinds and ("agent", str(a["id"])) in kinds
