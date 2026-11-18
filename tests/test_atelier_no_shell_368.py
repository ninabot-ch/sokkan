"""3.6.8 — no shell in an Atelier work (simple profile), whoever asks.

The parity bench of 16.11.2026 saw shell commands run in an Atelier work without any card:
* `ls -la "…/deliverables/…/" 2>/dev/null || echo "DOES NOT EXIST"` — the CLI approves a
  read-only command by itself and never calls `can_use_tool`, where the refusal lived;
* `pwd && ls -la …/work/ 2>&1 | head -50` run by a SUB-AGENT (Agent tool) — same path;
* refusal texts naming a tool the session did not have (the model then called it).

Unit half: the options a work's CLI is started with, the hook, the callback, the texts, the
thread. End-to-end half: the REAL Claude Code CLI (bundled with claude-agent-sdk) against a
scripted model (tests/mock_anthropic.py) plays the bench's commands, in the work and in a
sub-agent. Opt out of the CLI half: SOKKAN_TEST_E2E_AGENTS=0.
"""
import asyncio
import os
import sys

import pytest

pytest.importorskip("claude_agent_sdk")

sys.path.insert(0, os.path.dirname(__file__))
from mock_anthropic import MockAnthropic, _text_of  # noqa: E402
from harness_helpers import ask, drain  # noqa: E402

# 4.0: the CLI's shell tools and the hook live in the legacy adapter (harness/legacy.py)
from harness import legacy  # noqa: E402

CANARY = "canary-7f3a9c.txt"
SUB = "SUBTASK-1b2c: look at the work folder"


def _denied(out: dict) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    import agentchat
    import audit
    import board
    import hitl_journal
    import notify
    import simple

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(simple, "DB", tmp_path / "simple.db")
    monkeypatch.setattr(simple, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send", lambda *a, **k: {})
    rows = []
    monkeypatch.setattr(hitl_journal, "automatic", lambda *a, **k: rows.append(k))
    monkeypatch.setattr(hitl_journal, "requested", lambda *a, **k: None)
    monkeypatch.setattr(hitl_journal, "decided", lambda *a, **k: None)
    work = tmp_path / "work"
    (work / "deliverables" / ("d" * 32)).mkdir(parents=True)
    (work / CANARY).write_text("x\n")
    (work / "deliverables" / CANARY).write_text("x\n")    # what either `ls` would list
    yield {"tmp": tmp_path, "work": work, "journal": rows, "agentchat": agentchat}


def _session(world, simple_work: bool, policy=None):
    import board
    import simple
    agentchat = world["agentchat"]
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="t", project="default", owner="ana@x")
    if simple_work:
        simple.add_work(sid, "default", "ana@x", "Prépare un plan.", "", "fr")
    s = agentchat.AgentSession(sid, cwd=str(world["work"]), user="ana@x", policy=policy)
    return s


class _FakeClient:
    last = None

    def __init__(self, options, transport=None):
        _FakeClient.last = options

    async def __aenter__(self):
        return self


def _options(world, monkeypatch, simple_work: bool):
    monkeypatch.setattr(legacy, "ClaudeSDKClient", _FakeClient)
    s = _session(world, simple_work)
    asyncio.new_event_loop().run_until_complete(s.ensure_started())
    return s, _FakeClient.last


def _shell_hook(opts):
    ms = [m for m in (opts.hooks or {}).get("PreToolUse") or []
          if m.matcher == "|".join(legacy.SHELL_TOOLS)]
    return ms[0].hooks[0] if ms else None


# ---- the options of an Atelier work ------------------------------------------------------------
def test_an_atelier_work_starts_without_any_shell_tool(world, monkeypatch):
    _s, opts = _options(world, monkeypatch, simple_work=True)
    assert set(legacy.SHELL_TOOLS) <= set(opts.disallowed_tools)
    assert {"Bash", "Monitor"} <= set(opts.disallowed_tools)
    # nothing that writes the document is taken away
    assert not {"Read", "Write", "Edit"} & set(opts.disallowed_tools)
    assert not any(t.startswith("Bash") for t in opts.allowed_tools)


def test_the_hook_refuses_every_shell_call_before_any_allow_rule(world, monkeypatch):
    agentchat = world["agentchat"]
    s, opts = _options(world, monkeypatch, simple_work=True)
    hook = _shell_hook(opts)
    assert hook is not None
    loop = asyncio.new_event_loop()
    for tool, inp in [
        ("Bash", {"command": 'ls -la "/w/deliverables/52fe/" 2>/dev/null || echo "DOES NOT EXIST"'}),
        ("Bash", {"command": "pwd && ls -la /w/ 2>&1 | head -50"}),     # the sub-agent's
        ("Monitor", {"command": "tail -f x", "description": "d", "timeout_ms": 1000}),
    ]:
        out = loop.run_until_complete(hook({"tool_name": tool, "tool_input": inp}, None, None))
        assert _denied(out), tool
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == agentchat.NO_SHELL
    drain(s)                                  # the adapter told the cockpit (4.0: an event)
    # journaled as a rule of the space (nobody clicked), one line per call
    assert [r["reason"] for r in world["journal"]] == ["policy:simple_profile_no_shell"] * 3
    assert not any(r["allowed"] for r in world["journal"])


def test_the_cockpit_keeps_its_shell(world, monkeypatch):
    _s, opts = _options(world, monkeypatch, simple_work=False)
    assert "Bash" not in (opts.disallowed_tools or [])
    assert _shell_hook(opts) is None


def test_the_callback_refuses_every_shell_tool_too(world):
    agentchat = world["agentchat"]
    s = _session(world, simple_work=True)
    s.mode = "bypassPermissions"            # even a widened mode does not open the shell
    loop = asyncio.new_event_loop()
    for tool in ("Bash", "Monitor"):
        res = loop.run_until_complete(ask(s, tool, {"command": "ls"}))
        assert res.denied and res.message == agentchat.NO_SHELL
    assert not [e for e in s.events if e.get("type") == "permission"]


def test_the_refusal_names_only_tools_the_work_has(world):
    agentchat = world["agentchat"]
    for word in ("Write", "Edit", "Read"):
        assert f"{word}" in agentchat.NO_SHELL
    for absent in (*legacy.SHELL_TOOLS, "mcp__"):
        assert absent not in agentchat.NO_SHELL


# ---- memory refusals name the MCP tool only when the session has it ----------------------------
def test_a_memory_refusal_names_the_mcp_tool_only_when_the_session_has_it(world):
    agentchat = world["agentchat"]
    mem = str(world["tmp"] / "memory" / "note.md")
    loop = asyncio.new_event_loop()

    def refuse(policy):
        s = _session(world, simple_work=False, policy=policy)
        orig = agentchat._closed_to_agent_run
        agentchat._closed_to_agent_run = lambda path, project: True
        try:
            return loop.run_until_complete(ask(s, "Write", {"file_path": mem, "content": "x"}))
        finally:
            agentchat._closed_to_agent_run = orig

    base = {"agent": "a", "run": "r", "tools": ["Write"], "auto_approve": []}
    without = refuse({**base, "mcp": []})
    assert without.denied
    assert "mcp__" not in without.message and "does not write memory files directly" in without.message
    with_mem = refuse({**base, "mcp": ["sokkan-memory"]})
    assert "mcp__sokkan-memory__memory_write" in with_mem.message
    # a person's session always has the memory server: named
    assert "mcp__sokkan-memory__memory_write" in _session(world, False)._memory_write_way()


# ---- the thread: a shell refused is not an orange failure -------------------------------------
@pytest.mark.parametrize("said", [
    "<tool_use_error>Error: No such tool available: Bash. Bash is disabled for this session, "
    "in subagents as well as here.</tool_use_error>",
    "No shell in this conversation: the person is not a developer. …",
])
def test_a_refused_shell_is_not_shown_as_a_failure(tmp_path, said):
    import plain
    deliv = tmp_path / "deliverables" / ("c" * 32)
    deliv.mkdir(parents=True)
    v = plain.View(docs_dir=tmp_path / "documents", deliv_dir=deliv, on_write=lambda *a: 1)
    v.feed({"type": "tool_use", "id": "t1", "tool": "Bash", "input": {"command": "ls"}})
    (done,) = v.feed({"type": "tool_result", "tool_use_id": "t1", "text": said, "is_error": True})
    assert done["ok"] is False and done["reason"] == "policy"   # « skipped »: not in the thread


# ---- end to end: the real CLI ------------------------------------------------------------------
def _bundled_cli() -> bool:
    import claude_agent_sdk
    d = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled")
    return os.path.exists(os.path.join(d, "claude"))


e2e = pytest.mark.skipif(os.environ.get("SOKKAN_TEST_E2E_AGENTS") == "0" or not _bundled_cli(),
                         reason="SOKKAN_TEST_E2E_AGENTS=0 or no bundled CLI")


class Bench(MockAnthropic):
    """Plays the bench's calls: the work runs `ls` (read-only, the A2 form) then hands a task
    to a sub-agent; the sub-agent runs `pwd && ls` (the R2 form). Any other request: text."""

    def __init__(self, work: str, main_calls):
        super().__init__()
        self.main_calls = main_calls
        self.sub_calls = [("Bash", {"command": f"pwd && ls -la {work}/ 2>&1 | head -50",
                                    "description": "Show working directory"})]

    def answer(self, req, mid):
        msgs = req.get("messages") or []
        if not req.get("tools") or not msgs:
            return {"type": "text", "text": "ok"}, "end_turn"
        first = _text_of(msgs[0].get("content"))
        calls = self.sub_calls if SUB in first and "Plan" not in first else self.main_calls
        n = sum(1 for m in msgs if isinstance(m.get("content"), list)
                for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
        if n < len(calls):
            name, inp = calls[n]
            if name == "@agent":
                tools = {t.get("name") for t in req.get("tools") or []}
                name = "Agent" if "Agent" in tools else "Task"
            return {"type": "tool_use", "id": f"toolu_{n}_{mid}", "name": name,
                    "input": inp}, "tool_use"
        return {"type": "text", "text": "Voilà."}, "end_turn"

    def results(self) -> list[dict]:
        return [b for r in self.main_requests() for m in r.get("messages") or []
                if isinstance(m.get("content"), list)
                for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]

    def tools_offered(self, sub: bool) -> set:
        out = set()
        for r in self.main_requests():
            first = _text_of((r.get("messages") or [{}])[0].get("content"))
            if (SUB in first and "Plan" not in first) == sub:
                out |= {t.get("name") for t in r.get("tools") or []}
        return out


@pytest.fixture()
def cli_env(world, monkeypatch):
    agentchat = world["agentchat"]
    monkeypatch.setattr(agentchat.memrecall, "provider", lambda *a, **k: None)
    for k, v in {"ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CONFIG_DIR": str(world["tmp"] / "claude"),
                 "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                 "DISABLE_AUTOUPDATER": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return world


def _play(world, monkeypatch, simple_work: bool):
    work = str(world["work"])
    main = [("Bash", {"command": f'ls -la "{work}/deliverables/" 2>/dev/null || echo "DOES NOT EXIST"',
                      "description": "Check deliverables folder exists"}),
            ("@agent", {"description": "look into it", "prompt": SUB,
                        "subagent_type": "general-purpose"})]
    with Bench(work, main) as mock:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.url)
        s = _session(world, simple_work)
        emit = s._emit
        cards = []

        def clicker(ev):
            # the person approves the hand-over to a sub-agent (a card, as in the Atelier);
            # any other card is recorded and refused — a shell must never reach one
            emit(ev)
            if ev.get("type") == "permission":
                cards.append(ev["tool"])
                ok = ev["tool"] in ("Agent", "Task")
                asyncio.get_running_loop().call_soon(
                    s.resolve_permission, ev["id"], {"decision": "allow" if ok else "deny"})

        s._emit = clicker

        async def go():
            # a card would wait for a click that never comes: the turn would time out
            await asyncio.wait_for(s.handle_user("Plan de formation, s'il te plaît."), 120)
            await s.close()

        asyncio.new_event_loop().run_until_complete(go())
        s.cards = cards
        return s, mock


@e2e
def test_no_shell_runs_in_an_atelier_work_nor_in_its_sub_agent(cli_env, monkeypatch):
    s, mock = _play(cli_env, monkeypatch, simple_work=True)
    res = mock.results()
    assert len(res) >= 2, res
    everything = str(mock.requests)
    assert CANARY not in everything, "a shell command ran"        # neither ls listed the folder
    # 4.0.1: an Atelier work has no sub-agent at all (Agent / Task out of its catalogue,
    # test_atelier_tools_off_401.py): the hand-over is refused, no sub-agent ever starts
    assert not {"Agent", "Task"} & mock.tools_offered(sub=False)
    # nor any other tool its space refuses, in the catalogue the REAL CLI sends the model
    assert not set(legacy.ATELIER_OFF_TOOLS) & mock.tools_offered(sub=False)
    assert {"Read", "Write", "Edit"} <= mock.tools_offered(sub=False)
    assert not any(SUB in _text_of((r.get("messages") or [{}])[0].get("content"))
                   for r in mock.main_requests() if "Plan" not in str(r.get("messages", [{}])[0]))
    # no shell tool in the catalogue, and no card for anyone
    assert "Bash" not in mock.tools_offered(sub=False)
    assert set(s.cards) <= {"Agent", "Task"}, s.cards     # never a card for a shell
    # each refusal reached the thread as a rule of the space, not as a failure
    import plain
    v = plain.View(docs_dir=None, deliv_dir=None, on_write=lambda *a: 1)
    shell_ids = {e["id"] for e in s.events if e.get("type") == "tool_use" and e.get("tool") == "Bash"}
    for e in s.events:
        for out in v.feed(e):
            if out.get("type") == "activity_done" and out["id"] in shell_ids:
                assert out.get("reason") == "policy", out


@e2e
def test_cause_the_cli_runs_a_read_only_command_without_asking(cli_env, monkeypatch):
    """Why the refusal in can_use_tool was not enough (3.6.7): in a session with a shell, the
    CLI runs a read-only `ls` — and the sub-agent's `pwd && ls` — without calling it."""
    s, mock = _play(cli_env, monkeypatch, simple_work=False)
    assert CANARY in str(mock.results())
    # the Cockpit keeps its catalogue (4.0.1 changes the Atelier only)
    assert {"Bash", "WebFetch", "WebSearch"} <= mock.tools_offered(sub=False)
    assert {"Agent", "Task"} & mock.tools_offered(sub=False)
    assert set(s.cards) <= {"Agent", "Task"}, s.cards     # both `ls` ran without a card
