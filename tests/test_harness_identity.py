"""Opaque identity handles (backend/harness/identity.py): an engine that declares
`Capabilities.identity == "opaque"` never sees a project name, an e-mail address nor a
SOKKAN session id; the SOKKAN MCP servers of its run get their identity back."""
import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness import contract, identity  # noqa: E402


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


def test_a_handle_is_stable_meaningless_and_resolved_here_only(world, monkeypatch):
    a = identity.handle("project", "radio")
    assert a == identity.handle("project", "radio") and a.startswith("prj-") and len(a) == 24
    assert "radio" not in a
    assert identity.handle("user", "alice@x").startswith("usr-")
    assert identity.handle("project", "tv") != a and identity.handle("user", "radio") != a
    assert identity.resolve(a) == ("project", "radio")
    assert identity.resolve("prj-0000") is None and identity.handle("project", "") == ""
    key = world / "engine-identity.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600 and len(key.read_bytes()) == 32
    # another instance (another key) gives other handles for the same names
    other = world / "other"
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(other))
    assert identity.handle("project", "radio") != a


def test_the_spec_of_an_opaque_engine_carries_handles_only(world):
    who = {"SOKKAN_SESSION_ID": "sid-42", "SOKKAN_SESSION_USER": "alice@x",
           "SOKKAN_SESSION_PROJECT": "radio", "SOKKAN_SESSION_SCOPE": "radio:team"}
    spec = contract.RunSpec(
        run_id="sid-42", cwd="/w", project="radio", user="alice@x",
        mcp_servers={"sokkan-board": {"command": "py", "args": ["b.py"],
                                      "env": {"KEEP": "1", **who}}},
        confinement=contract.Confinement(project="radio"))
    o = identity.opaque_spec(spec)
    flat = repr(o.describe()) + repr(o.mcp_servers) + repr(o.confinement)
    for word in ("sid-42", "alice@x", "radio"):
        assert word not in flat, word
    assert o.run_id.startswith("run-") and o.user.startswith("usr-")
    assert o.project.startswith("prj-") and o.confinement.project == o.project
    env = o.mcp_servers["sokkan-board"]["env"]
    assert env == {"KEEP": "1", identity.HANDLE_ENV: o.run_id}
    assert spec.project == "radio"                          # the cockpit's spec is untouched
    # the server gets its identity back when it starts
    environ = dict(env)
    assert identity.expand_env(environ) is True
    assert {k: environ[k] for k in who} == who
    # an environment that already has an identity, or a handle unknown here: untouched
    assert identity.expand_env({"SOKKAN_SESSION_ID": "x", identity.HANDLE_ENV: o.run_id}) is False
    assert identity.expand_env({identity.HANDLE_ENV: "run-unknown"}) is False


def test_the_cockpit_hands_handles_to_an_opaque_engine_and_names_to_the_cli(world, monkeypatch):
    import agentchat
    import board
    from harness import legacy, registry
    from harness.conformance import driver, reference
    model = reference.ReferenceModel()
    model.script.append([driver.say("ok")])
    made = []

    def create(name=None):
        made.append(reference.ReferenceHarness(model))
        return made[-1]
    monkeypatch.setattr(registry, "create", create)
    board.add_sdk_session("sid-op", "t")
    s = agentchat.AgentSession("sid-op", cwd=str(world), user="alice@x")

    async def go():
        await s.handle_user("hello")
        await s.close()
    asyncio.new_event_loop().run_until_complete(go())
    spec = made[0]._spec
    assert spec.run_id.startswith("run-") and spec.user.startswith("usr-")
    assert identity.resolve(spec.run_id) == ("run", "sid-op")
    assert identity.resolve(spec.user) == ("user", "alice@x")
    for cfg in spec.mcp_servers.values():
        assert "SOKKAN_SESSION_ID" not in cfg["env"] and identity.HANDLE_ENV in cfg["env"]
    # the session itself still knows who it is
    assert [e for e in s.events if e["type"] == "result"]
    # the CLI is a local engine that declares plain identity: it keeps the names
    assert legacy.CAPABILITIES.identity == "plain"


# ---- the journal of a run's events, written by the cockpit (backend/run_journal.py) ------

def test_every_event_of_a_run_is_journalled_by_the_cockpit_shape_only(world):
    """The fake engine through the cockpit: a shell gate denied, an answer — the journal
    has run → turn → tool → gate → result → end in order, with names and decisions, and
    none of what was written (prompt, command, answer)."""
    import agentchat
    import board
    import run_journal
    from harness.conformance import driver, reference

    async def go():
        model = reference.ReferenceModel()
        model.script.append([driver.shell("rm secret-plan.txt"), driver.say("Left it alone.")])
        board.add_sdk_session("sid-j", "t")
        s = agentchat.AgentSession("sid-j", cwd=str(world), user="alice@x")
        s.attach(reference.ReferenceHarness(model),
                 contract.RunSpec(run_id="sid-j", cwd=str(world)))
        turn = asyncio.ensure_future(s.handle_user("tidy the private folder"))
        for _ in range(300):
            if s._perms:
                break
            await asyncio.sleep(0.01)
        (pid,) = s._perms
        # the gate is in the journal while it waits (written when it opened)
        assert [e["type"] for e in run_journal.events("sid-j")][-1] == "gate.requested"
        import hitl_journal
        hitl_journal.attribute(pid, "alice@x", "rail")
        s.resolve_permission(pid, {"decision": "deny", "message": "no"})
        await asyncio.wait_for(turn, 10)
        await s.close()
        return s

    asyncio.new_event_loop().run_until_complete(go())
    evs = run_journal.events("sid-j")
    types = [e["type"] for e in evs]
    for t in ("turn.started", "tool.called", "gate.requested", "gate.resolved", "turn.ended"):
        assert t in types, (t, types)
    assert types.index("gate.requested") < types.index("gate.resolved") < types.index("turn.ended")
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    assert {e["engine"] for e in evs} == {"reference"} and {e["project"] for e in evs} == {"default"}
    (res,) = [e for e in evs if e["type"] == "gate.resolved"]
    assert res["decision"] == "deny" and res["by"] == "alice@x"
    (call,) = [e for e in evs if e["type"] == "tool.called"]
    assert call["name"] == "shell" and call["classification"] == "shell"
    raw = (world / "run_events.db").read_bytes()
    for word in (b"secret-plan", b"private folder", b"Left it alone"):
        assert word not in raw, word
    assert not [e for e in evs if e["type"] in ("assistant.text", "usage")]
