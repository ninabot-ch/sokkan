"""4.0.1 — an Atelier work no longer sees the engine tools its space refuses.

4.0.0 still offered the agent of an Atelier work WebFetch, WebSearch, Agent, CronCreate,
Workflow, EnterWorktree… (launch shoot, 18.11.2026), filtered only downstream. They leave its
catalogue as the shell did in 3.6.8, on both engines:

* legacy (Claude Agent SDK): ``disallowed_tools`` of the options + a PreToolUse hook that
  refuses one called anyway;
* kernel: ``deny_tools`` of the session it opens;
* the cockpit's gate as a last line.

The Cockpit keeps them.
"""
import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("claude_agent_sdk")
httpx = pytest.importorskip("httpx")

sys.path.insert(0, os.path.dirname(__file__))
from harness_helpers import ask, drain  # noqa: E402

from harness import contract, kernel, legacy, refusals  # noqa: E402

# what the brief of the defect names, per engine (the lists may hold more)
LEGACY_OFF = {"WebFetch", "WebSearch", "Agent", "Task", "CronCreate", "CronDelete", "CronList",
              "Workflow", "EnterWorktree", "ExitWorktree"}
KERNEL_OFF = {"web_search", "web_fetch", "delegate", "delegate_status", "delegate_cancel",
              "agent_draft"}
# what an Atelier work must keep: reading, writing its document, its to-do list, questions
LEGACY_KEPT = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "TaskCreate", "TaskGet",
               "TaskList", "TaskUpdate", "TodoWrite", "AskUserQuestion", "Skill"}
KERNEL_KEPT = {"read_file", "search_files", "write_file", "patch", "todo", "clarify",
               "skill_list", "skill_view", "recall_context", "checkpoint_list"}


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
    yield {"tmp": tmp_path, "work": work, "journal": rows, "agentchat": agentchat}


def _session(world, simple_work: bool):
    import board
    import simple
    agentchat = world["agentchat"]
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="t", project="default", owner="ana@x")
    if simple_work:
        simple.add_work(sid, "default", "ana@x", "Prépare un plan.", "", "fr")
    return agentchat.AgentSession(sid, cwd=str(world["work"]), user="ana@x")


def _spec(world, simple_work: bool) -> tuple:
    s = _session(world, simple_work)
    return s, asyncio.new_event_loop().run_until_complete(s._run_spec())


# ---- the lists ---------------------------------------------------------------------------------
def test_the_lists_name_what_the_defect_names_and_nothing_a_work_needs():
    assert LEGACY_OFF <= set(legacy.ATELIER_OFF_TOOLS) == set(refusals.ATELIER_OFF_LEGACY)
    assert KERNEL_OFF <= set(refusals.ATELIER_OFF_KERNEL)
    assert not LEGACY_KEPT & refusals.ATELIER_OFF
    assert not KERNEL_KEPT & refusals.ATELIER_OFF
    # the kernel's Atelier list keeps the shell and memory tools of 3.6.8 / 4.0
    assert {"terminal", "process_start", "process_logs", "memory_note"} <= set(kernel.ATELIER_DENIED)
    assert KERNEL_OFF <= set(kernel.ATELIER_DENIED)


def test_the_refusal_is_a_rule_of_the_space_and_names_no_tool():
    import plain
    assert any(m in refusals.ATELIER_NO_TOOL for m in plain.POLICY_DENIED)
    for name in (*refusals.ATELIER_OFF, *legacy.SHELL_TOOLS, "mcp__"):
        assert name not in refusals.ATELIER_NO_TOOL, name


# ---- legacy engine: the options --------------------------------------------------------------
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


def _off_hook(opts):
    ms = [m for m in (opts.hooks or {}).get("PreToolUse") or []
          if m.matcher == "|".join(legacy.ATELIER_OFF_TOOLS)]
    return ms[0].hooks[0] if ms else None


def test_legacy_an_atelier_work_starts_without_these_tools(world, monkeypatch):
    _s, opts = _options(world, monkeypatch, simple_work=True)
    off = set(opts.disallowed_tools)
    assert LEGACY_OFF <= off
    assert set(legacy.SHELL_TOOLS) <= off                     # 3.6.8 still holds
    assert not LEGACY_KEPT & off
    assert not LEGACY_OFF & {t.split("(", 1)[0] for t in opts.allowed_tools}
    assert _off_hook(opts) is not None


def test_legacy_the_cockpit_keeps_them(world, monkeypatch):
    _s, opts = _options(world, monkeypatch, simple_work=False)
    assert not LEGACY_OFF & set(opts.disallowed_tools or [])
    assert _off_hook(opts) is None


# ---- legacy engine: the hook -----------------------------------------------------------------
def test_legacy_the_hook_refuses_each_of_them_and_journals_it(world, monkeypatch):
    s, opts = _options(world, monkeypatch, simple_work=True)
    hook = _off_hook(opts)
    loop = asyncio.new_event_loop()
    for tool in sorted(LEGACY_OFF):
        out = loop.run_until_complete(hook({"tool_name": tool, "tool_input": {"q": "x"}},
                                           None, None))
        assert _denied(out), tool
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == refusals.ATELIER_NO_TOOL
    drain(s)
    reasons = [r["reason"] for r in world["journal"]]
    assert reasons == ["policy:simple_profile_tool_off"] * len(LEGACY_OFF)
    assert not any(r["allowed"] for r in world["journal"])


def test_legacy_the_hook_matches_by_exact_name_only(world, monkeypatch):
    """« Task » in the matcher must not refuse « TaskCreate » (the to-do list) if the CLI reads
    the alternation as a regex: the hook itself checks the exact name."""
    _s, opts = _options(world, monkeypatch, simple_work=True)
    hook = _off_hook(opts)
    loop = asyncio.new_event_loop()
    for tool in ("TaskCreate", "TaskUpdate", "Read", "Write", "ListAgentsX"):
        out = loop.run_until_complete(hook({"tool_name": tool, "tool_input": {}}, None, None))
        assert out == {}, tool
    assert world["journal"] == []


def test_legacy_a_removed_tool_called_anyway_reads_as_a_rule_of_the_space(world, monkeypatch):
    import plain
    from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
    monkeypatch.setattr(legacy, "ClaudeSDKClient", _FakeClient)
    h = legacy.LegacyHarness(client=object())
    h._options(contract.RunSpec(run_id="r", cwd=str(world["tmp"]), shell=False))
    h._called = {}
    h.translate(AssistantMessage(content=[ToolUseBlock(id="t1", name="WebSearch",
                                                       input={"query": "x"})], model="m"))
    (res,) = [e for e in h.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="t1", content="<tool_use_error>Error: No such tool available: WebSearch"
        "</tool_use_error>", is_error=True)])) if isinstance(e, contract.ToolResult)]
    assert res.refused and res.output.startswith(refusals.ATELIER_NO_TOOL)
    v = plain.View(docs_dir=None, deliv_dir=None, on_write=lambda *a: 1)
    v.feed({"type": "tool_use", "id": "t1", "tool": "WebSearch", "input": {"query": "x"}})
    (done,) = v.feed({"type": "tool_result", "tool_use_id": "t1", "text": res.output,
                      "is_error": True})
    assert done["ok"] is False and done["reason"] == "policy"


# ---- the cockpit's gate (last line, any engine) ----------------------------------------------
@pytest.mark.parametrize("tool", ["WebSearch", "WebFetch", "Agent", "Task", "CronCreate",
                                  "Workflow", "EnterWorktree", "web_search", "delegate"])
def test_the_gate_refuses_them_in_the_atelier_whatever_the_mode(world, tool):
    s = _session(world, simple_work=True)
    s.mode = "bypassPermissions"
    res = asyncio.new_event_loop().run_until_complete(ask(s, tool, {"query": "x"}))
    assert res.denied and res.message == refusals.ATELIER_NO_TOOL
    assert not [e for e in s.events if e.get("type") == "permission"]
    assert world["journal"][-1]["reason"] == "policy:simple_profile_tool_off"


@pytest.mark.parametrize("tool", ["WebSearch", "CronCreate", "EnterWorktree"])
def test_the_gate_leaves_them_to_the_cockpit(world, tool):
    s = _session(world, simple_work=False)
    s.mode = "bypassPermissions"
    res = asyncio.new_event_loop().run_until_complete(ask(s, tool, {"query": "x"}))
    assert not res.denied
    assert "policy:simple_profile_tool_off" not in [r.get("reason") for r in world["journal"]]


# ---- kernel engine: deny_tools ---------------------------------------------------------------
class _Never(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(3600)
        yield b""


class _Kernel:
    def __init__(self, unknown=None):
        self.sessions, self.unknown = [], unknown

    def handler(self, req):
        p = req.url.path
        if req.method == "DELETE":
            return httpx.Response(200, json={"closed": True})
        if p == "/v1/events":
            return httpx.Response(200, stream=_Never(), headers={"content-type": "text/event-stream"})
        if p == "/v1/local/sessions":
            self.sessions.append(json.loads(req.content))
            out = {"thread_id": "th_1"}
            if self.unknown is not None:
                out["unknown_denied"] = self.unknown
            return httpx.Response(201, json=out)
        if p.endswith("/mode"):
            m = json.loads(req.content)["mode"]
            return httpx.Response(200, json={"mode": m, "effective": m})
        if p == "/v1/local/setup":
            return httpx.Response(200, json={"models": {}})
        return httpx.Response(404, json={})


@pytest.fixture()
def mocked(monkeypatch):
    real = httpx.AsyncClient

    def make(**kw):
        k = _Kernel(**kw)

        def client(*a, **kwa):
            kwa["transport"] = httpx.MockTransport(k.handler)
            return real(*a, **kwa)
        monkeypatch.setattr(kernel.httpx, "AsyncClient", client)
        monkeypatch.setenv("SOKKAN_KERNEL_TOKEN", "t")
        monkeypatch.setenv("SOKKAN_KERNEL_URL", "http://kernel.test")
        monkeypatch.delenv("SOKKAN_KERNEL_DENY_TOOLS", raising=False)
        return k
    return make


def _open(spec):
    async def go():
        h = kernel.KernelHarness()
        stream = h.start(spec)
        evs = [await stream.__anext__(), await stream.__anext__()]
        while not h._mail.empty():
            evs.append(h._mail.get_nowait())
        await h._close_client()
        return h, evs
    return asyncio.run(go())


def test_kernel_an_atelier_work_opens_without_these_tools(world, mocked):
    k = mocked()
    _s, spec = _spec(world, simple_work=True)
    assert spec.shell is False
    h, _ = _open(spec)
    denied = set(k.sessions[0]["deny_tools"])
    assert KERNEL_OFF <= denied and set(refusals.ATELIER_OFF_KERNEL) <= denied
    assert {"terminal", "process_start", "memory_note"} <= denied       # 3.6.8 / 4.0 still hold
    assert not KERNEL_KEPT & denied
    assert h._atelier is True


def test_kernel_the_cockpit_keeps_them(world, mocked):
    k = mocked()
    _s, spec = _spec(world, simple_work=False)
    assert spec.shell is True
    h, _ = _open(spec)
    denied = set(k.sessions[0].get("deny_tools") or [])
    assert not KERNEL_OFF & denied and "terminal" not in denied
    assert h._atelier is False


def test_kernel_names_it_does_not_have_are_not_an_error(tmp_path, mocked):
    """A kernel without web search (or without delegation) answers them in `unknown_denied`:
    harmless, nothing is said (only the shell must match)."""
    mocked(unknown=list(refusals.ATELIER_OFF_KERNEL))
    h, evs = _open(contract.RunSpec(run_id="r1", cwd=str(tmp_path), shell=False))
    assert h.deny_unmatched == []
    assert not [e for e in evs if isinstance(e, contract.Error) and e.code == "deny_tools_unmatched"]


def test_kernel_a_removed_tool_called_anyway_reads_as_a_rule_of_the_space(tmp_path, mocked):
    import plain
    mocked()
    h, _ = _open(contract.RunSpec(run_id="r1", cwd=str(tmp_path), shell=False))
    h._turn, h._thread = "t1", "th_1"
    h._on_tool_result({"call_id": "c1", "name": "web_search", "is_error": False,
                       "reason": "deny_rule", "preview": "Refusé : règle deny web_search()."})
    res = h._mail.get_nowait()
    while not isinstance(res, contract.ToolResult):
        res = h._mail.get_nowait()
    assert res.ok is False and res.output.startswith(refusals.ATELIER_NO_TOOL)
    v = plain.View(docs_dir=None, deliv_dir=None, on_write=lambda *a: 1)
    v.feed({"type": "tool_use", "id": "c1", "tool": "web_search", "input": {"query": "x"}})
    (done,) = v.feed({"type": "tool_result", "tool_use_id": "c1", "text": res.output,
                      "is_error": True})
    assert done["reason"] == "policy"
