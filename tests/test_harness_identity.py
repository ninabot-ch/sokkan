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


# ---- 4.0 lot 0c: the working folder and the tools' environment name nobody ----------------

def test_the_working_folder_of_a_project_is_handed_without_its_slug(world):
    work = world / "projects" / "radio" / "work"
    work.mkdir(parents=True)
    alias = identity.opaque_cwd(str(work))
    assert "radio" not in alias and alias.endswith("/work")
    assert Path(alias).resolve() == work.resolve()          # the same files
    (Path(alias) / "minutes.md").write_text("x")
    assert (work / "minutes.md").read_text() == "x"
    assert identity.opaque_cwd(str(work)) == alias            # stable
    # the instance's own workspace (default project) has no project name in it: unchanged
    ws = world / "workspace"
    ws.mkdir()
    assert identity.opaque_cwd(str(ws)) == str(ws)
    # a link that points elsewhere (tampered) is put back
    link = Path(alias).parent
    link.unlink()
    os.symlink(world / "workspace", link)
    assert Path(identity.opaque_cwd(str(work))).resolve() == work.resolve()


def test_the_tools_environment_of_an_opaque_engine_names_nobody(world, monkeypatch):
    import base64
    from forge import gitcred
    monkeypatch.setattr(gitcred.links, "mac_key", lambda: b"k" * 32)
    monkeypatch.setattr(gitcred.links, "mac_keys", lambda: [b"k" * 32])
    sid = "5f0c2e9a7b1d4c3e8f6a2b0c9d8e7f6a"
    work = world / "projects" / "radio" / "work"
    work.mkdir(parents=True)
    env = {
        "ANTHROPIC_CUSTOM_HEADERS": "x-sokkan-user: alice@x\nx-sokkan-tier: claude\n"
                                    "x-sokkan-project: radio",
        "SOKKAN_FORGE_TICKET": gitcred.ticket(sid, "alice@x"),
        "STRIPE_KEY": "sk_live_value",
        "AWS_PROFILE": "default",
        "SOME_TOOL_DIR": "/srv/radio/cache",                   # nobody rewrote this one
        "NOTE": "radiohead playlist",                          # « radio » inside a word: kept
        "BLOB": base64.b64encode(b"alice@x").decode(),         # encoded: still a name
    }
    spec = contract.RunSpec(run_id=sid, cwd=str(work), project="radio", user="alice@x",
                            env=env, secret_names=("STRIPE_KEY", "BLOB"))
    import agentchat
    o = identity.opaque_spec(spec, rewrite=agentchat._opaque_env_rewrite)
    flat = repr(o.env) + o.cwd
    for word in ("alice@x", sid, "/radio", ": radio", base64.b64encode(b"alice@x").decode()):
        assert word not in flat, (word, o.env)
    assert identity.leaks(o.env, [sid, "alice@x"], ["radio"]) == []
    assert "SOME_TOOL_DIR" not in o.env and "BLOB" not in o.env
    assert o.secret_names == ("STRIPE_KEY",)
    assert o.env["STRIPE_KEY"] == "sk_live_value" and o.env["AWS_PROFILE"] == "default"
    assert o.env["NOTE"] == "radiohead playlist"
    hdr = dict(x.split(": ", 1) for x in o.env["ANTHROPIC_CUSTOM_HEADERS"].split("\n"))
    assert identity.resolve(hdr["x-sokkan-user"]) == ("user", "alice@x")
    assert identity.resolve(hdr["x-sokkan-project"]) == ("project", "radio")
    assert hdr["x-sokkan-tier"] == "claude"
    # the forge ticket was re-minted over the handles; the credential helper gets the
    # session and the person back on the api
    assert o.env["SOKKAN_FORGE_TICKET"] != env["SOKKAN_FORGE_TICKET"]
    assert gitcred.verify(o.env["SOKKAN_FORGE_TICKET"]) == (sid, "alice@x")
    assert gitcred.verify(env["SOKKAN_FORGE_TICKET"]) == (sid, "alice@x")   # the CLI's: unchanged
    # a forged handle ticket (unknown handles) is refused
    assert gitcred.verify(gitcred.ticket("run-" + "0" * 20, "usr-" + "0" * 20)) is None
    # a real address that happens to start like a handle is still an address
    assert gitcred.verify(gitcred.ticket(sid, "usr-bob@x")) == (sid, "usr-bob@x")


def test_the_claude_routing_resolves_the_projects_handle_on_the_api(world, monkeypatch):
    import claudetier
    seen = []
    monkeypatch.setattr(claudetier, "router_secret", lambda: "s3")
    monkeypatch.setattr(claudetier, "gate", lambda project, model: seen.append(project) or {
        "allowed": False, "code": "x", "reason": ""})
    h = identity.handle("project", "radio")
    claudetier.grant(h, "claude-x", "s3", False)
    claudetier.grant("prj-unknown", "claude-x", "s3", False)
    claudetier.grant("radio", "claude-x", "s3", False)
    assert seen == ["radio", "prj-unknown", "radio"]


def test_a_project_session_on_an_opaque_engine_is_handed_no_name_end_to_end(world, monkeypatch):
    """Through the cockpit: a session of project « radio » with « Write with Claude »
    routing and a managed inference header — the engine's spec, folder and environment
    carry handles only, and what it writes lands in the project's folder."""
    import agentchat
    import board
    import claudetier
    import llm
    import projects
    from harness import registry
    from harness.conformance import driver, reference
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    projects.create("radio", "Radio", created_by="admin@x")
    monkeypatch.setattr(llm, "session_env", lambda user="": {
        "ANTHROPIC_CUSTOM_HEADERS": f"x-sokkan-user: {user}"})
    monkeypatch.setattr(claudetier, "session_env", lambda sid, proj, env: {
        "ANTHROPIC_CUSTOM_HEADERS": env.get("ANTHROPIC_CUSTOM_HEADERS", "")
        + f"\nx-sokkan-project: {proj}"})
    model = reference.ReferenceModel()
    model.script.append([driver.write("notes.md", "# notes\n"), driver.say("ok")])
    made = []

    def create(name=None):
        made.append(reference.ReferenceHarness(model))
        return made[-1]
    monkeypatch.setattr(registry, "create", create)
    sid = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    board.add_sdk_session(sid, "t", project="radio", owner="alice@x")
    s = agentchat.AgentSession(sid, cwd=agentchat.project_cwd(sid), user="alice@x")

    async def go():
        s.mode = "bypassPermissions"
        await s.handle_user("take notes")
        await s.close()
    asyncio.new_event_loop().run_until_complete(go())
    spec = made[0]._spec
    flat = repr(spec.describe()) + repr(spec.env) + spec.cwd + repr(spec.mcp_servers)
    for word in ("radio", "alice@x", sid):
        assert word not in flat, (word, flat)
    assert (world / "projects" / "radio" / "work" / "notes.md").exists()
