"""The session contract (backend/harness): events, gate, registry — and the adapter of the
engine SOKKAN drives today, played with a scripted SDK client (no CLI, no model).

What is proved here is the seam itself: every SDK message becomes contract events, a
permission request of the SDK becomes a gate the cockpit decides, the recall of a turn is
delivered by the adapter and announced, and nothing of the SDK is left in the cockpit.
"""
import ast
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from harness import contract, registry  # noqa: E402
from harness.contract import Decision, Gate  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- the contract ------------------------------------------------------------------------

def test_every_event_round_trips_through_json():
    gate = Gate(gate_id="g1", kind="shell", subject={"tool": "Bash", "input": {"command": "ls"},
                                                     "command": "ls"},
                options=("allow_once", "edit", "deny"), turn_id="t1")
    events = [
        contract.RunStarted(run_id="r", harness="legacy", sid="r", cwd="/w", model="m",
                            project="default", resumed_from="s0"),
        contract.RunInfo(engine_session="s1", model="m2"),
        contract.TurnStarted(turn_id="t1", prompt_ref=contract.prompt_ref("hello")),
        contract.AssistantText(turn_id="t1", text="hi"),
        contract.AssistantThink(turn_id="t1", text="hm", delta=True),
        contract.ToolCalled(turn_id="t1", call_id="c1", name="Bash", input={"command": "ls"},
                            classification="shell"),
        contract.ToolResult(call_id="c1", output="a\nb", ok=True, duration_ms=12, turn_id="t1"),
        contract.GateRequested(gate=gate),
        contract.GateResolved(gate_id="g1", decision="deny", by="alice@x", via="rail",
                              message="no"),
        contract.ContextInjected(turn_id="t1", source="hook", note_names=("a", "b"), chars=40),
        contract.Usage(turn_id="t1", model="m", input=10, output=5, cache_read=3,
                       cache_write=8, cache_write_long=2, message_id="msg_1", basis="api"),
        contract.TurnEnded(turn_id="t1", stop_reason="success", text="done", num_turns=1,
                           cost_usd=0.01),
        contract.RunEnded(status="ended", resumable=True, engine_session="s1"),
        contract.Error(code="confined", message="outside", recoverable=True,
                       data={"tool": "Write"}),
    ]
    assert {e.type for e in events} == set(contract.EVENT_TYPES)      # none forgotten
    for e in events:
        wire = json.loads(json.dumps(e.to_dict()))
        assert wire["type"] == e.type
        assert contract.event_from_dict(wire) == e
    usage = events[10].to_dict()
    assert (usage["in"], usage["out"]) == (10, 5) and "input" not in usage  # wire names of § 3.1
    flat = events[7].to_dict()
    assert flat["gate_id"] == "g1" and flat["kind"] == "shell" and flat["options"] == [
        "allow_once", "edit", "deny"]
    with pytest.raises(contract.ContractError):
        contract.event_from_dict({"type": "nope"})
    with pytest.raises(contract.ContractError):
        contract.event_from_dict({"type": "turn.started"})            # turn_id missing


def test_gate_and_decision_refuse_what_the_contract_does_not_know():
    with pytest.raises(contract.ContractError):
        Gate(gate_id="g", kind="teleport")
    with pytest.raises(contract.ContractError):
        Gate(gate_id="g", kind="tool", options=("maybe",))
    with pytest.raises(contract.ContractError):
        Decision("maybe")
    d = Decision.deny("no", by="bob@x", via="teams")
    assert d.denied and not d.allowed and Decision.from_dict(d.to_dict()) == d
    assert Decision.allow().kind == "allow_once" and Decision.allow(session=True).kind == \
        "allow_session"
    assert Decision.answer({"q": "a"}).allowed and Decision.edit({"command": "ls"}).allowed
    caps = contract.Capabilities(gate_kinds=("shell",), question=True, context=("hook",))
    assert contract.Capabilities.from_dict(json.loads(json.dumps(caps.to_dict()))) == caps


def test_a_run_spec_describes_itself_without_secret_nor_callable():
    async def provider(req):
        return None
    spec = contract.RunSpec(run_id="r1", cwd="/w", env={"STRIPE_KEY": "sk_live_x"},
                            secret_names=("STRIPE_KEY",), context=provider,
                            mcp_servers={"sokkan-memory": {"command": "python"}})
    d = json.loads(json.dumps(spec.describe()))
    assert d["env"] == ["STRIPE_KEY"] and "sk_live_x" not in json.dumps(d)
    assert d["context"] is True and d["mcp_servers"] == ["sokkan-memory"]


# ---- the registry ------------------------------------------------------------------------

def test_one_engine_is_declared_and_selected_by_configuration(monkeypatch, capsys):
    assert registry.declared() == ["legacy"] and registry.default() == "legacy"
    monkeypatch.delenv(registry.ENV, raising=False)
    assert registry.selected() == "legacy"
    monkeypatch.setenv(registry.ENV, "Legacy ")
    assert registry.selected() == "legacy"
    monkeypatch.setenv(registry.ENV, "warp-drive")          # a typo never leaves no engine
    assert registry.selected() == "legacy"
    assert "warp-drive" in capsys.readouterr().err
    h = registry.create()
    assert isinstance(h, registry.Harness) and h.name == "legacy"
    caps = registry.capabilities()
    assert caps == h.capabilities() and caps.gate_authority == "core"
    assert caps.question and caps.plan and caps.resume and caps.context == ("hook", "mcp")
    (d,) = registry.describe()
    assert d["name"] == "legacy" and d["selected"] and isinstance(d["available"], bool)
    assert d["available"] is False or d["capabilities"]["usage"] == "transcripts"


def test_an_engine_whose_dependency_is_missing_is_reported_not_raised(monkeypatch):
    ghost = registry.Engine(name="ghost", label="Ghost", module="harness.no_such_module",
                            factory="X")
    ok, note = ghost.detect()
    assert ok is False and "ModuleNotFoundError" in note


# ---- no SDK outside the adapter ----------------------------------------------------------

def test_the_sdk_is_imported_by_the_adapter_only():
    """The point of the lot: nothing of the engine's SDK outside backend/harness/legacy.py."""
    allowed = {"harness/legacy.py"}
    found = set()
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            if any(m.split(".")[0] == "claude_agent_sdk" for m in mods):
                found.add(rel)
    assert found <= allowed, f"the SDK is imported outside the adapter: {found - allowed}"
    assert "harness/legacy.py" in found
    import agentchat
    assert not [n for n in vars(agentchat) if "SDK" in n or n.startswith("PermissionResult")]


# ---- the adapter, with a scripted SDK client ---------------------------------------------

def _sdk():
    import claude_agent_sdk as sdk
    return sdk


class _Client:
    """An SDK-shaped client: plays `script(client)` (an async generator of SDK messages)."""

    def __init__(self, script):
        self.script, self.asked, self.interrupted, self.modes = script, [], 0, []

    async def query(self, text):
        self.asked.append(text)

    async def receive_response(self):
        async for m in self.script(self):
            yield m

    async def interrupt(self):
        self.interrupted += 1

    async def set_permission_mode(self, mode):
        self.modes.append(mode)


def _result(sdk, **kw):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id="cs-1", total_cost_usd=0.02,
                usage={"input_tokens": 100, "output_tokens": 20,
                       "cache_read_input_tokens": 7, "cache_creation_input_tokens": 9},
                result="done")
    return sdk.ResultMessage(**{**base, **kw})


async def _turn(h, stream, prompt, on_gate=None):
    turn = await h.send(prompt)
    out = []
    async for ev in stream:
        out.append(ev)
        if isinstance(ev, contract.GateRequested) and on_gate is not None:
            await h.decide(ev.gate.gate_id, on_gate(ev.gate))
        if isinstance(ev, contract.TurnEnded) and ev.turn_id == turn:
            return out


def test_sdk_messages_become_contract_events():
    from harness import legacy
    sdk = _sdk()

    async def script(client):
        yield sdk.SystemMessage(subtype="init", data={"session_id": "cs-1", "model": "m-1"})
        yield sdk.AssistantMessage(
            content=[sdk.ThinkingBlock(thinking="hm", signature=""), sdk.TextBlock(text="Hi"),
                     sdk.ToolUseBlock(id="tu1", name="Bash", input={"command": "ls"})],
            model="m-1", usage={"input_tokens": 50, "output_tokens": 4,
                                "cache_creation_input_tokens": 30,
                                "cache_creation": {"ephemeral_1h_input_tokens": 10,
                                                   "ephemeral_5m_input_tokens": 20}},
            message_id="msg_1")
        yield sdk.UserMessage(content=[sdk.ToolResultBlock(
            tool_use_id="tu1", content=[{"type": "text", "text": "a.txt"}], is_error=False)])
        yield _result(sdk)

    async def go():
        h = legacy.LegacyHarness(client=_Client(script))
        stream = h.start(None)
        first = await _turn(h, stream, "list")
        second = await _turn(h, stream, "again")
        return first, second

    first, second = _run(go())
    types = [e.type for e in first]
    assert types == ["run.started", "turn.started", "run.info", "run.info", "usage",
                     "assistant.think", "assistant.text", "tool.called", "tool.result",
                     "usage", "usage", "turn.ended"], types
    turn = first[1].turn_id
    assert first[1].prompt_ref == contract.prompt_ref("list") and "list" not in first[1].prompt_ref
    assert (first[2].engine_session, first[2].model) == ("cs-1", "m-1")
    msg = first[4]
    assert (msg.scope, msg.message_id, msg.model, msg.input, msg.output) == (
        "message", "msg_1", "m-1", 50, 4)
    assert (msg.cache_write, msg.cache_write_long) == (30, 10)
    assert first[7].classification == "shell" and first[7].call_id == "tu1"
    assert first[8].output == "a.txt" and first[8].ok and first[8].call_id == "tu1"
    # the call was reported when it started (4 output tokens); the result of the turn says
    # what it really was: the call is revised, same id, so that the calls add up to the turn
    settled = first[9]
    assert settled.revision and (settled.scope, settled.message_id) == ("message", "msg_1")
    assert (settled.input, settled.output, settled.cache_read) == (100, 20, 7)
    assert (settled.cache_write, settled.cache_write_long) == (30, 10)   # never lowered
    assert not msg.revision
    total = first[10]
    assert total.scope == "turn" and (total.input, total.cache_read, total.cache_write) == (
        100, 7, 9)
    end = first[11]
    assert (end.turn_id, end.stop_reason, end.text, end.cost_usd, end.reported) == (
        turn, "success", "done", 0.02, True)
    assert {getattr(e, "turn_id", turn) for e in first[1:]} == {turn}
    # second turn of the same CLI process: its cost counter is cumulative → the delta is 0;
    # the engine session did not change → it is not announced again
    assert [e.type for e in second].count("run.info") == 2
    assert all(e.engine_session is None for e in second if e.type == "run.info")
    assert second[-1].cost_usd == pytest.approx(0.0)


def test_a_failed_call_is_an_error_event_and_the_run_stays_usable():
    from harness import legacy
    sdk = _sdk()
    calls = []

    async def script(client):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the CLI died")
        yield _result(sdk)

    async def go():
        h = legacy.LegacyHarness(client=_Client(script))
        stream = h.start(None)
        return await _turn(h, stream, "one"), await _turn(h, stream, "two")

    first, second = _run(go())
    err = [e for e in first if e.type == "error"][0]
    assert (err.code, err.message, err.recoverable) == ("RuntimeError", "the CLI died", False)
    assert first[-1].reported is False and first[-1].is_error
    assert second[-1].reported and second[-1].text == "done"


def test_a_permission_request_of_the_sdk_is_a_gate_decided_through_the_contract():
    from harness import legacy
    sdk = _sdk()
    seen = {}

    def script_for(h):
        async def script(client):
            yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(
                id="tu1", name="Bash", input={"command": "rm -rf build"})], model="m")
            seen["bash"] = await h._can_use_tool("Bash", {"command": "rm -rf build"}, None)
            seen["edit"] = await h._can_use_tool("Edit", {"file_path": "/w/a.py"}, None)
            seen["ask"] = await h._can_use_tool(
                "AskUserQuestion", {"questions": [{"question": "Which env?"}]}, None)
            seen["plain"] = await h._can_use_tool("WebFetch", {"url": "https://x"}, None)
            yield _result(sdk)
        return script

    def on_gate(gate):
        if gate.kind == "shell":
            return Decision.deny("not this one", by="alice@x", via="rail")
        if gate.kind == "file_write":
            return Decision.edit({"file_path": "/w/b.py"}, by="bob@x", via="teams")
        if gate.kind == "question":
            return Decision.answer({"Which env?": "stg"}, by="alice@x", via="rail")
        return Decision.allow(by="mode:bypassPermissions", via="auto_rule")

    async def go():
        client = _Client(None)
        h = legacy.LegacyHarness(client=client)
        client.script = script_for(h)
        return await _turn(h, h.start(None), "go", on_gate), h

    events, h = _run(go())
    gates = [e.gate for e in events if e.type == "gate.requested"]
    assert [(g.kind, g.tool) for g in gates] == [
        ("shell", "Bash"), ("file_write", "Edit"), ("question", "AskUserQuestion"),
        ("net", "WebFetch")]
    assert gates[0].subject["command"] == "rm -rf build" and gates[1].path == "/w/a.py"
    assert gates[0].options == ("allow_once", "edit", "deny") and gates[2].options == (
        "answer", "deny")
    assert len({g.gate_id for g in gates}) == 4 and all(g.turn_id for g in gates)
    resolved = [e for e in events if e.type == "gate.resolved"]
    assert [(r.gate_id, r.decision, r.by, r.via) for r in resolved] == [
        (gates[0].gate_id, "deny", "alice@x", "rail"),
        (gates[1].gate_id, "edit", "bob@x", "teams"),
        (gates[2].gate_id, "answer", "alice@x", "rail"),
        (gates[3].gate_id, "allow_once", "mode:bypassPermissions", "auto_rule")]
    assert resolved[0].message == "not this one"
    # what the SDK receives back: its own types, built by the adapter and nowhere else
    assert type(seen["bash"]).__name__ == "PermissionResultDeny" and \
        seen["bash"].message == "not this one"
    assert seen["edit"].updated_input == {"file_path": "/w/b.py"}
    assert seen["ask"].updated_input == {"questions": [{"question": "Which env?"}],
                                         "answers": {"Which env?": "stg"}}
    assert seen["plain"].updated_input == {"url": "https://x"}
    assert not h._gates                                           # nothing left waiting
    assert _run(h.decide(gates[0].gate_id, Decision.allow())) is False    # written once


def test_stop_denies_what_still_waits_and_ends_the_stream():
    from harness import legacy

    async def go():
        async def script(client):
            if False:
                yield None
        h = legacy.LegacyHarness(client=_Client(script))
        stream = h.start(None)
        task = asyncio.ensure_future(h._can_use_tool("Bash", {"command": "ls"}, None))
        await asyncio.sleep(0)
        await h.stop()
        await h.stop()                                            # idempotent
        res = await task
        rest = [ev async for ev in stream]
        return res, rest

    res, rest = _run(go())
    assert type(res).__name__ == "PermissionResultDeny" and res.message == "Request cancelled"
    # the stream finishes on run.ended: what the engine says after it is not delivered
    assert [e.type for e in rest] == ["run.started", "gate.requested", "run.ended"]
    ended = rest[-1]
    assert ended.resumable is False                  # no engine session was ever announced


def test_the_recall_is_delivered_by_hooks_and_announced():
    from harness import legacy
    asked, said = [], []

    async def provider(req):
        asked.append(req)
        if "nothing" in req.text:
            return None
        return contract.ContextBlock(text="[memory-recall] tide tables are in note tides",
                                     note_names=("tides",))

    hooks = legacy.recall_hooks(provider, said.append, lambda: "t9")
    assert set(hooks) == {"UserPromptSubmit", "PreToolUse"}
    assert hooks["PreToolUse"][0].matcher == "Task|Agent"
    on_prompt = hooks["UserPromptSubmit"][0].hooks[0]
    on_sub = hooks["PreToolUse"][0].hooks[0]

    out = _run(on_prompt({"hook_event_name": "UserPromptSubmit", "prompt": "when is high tide?",
                          "agent_id": None}, None, None))
    assert out == {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "[memory-recall] tide tables are in note tides"},
        "suppressOutput": True}
    assert (asked[0].channel, asked[0].text, asked[0].turn_id) == (
        "prompt", "when is high tide?", "t9")
    assert said == [contract.ContextInjected(turn_id="t9", source="hook", note_names=("tides",),
                                             chars=len(out["hookSpecificOutput"][
                                                 "additionalContext"]), channel="prompt")]
    assert _run(on_prompt({"hook_event_name": "UserPromptSubmit", "prompt": "nothing here"},
                          None, None)) == {} and len(said) == 1

    out = _run(on_sub({"hook_event_name": "PreToolUse", "tool_name": "Agent",
                       "tool_use_id": "tu7",
                       "tool_input": {"description": "tides", "prompt": "look up the tides"}},
                      None, None))
    assert out["hookSpecificOutput"]["updatedInput"]["prompt"] == \
        "look up the tides\n[memory-recall] tide tables are in note tides"
    assert (asked[-1].channel, asked[-1].title, asked[-1].agent_id) == ("subagent", "tides", "tu7")
    assert said[-1].channel == "subagent"
    assert _run(on_sub({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                        "tool_input": {"prompt": "x"}}, None, None)) == {}

    async def broken(req):
        raise RuntimeError("store down")
    hooks = legacy.recall_hooks(broken)
    assert _run(hooks["UserPromptSubmit"][0].hooks[0](
        {"hook_event_name": "UserPromptSubmit", "prompt": "anything"}, None, None)) == {}
    assert legacy.recall_hooks(None) == {}


def test_a_run_spec_becomes_the_options_the_cli_was_started_with_before():
    from harness import legacy

    async def provider(req):
        return None
    spec = contract.RunSpec(
        run_id="sid1", cwd="/w", project="default", user="alice@x", model="m-1",
        resume="cs-0", plan=True, env={"K": "v"}, mcp_servers={"s": {"command": "c"}},
        auto_allow=("Read", "Glob"), deny_tools=("Bash",), budget_usd=1.5, context=provider,
        options={"legacy": {"setting_sources": ["project", "local"]}})
    h = legacy.LegacyHarness()
    kw = h._options(spec)
    assert kw["cwd"] == "/w" and kw["allowed_tools"] == ["Read", "Glob"]
    assert kw["permission_mode"] == "plan" and kw["setting_sources"] == ["project", "local"]
    assert kw["disallowed_tools"] == ["Bash"] and kw["model"] == "m-1" and kw["resume"] == "cs-0"
    assert kw["env"]["K"] == "v" and kw["env"].get("PATH") == os.environ.get("PATH")
    assert kw["can_use_tool"] == h._can_use_tool and kw["mcp_servers"] == {"s": {"command": "c"}}
    assert set(kw["hooks"]) == {"UserPromptSubmit", "PreToolUse"}
    if "max_budget_usd" in legacy._OPTION_FIELDS:
        assert kw["max_budget_usd"] == 1.5
    bare = legacy.LegacyHarness()._options(contract.RunSpec(run_id="s", cwd="/w"))
    assert bare["permission_mode"] == "default"      # never a bypass mode in the engine
    assert not ({"hooks", "env", "model", "resume", "disallowed_tools", "max_budget_usd"}
                & set(bare))


# ---- the cockpit consumes the contract ---------------------------------------------------

@pytest.fixture()
def world(tmp_path, monkeypatch):
    import agentchat
    import audit
    import board
    import usage
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(usage, "DB", tmp_path / "usage.db")
    monkeypatch.setattr(agentchat, "_registry", {})
    return tmp_path


def test_a_session_turn_goes_adapter_contract_cockpit_and_back(world):
    """The whole loop on a scripted client: SDK permission request → gate → the person's
    click → Decision → the SDK's answer; and the front receives the flow it always did."""
    import agentchat
    from harness import legacy
    sdk = _sdk()
    seen = {}

    async def go():
        client = _Client(None)
        h = legacy.LegacyHarness(client=client)

        async def script(c):
            yield sdk.SystemMessage(subtype="init", data={"session_id": "cs-9", "model": "m-1"})
            yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(
                id="tu1", name="Bash", input={"command": "make deploy"})], model="m-1",
                usage={"input_tokens": 10, "output_tokens": 2}, message_id="msg_1")
            seen["sdk"] = await h._can_use_tool("Bash", {"command": "make deploy"}, None)
            yield sdk.UserMessage(content=[sdk.ToolResultBlock(
                tool_use_id="tu1", content="deployed", is_error=False)])
            yield _result(sdk, total_cost_usd=0.5)
        client.script = script
        board.add_sdk_session("sid-loop", "t")
        s = agentchat.AgentSession("sid-loop", cwd="/tmp", user="alice@x")
        s.attach(h)
        turn = asyncio.ensure_future(s.handle_user("ship it"))
        for _ in range(200):
            if s._perms:
                break
            await asyncio.sleep(0.01)
        (pid,) = s._perms
        s.resolve_permission(pid, {"decision": "allow",
                                   "updated_input": {"command": "make deploy --dry-run"}})
        await asyncio.wait_for(turn, 10)
        return s, pid

    import board
    s, pid = _run(go())
    assert seen["sdk"].updated_input == {"command": "make deploy --dry-run"}
    flow = [e["type"] for e in s.events]
    assert flow == ["status", "session", "model", "tool_use", "permission",
                    "permission_resolved", "tool_result", "result", "status"], flow
    perm = s.events[4]
    assert perm["id"] == pid and perm["tool"] == "Bash" and perm["title"] == "make deploy"
    assert s.events[6] == {"type": "tool_result", "tool_use_id": "tu1", "text": "deployed",
                           "is_error": False, "truncated": False}
    assert s.events[7]["cost_usd"] == 0.5 and s.cost_usd == 0.5
    assert (s.tokens_in, s.tokens_out) == (100 + 7 + 9, 20)
    assert s.claude_session_id == "cs-9" and board.get_claude_session_id("sid-loop") == "cs-9"
    assert not s._perms and not s._gate_tasks


def test_a_gate_settled_by_the_engine_closes_the_card(world):
    """An engine whose gates are born on its side can settle one by itself (its delay ran
    out, another client decided): the card of the cockpit closes, the journal says nobody
    here decided."""
    import agentchat
    import board
    import hitl_journal
    from harness.conformance import driver, reference

    async def go():
        model = reference.ReferenceModel()
        model.script.append([driver.shell("touch made.txt")])
        h = reference.ReferenceHarness(model)
        board.add_sdk_session("sid-exp", "t")
        s = agentchat.AgentSession("sid-exp", cwd=str(world), user="alice@x")
        s.attach(h, contract.RunSpec(run_id="sid-exp", cwd=str(world)))
        turn = asyncio.ensure_future(s.handle_user("make it"))
        for _ in range(200):
            if s._perms:
                break
            await asyncio.sleep(0.01)
        (pid,) = s._perms
        assert h.expire(pid)
        await asyncio.wait_for(turn, 10)
        return s, pid

    s, pid = _run(go())
    assert not (world / "made.txt").exists()
    kinds = [e["type"] for e in s.events]
    assert kinds.count("permission") == 1 and kinds.count("permission_resolved") == 1
    assert not s._perms
    (row,) = hitl_journal.query(session_id="sid-exp")
    assert row["decision"] == "cancelled" and row["actor"] == ""


def test_the_cockpit_can_only_restrict_an_engine_that_owns_its_gates(world):
    """Reference engine, gates born on the engine's side: what its policy refuses without
    asking never becomes a card; what the cockpit allows it can still refuse — and the
    cockpit's own rules (here: bypass mode) never run what the engine refused."""
    import agentchat
    import board
    from harness.conformance import driver, reference

    async def go():
        model = reference.ReferenceModel()
        model.script.append([driver.shell("sudo touch root.txt"),      # engine: never
                             driver.shell("touch revoked.txt"),          # engine: revoked
                             driver.shell("touch fine.txt")])
        h = reference.ReferenceHarness(model, revoke=("revoked",))
        board.add_sdk_session("sid-auth", "t")
        s = agentchat.AgentSession("sid-auth", cwd=str(world), user="alice@x")
        s.mode = "bypassPermissions"            # the cockpit's most permissive rule
        s.attach(h, contract.RunSpec(run_id="sid-auth", cwd=str(world)))
        seen = []
        consume = s._consume
        s._consume = lambda ev: (seen.append(ev), consume(ev))[1]
        await asyncio.wait_for(s.handle_user("go"), 20)
        await asyncio.sleep(0.05)
        return s, seen

    s, seen = _run(go())
    assert (world / "fine.txt").exists()
    assert not (world / "root.txt").exists() and not (world / "revoked.txt").exists()
    gates = [e.gate for e in seen if e.type == "gate.requested"]
    assert [g.subject["command"] for g in gates] == ["touch revoked.txt", "touch fine.txt"]
    resolved = {e.gate_id: e for e in seen if e.type == "gate.resolved"}
    assert resolved[gates[0].gate_id].decision == "deny" and \
        resolved[gates[0].gate_id].via == "engine"
    assert resolved[gates[1].gate_id].decision == "allow_once"
    results = [e for e in s.events if e["type"] == "tool_result"]
    assert [r["is_error"] for r in results] == [True, True, False]
    assert not [e for e in s.events if e["type"] == "permission"]   # bypass: nobody was asked


def test_an_edit_is_not_sent_to_an_engine_that_does_not_take_one(world):
    import agentchat
    import board
    from harness.conformance import driver, reference

    async def go():
        model = reference.ReferenceModel()
        model.script.append([driver.shell("touch a.txt")])
        h = reference.ReferenceHarness(model)
        board.add_sdk_session("sid-edit", "t")
        s = agentchat.AgentSession("sid-edit", cwd=str(world), user="alice@x")
        s.attach(h, contract.RunSpec(run_id="sid-edit", cwd=str(world)))
        turn = asyncio.ensure_future(s.handle_user("go"))
        for _ in range(200):
            if s._perms:
                break
            await asyncio.sleep(0.01)
        (pid,) = s._perms
        s.resolve_permission(pid, {"decision": "allow",
                                   "updated_input": {"command": "touch b.txt"}})
        await asyncio.wait_for(turn, 10)
        return s

    s = _run(go())
    assert not (world / "a.txt").exists() and not (world / "b.txt").exists()
    (res,) = [e for e in s.events if e["type"] == "tool_result"]
    assert res["is_error"] and "does not take the decision 'edit'" in res["text"]


def test_a_mode_the_engine_does_not_have_is_not_engaged(world):
    """Cockpit side of C7: no plan mode in the engine → the session stays in its mode and
    says so; with an engine that has one, the mode is engaged in the engine."""
    import agentchat
    import board
    from harness.conformance import reference

    async def go(plan):
        h = reference.ReferenceHarness(plan=plan)
        sid = f"sid-plan-{int(plan)}"
        board.add_sdk_session(sid, "t")
        s = agentchat.AgentSession(sid, cwd=str(world), user="alice@x")
        s.attach(h, contract.RunSpec(run_id=sid, cwd=str(world)))
        await s.set_mode("plan", actor="alice@x")
        return s, h

    s, h = _run(go(False))
    assert s.mode == "default" and h.mode == "default"
    assert [e["type"] for e in s.events] == ["error", "perm_mode"]
    assert "no plan mode" in s.events[0]["message"] and s.events[1]["mode"] == "default"
    s, h = _run(go(True))
    assert s.mode == "plan" and h.mode == "plan"
    assert s.events == [{"type": "perm_mode", "mode": "plan"}]


def test_the_scope_of_the_recall_is_the_cockpits(world, monkeypatch):
    """Cockpit side of C8: the provider handed to the engine is built with the session's
    project and the clearance of the person — the engine has no say in it."""
    import agentchat
    import board
    import classification
    import memrecall
    got = {}

    def provider(session_id, projects=None):
        got["sid"], got["projects"] = session_id, projects
        return None
    monkeypatch.setattr(memrecall, "provider", provider)
    board.add_sdk_session("sid-scope", "t")
    s = agentchat.AgentSession("sid-scope", cwd=str(world), user="alice@x")
    spec = _run(s._run_spec())
    assert got["sid"] == "sid-scope"
    assert tuple(got["projects"]) == tuple(classification.scope_for_email("alice@x", "default"))
    assert spec.context is None and spec.run_id == "sid-scope" and spec.kind == "session"
    assert spec.auto_allow == tuple(agentchat.SAFE_TOOLS) and spec.deny_tools is None
    assert spec.plan is False and spec.options == {"legacy": {"setting_sources": [
        "user", "project", "local"]}}
    assert set(spec.mcp_servers) == set(agentchat.MCP_SERVERS)
