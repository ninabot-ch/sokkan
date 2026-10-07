"""3.2 lot 8 — the project sandbox.

A session of project `radio` reaches only $DATA/projects/radio (+ `shared` read-only):

* file tools (Read, Write, Edit, Glob, Grep…) outside the space are refused by the
  PreToolUse hook, paths normalised and symlinks resolved;
* Bash is refused when no OS sandbox is available (hooks-only), and runs inside bubblewrap
  when it is (only the workspace writable, nothing of another project mounted, empty env);
* `default` keeps its 3.1 behaviour (no hook, no wrapper), and so does every project when
  the feature is off.

The bubblewrap tests run when `bwrap` works on the host (or SOKKAN_TEST_BWRAP=<path>).
"""
import asyncio
import os
import shutil
import subprocess

import pytest

BW = os.environ.get("SOKKAN_TEST_BWRAP") or shutil.which("bwrap") or ""


def _bwrap_works() -> bool:
    if not BW:
        return False
    import sandbox
    return sandbox._probe(BW) is None


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap not usable here "
                                 "(install bubblewrap or set SOKKAN_TEST_BWRAP)")


@pytest.fixture()
def world(tmp_path, monkeypatch):
    import audit
    import board
    import projects
    import sandbox

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_SANDBOX", "1")
    monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", "/nonexistent/bwrap")   # hooks-only unless a test says
    monkeypatch.delenv("SOKKAN_SANDBOX_NETWORK", raising=False)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    import auth
    import iam
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    iam.upsert_user("admin@x", "admin")
    monkeypatch.setattr(auth, "resolve_email", lambda request: "admin@x")
    projects.create("radio", "Radio", created_by="admin@x")
    projects.create("tv", "TV", created_by="admin@x")
    sandbox.reset()
    pr = tmp_path / "projects"
    for p in ("radio", "tv", "shared"):
        (pr / p / "work").mkdir(parents=True, exist_ok=True)
        (pr / p / "memory").mkdir(parents=True, exist_ok=True)
    (pr / "radio" / "work" / "mine.txt").write_text("radio-own-content\n")
    (pr / "tv" / "work" / "secret.txt").write_text("tv-confidential\n")
    (pr / "shared" / "work" / "conventions.md").write_text("shared-conventions\n")
    (tmp_path / "outside.txt").write_text("instance-file\n")
    yield {"tmp": tmp_path, "radio": str(pr / "radio" / "work"),
           "tv_secret": str(pr / "tv" / "work" / "secret.txt"),
           "shared": str(pr / "shared" / "work" / "conventions.md")}
    sandbox.reset()


def _decide(tool, inp, w, script=None, rules=None):
    import sandbox
    return sandbox.decide(tool, inp, sid="s1", user="alice@x", project="radio", cwd=w["radio"],
                          script=script, auto_rules=rules)


def _denied(out) -> bool:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


# ---- file tools -----------------------------------------------------------------------

def test_mode_detection_and_api_state(world, monkeypatch):
    import sandbox
    assert sandbox.mode() == "hooks-only"
    assert sandbox.applies("radio") and not sandbox.applies("default")
    monkeypatch.setenv("SOKKAN_FEATURE_SANDBOX", "0")
    assert sandbox.detect(force=True) == "off" and not sandbox.applies("radio")


def test_file_tools_stay_in_the_project(world):
    w = world
    # own workspace: allowed (relative and absolute)
    assert _decide("Read", {"file_path": "mine.txt"}, w) == {}
    assert _decide("Read", {"file_path": os.path.join(w["radio"], "mine.txt")}, w) == {}
    assert _decide("Write", {"file_path": "new.txt", "content": "x"}, w) == {}
    # shared: read yes, write no
    assert _decide("Read", {"file_path": w["shared"]}, w) == {}
    assert _denied(_decide("Edit", {"file_path": w["shared"], "old_string": "a",
                                    "new_string": "b"}, w))
    # another project, the instance's files, system files: no
    for tool, inp in [("Read", {"file_path": w["tv_secret"]}),
                      ("Read", {"file_path": "../../tv/work/secret.txt"}),
                      ("Read", {"file_path": str(world["tmp"] / "outside.txt")}),
                      ("Read", {"file_path": "/etc/passwd"}),
                      ("Write", {"file_path": w["tv_secret"], "content": "x"}),
                      ("Glob", {"pattern": "/etc/*"}),
                      ("Glob", {"pattern": "../../tv/**/*.txt"}),
                      ("Glob", {"pattern": "*.txt", "path": os.path.dirname(w["tv_secret"])}),
                      ("Grep", {"pattern": "confidential", "path": os.path.dirname(w["tv_secret"])}),
                      ("Grep", {"pattern": "x", "glob": "/root/**"}),
                      ("NotebookEdit", {"notebook_path": "/tmp/x.ipynb", "new_source": ""})]:
        assert _denied(_decide(tool, inp, w)), (tool, inp)
    # a Glob / Grep without a path searches the workspace: allowed
    assert _decide("Glob", {"pattern": "**/*.py"}, w) == {}
    assert _decide("Grep", {"pattern": "radio"}, w) == {}


def test_a_symlink_to_the_outside_is_refused(world):
    w = world
    link = os.path.join(w["radio"], "innocent.txt")
    os.symlink(w["tv_secret"], link)
    os.symlink(os.path.dirname(w["tv_secret"]), os.path.join(w["radio"], "tvdir"))
    assert _denied(_decide("Read", {"file_path": link}, w))
    assert _denied(_decide("Read", {"file_path": "tvdir/secret.txt"}, w))
    assert _denied(_decide("Write", {"file_path": "tvdir/new.txt", "content": "x"}, w))
    assert _denied(_decide("Grep", {"pattern": "x", "path": "tvdir"}, w))


def test_refusals_are_logged(world):
    import audit
    _decide("Read", {"file_path": world["tv_secret"]}, world)
    ev = [e for e in audit.recent(10) if e["action"] == "sandbox.deny"]
    assert ev and "radio:Read" in str(ev[0]) and "secret.txt" in str(ev[0])


# ---- Bash -----------------------------------------------------------------------------

def test_bash_is_refused_without_an_os_sandbox(world):
    out = _decide("Bash", {"command": f"cat {world['tv_secret']}"}, world, script=None)
    assert _denied(out)
    assert "bubblewrap" in out["hookSpecificOutput"]["permissionDecisionReason"]
    out = _decide("Bash", {"command": "ls"}, world, script=None)
    assert _denied(out)


def _run_wrapped(cmd_line: str, env=None) -> subprocess.CompletedProcess:
    """Run the hook's rewritten command the way the CLI does: a bash -c from the cwd."""
    return subprocess.run(["/bin/bash", "-c", cmd_line], capture_output=True, text=True,
                          timeout=30, env=env)


@needs_bwrap
def test_bash_runs_inside_bubblewrap(world, monkeypatch):
    import sandbox
    monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", BW)
    sandbox.reset()
    assert sandbox.mode() == "bwrap"
    w = world
    script = sandbox.wrapper_script("s1", "radio", w["radio"])

    def run(cmd, rules=None):
        out = _decide("Bash", {"command": cmd}, w, script=script, rules=rules)
        hs = out["hookSpecificOutput"]
        assert hs["permissionDecision"] in ("ask", "allow")
        return hs, _run_wrapped(hs["updatedInput"]["command"],
                                env={**os.environ, "SOKKAN_SCIM_TOKEN": "api-only-secret"})

    hs, r = run("cat mine.txt")
    assert hs["permissionDecision"] == "ask"          # a human still approves it
    assert r.returncode == 0 and "radio-own-content" in r.stdout
    # another project's file does not exist inside the sandbox — by path or by symlink
    _, r = run(f"cat {w['tv_secret']}")
    assert r.returncode != 0 and "tv-confidential" not in r.stdout
    os.symlink(w["tv_secret"], os.path.join(w["radio"], "link.txt"))
    _, r = run("cat link.txt")
    assert r.returncode != 0 and "tv-confidential" not in r.stdout
    _, r = run(f"cat {world['tmp'] / 'outside.txt'}")
    assert r.returncode != 0
    # the workspace is writable, shared is readable but not writable
    _, r = run("echo built > out.txt && cat out.txt")
    assert r.returncode == 0 and open(os.path.join(w["radio"], "out.txt")).read() == "built\n"
    _, r = run(f"cat {w['shared']} && echo x >> {w['shared']}")
    assert "shared-conventions" in r.stdout and r.returncode != 0
    # no API secret in the shell's environment; no network
    _, r = run("env")
    assert "api-only-secret" not in r.stdout and "SOKKAN_SANDBOX=bwrap" in r.stdout
    _, r = run("cat /proc/net/dev | grep -c : ; ls /sys/class/net 2>/dev/null | wc -l")
    assert r.stdout.split()[0] == "1"                  # loopback only
    # an agent's auto-approve rule approves, still inside the sandbox; a compound never
    hs, _ = run("cat mine.txt", rules=["Bash(cat:*)"])
    assert hs["permissionDecision"] == "allow"
    hs, _ = run(f"cat mine.txt; cat {w['tv_secret']}", rules=["Bash(cat:*)"])
    assert hs["permissionDecision"] == "ask"


@needs_bwrap
def test_an_approval_cannot_unwrap_the_command(world, monkeypatch):
    """The browser sends the approved input back: an edited, unwrapped command is
    re-wrapped before it runs."""
    import sandbox
    monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", BW)
    sandbox.reset()
    script = sandbox.wrapper_script("s2", "radio", world["radio"])
    out = sandbox.recheck("Bash", {"command": f"cat {world['tv_secret']}"}, sid="s2",
                          user="alice@x", project="radio", cwd=world["radio"])
    assert out["command"] == sandbox.wrap(script, f"cat {world['tv_secret']}")
    r = _run_wrapped(out["command"])
    assert "tv-confidential" not in r.stdout
    # already wrapped: unchanged (no double wrapping)
    assert sandbox.recheck("Bash", out, sid="s2", user="a", project="radio",
                           cwd=world["radio"]) == out
    assert sandbox.display_command(out["command"]) == f"cat {world['tv_secret']}"


# ---- the SDK session ------------------------------------------------------------------

class _FakeClient:
    last = None

    def __init__(self, options):
        _FakeClient.last = options

    async def __aenter__(self):
        return self


def _start(sid, monkeypatch, mode="default"):
    import agentchat
    monkeypatch.setattr(agentchat, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(agentchat.memrecall, "sdk_hooks", lambda *a, **k: {})
    s = agentchat.AgentSession(sid, cwd=agentchat.project_cwd(sid), user="alice@x")
    s.mode = mode
    asyncio.new_event_loop().run_until_complete(s.ensure_started())
    return s, _FakeClient.last


def test_session_of_a_project_is_confined(world, monkeypatch):
    import board
    import sandbox
    board.add_sdk_session("sid-radio", "t", project="radio")
    s, opts = _start("sid-radio", monkeypatch)
    assert opts.cwd == world["radio"] and s.sandboxed == "radio"
    assert list(opts.add_dirs) == []
    (m,) = [m for m in opts.hooks["PreToolUse"] if m.matcher == sandbox.HOOK_MATCHER]
    out = asyncio.new_event_loop().run_until_complete(
        m.hooks[0]({"tool_name": "Read", "tool_input": {"file_path": world["tv_secret"]}},
                   None, None))
    assert _denied(out)
    # defence in depth in the permission callback (bypass mode would allow anything)
    s.mode = "bypassPermissions"
    res = asyncio.new_event_loop().run_until_complete(
        s._can_use_tool("Read", {"file_path": world["tv_secret"]}, None))
    assert type(res).__name__ == "PermissionResultDeny"
    res = asyncio.new_event_loop().run_until_complete(
        s._can_use_tool("Bash", {"command": "id"}, None))
    assert type(res).__name__ == "PermissionResultDeny"       # hooks-only: no Bash


def test_default_keeps_its_behaviour(world, monkeypatch):
    import agentchat
    import board
    board.add_sdk_session("sid-default", "t", project="default")
    s, opts = _start("sid-default", monkeypatch)
    assert opts.cwd == agentchat.CWD and s.sandboxed is None
    assert not (opts.hooks or {}).get("PreToolUse")
    assert getattr(opts, "add_dirs", []) == []
    s.mode = "bypassPermissions"
    res = asyncio.new_event_loop().run_until_complete(
        s._can_use_tool("Read", {"file_path": world["tv_secret"]}, None))
    assert type(res).__name__ == "PermissionResultAllow"
    res = asyncio.new_event_loop().run_until_complete(
        s._can_use_tool("Bash", {"command": f"cat {world['tv_secret']}"}, None))
    assert type(res).__name__ == "PermissionResultAllow"
    assert res.updated_input == {"command": f"cat {world['tv_secret']}"}


def test_feature_off_keeps_the_lot3_behaviour(world, monkeypatch):
    import board
    import sandbox
    monkeypatch.setenv("SOKKAN_FEATURE_SANDBOX", "0")
    sandbox.reset()
    board.add_sdk_session("sid-radio2", "t", project="radio")
    s, opts = _start("sid-radio2", monkeypatch)
    assert opts.cwd == world["radio"] and s.sandboxed is None
    assert not (opts.hooks or {}).get("PreToolUse")


def test_api_features_reports_the_mode(world, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import sandbox
    sandbox.reset()
    body = TestClient(a.app).get("/api/features").json()
    assert body["sandbox"] == "hooks-only"
    item = next(i for i in body["registry"]["items"] if i["id"] == "sandbox")
    assert item["enabled"] and "hooks-only" in item["note"]
    if _bwrap_works():
        monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", BW)
        sandbox.reset()
        assert TestClient(a.app).get("/api/features").json()["sandbox"] == "bwrap"


def test_pod_mode_is_the_reference_isolation(world, monkeypatch):
    """Kubernetes / OpenShift restricted: no bubblewrap; sessions run in their own pod
    (session runner). The hook stays on for files; Bash is not refused nor wrapped."""
    from fastapi.testclient import TestClient

    import app as a
    import sandbox
    monkeypatch.setenv("SOKKAN_RUNNER", "kubernetes")
    monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", BW or "/nonexistent/bwrap")
    sandbox.reset()
    assert sandbox.mode() == "pod" and sandbox.applies("radio") and not sandbox.applies("default")
    assert _denied(sandbox.decide("Read", {"file_path": world["tv_secret"]}, sid="s", user="u",
                                  project="radio", cwd=world["radio"], script=None, pod=True))
    assert sandbox.decide("Bash", {"command": "make test"}, sid="s", user="u", project="radio",
                          cwd=world["radio"], script=None, pod=True) == {}
    assert sandbox.recheck("Bash", {"command": "make test"}, sid="s", user="u",
                           project="radio", cwd=world["radio"]) == {"command": "make test"}
    body = TestClient(a.app).get("/api/features").json()
    assert body["sandbox"] == "pod"
    assert "pod" in next(i for i in body["registry"]["items"] if i["id"] == "sandbox")["note"]


def test_pod_mode_follows_the_runner_not_the_variable_alone(monkeypatch, tmp_path):
    """night-runner × lot 8: `pod` (Bash allowed, the container is the boundary) only when the
    session runner REALLY spawns containers — SOKKAN_RUNNER=kubernetes with the feature
    `kubernetes_runner` resolved on. The variable alone (feature off) keeps hooks-only."""
    import runner
    import sandbox
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_SANDBOX", "1")
    monkeypatch.setenv("SOKKAN_SANDBOX_BWRAP", "/nonexistent/bwrap")
    monkeypatch.setenv("SOKKAN_RUNNER", "kubernetes")
    monkeypatch.setenv("SOKKAN_FEATURE_KUBERNETES_RUNNER", "0")
    assert runner.selected() == "local"
    assert sandbox.detect(force=True) == sandbox.HOOKS
    monkeypatch.setenv("SOKKAN_FEATURE_KUBERNETES_RUNNER", "1")
    assert runner.selected() == "kubernetes"
    assert sandbox.detect(force=True) == sandbox.POD
    sandbox.reset()
