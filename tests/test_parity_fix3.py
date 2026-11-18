"""Parity bench, passage 3: the bench's own defects found on run n°2 (17.11.2026) and the kernel
adapter's. No engine, no model: the meter against a local upstream, the grading on recorded
events, the adapter against a mocked kernel API (httpx.MockTransport)."""
import asyncio
import json
import os
import stat
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "parity"))
sys.path.insert(0, str(HERE.parent / "backend"))

import policy  # noqa: E402
import run  # noqa: E402
import wire  # noqa: E402
from harness import contract, kernel  # noqa: E402


# ---- 1. the meter isolates a run, 4. it keeps what the model was offered ------------------
def test_a_run_never_counts_the_requests_of_another_and_the_tools_are_kept(tmp_path):
    class Up(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            out = json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}},
             {"type": "function", "function": {"name": "write_file", "parameters": {}}}]

    def call(url):
        body = {"messages": [{"role": "system", "content": "s"}], "tools": tools}
        req = urllib.request.Request(url + "/v1/chat/completions", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()

    try:
        with wire.Wire(f"http://127.0.0.1:{up.server_address[1]}/v1",
                       str(tmp_path / "w.jsonl")) as w:
            remote = wire.RemoteWire(w.url)       # the lab's way: by HTTP
            # an older bench (a smoke) left requests under the SAME journey label
            remote.tag = "kernel/D-P01/p1"
            for _ in range(3):
                call(w.url)
            # this bench: its own nonce in the tag
            remote.tag = "20261117-1-abc/kernel/D-P01/p1"
            call(w.url)
            remote.tag = ""
            mine = remote.of("20261117-1-abc/kernel/D-P01/p1")
            assert len(mine) == 1
            assert mine[0]["tool_names"] == ["read_file", "write_file"]
            assert mine[0]["tools_sha"] and mine[0]["system_sha"]
            assert remote.forget("20261117-1-abc/kernel/D-P01/p1") == 1
            assert remote.of("20261117-1-abc/kernel/D-P01/p1") == []
            assert len(remote.of("kernel/D-P01/p1")) == 3
    finally:
        up.shutdown()


def test_offered_reads_both_formats():
    m = wire.offered("messages", {"system": "x", "tools": [{"name": "Bash"}, {"name": "Read"}]})
    assert m["tool_names"] == ["Bash", "Read"] and m["system_sha"]
    assert wire.offered("chat", {"messages": []}) == {"tool_names": [], "tools_sha": "",
                                                      "system_sha": ""}


# ---- 3. a shell that RAN, never a refusal whatever its `ok` ------------------------------
class _Play(run.Play):
    def __init__(self, events, engine="kernel"):
        self.events = [(0.0, e) for e in events]
        self.pol = policy.Policy("atelier")
        self.engine = engine


def _shell(call, out, ok=True, name="terminal"):
    return [{"type": "tool.called", "call_id": call, "name": name, "classification": "shell",
             "input": {"command": "cp a b"}},
            {"type": "tool.result", "call_id": call, "ok": ok, "output": out}]


def test_a_refusal_published_as_a_success_is_not_a_shell_that_ran():
    p = _Play([*_shell("c1", "Refusé : règle deny terminal(). Ne le réessaie pas.", ok=True),
               *_shell("c2", "No such tool available: Bash. Bash is disabled", ok=False),
               *_shell("c3", policy.NO_SHELL, ok=False),
               *_shell("c4", "EN ATTENTE D'APPROBATION HUMAINE (x)", ok=True)])
    assert p.shell_ran() == 0
    # a command that ran and failed still ran
    p2 = _Play([*_shell("c1", "ls: cannot access 'x': No such file", ok=False),
                *_shell("c2", "total 8", ok=True)])
    assert p2.shell_ran() == 2


# ---- 2. R2 (decision 17.11): parasitic = what the simple view shows as a failure ---------
def test_r2_judges_failure_lines_by_the_simple_view():
    j = run.J.Journey(id="R2-memoire-directe", title="", kind="refusal", ask="")
    events = [
        {"type": "tool.called", "call_id": "r1", "name": "read_file", "classification": "file_read",
         "input": {"path": "/srv/x/projects/parity/memory"}},
        {"type": "tool.result", "call_id": "r1", "ok": False, "output": "Ce chemin n'existe pas"}]
    p = _Play(events)
    p.turns, p.timed_out = [{"is_error": False}], False
    p.h = object()
    out = {"tool_calls": p.tool_calls(), "memory_files": [], "shell_ran": 0, "engine": "legacy",
           "cards": {"shell_refused": 0, "memory_write_refused": 0}}
    g = run.grade_refusal(j, p, out, None)
    # the person reads « une action a échoué »: a parasitic line, whatever the engine
    assert not g["checks"]["no_parasitic_failure"]
    ok = _Play(events[:1] + [{**events[1], "ok": True, "output": "note.md"}])
    ok.turns, ok.timed_out, ok.h = [{"is_error": False}], False, object()
    out2 = {**out, "tool_calls": ok.tool_calls()}
    assert run.grade_refusal(j, ok, out2, None)["success"]
    out2["shell_ran"] = 1                      # R2 says it too: the Atelier never runs a shell
    assert not run.grade_refusal(j, ok, out2, None)["success"]


# ---- 5. N passes per repository task, 2 by default ----------------------------------------
def test_repository_tasks_get_two_passes_by_default(monkeypatch):
    seen = {}

    def fake_parse(self, args=None, namespace=None):
        ns = real(self, ["--report", "x"])
        seen["repo_passes"] = ns.repo_passes
        raise SystemExit(0)

    import argparse
    real = argparse.ArgumentParser.parse_args
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse)
    with pytest.raises(SystemExit):
        run.main()
    assert seen["repo_passes"] == 2


# ---- the adapter against a mocked kernel --------------------------------------------------
class _Never(httpx.AsyncByteStream):
    """An event stream that stays open and says nothing (as a quiet kernel does)."""

    async def __aiter__(self):
        await asyncio.sleep(3600)
        yield b""


class _Kernel:
    def __init__(self, unknown=None, delete_status=200):
        self.sessions, self.modes, self.unknown = [], [], unknown
        self.deleted, self.delete_status = [], delete_status

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if req.method == "DELETE" and p.startswith("/v1/local/sessions/"):
            self.deleted.append(p.rsplit("/", 1)[1])
            if self.delete_status == 200:
                return httpx.Response(200, json={"closed": True, "thread_id": self.deleted[-1]})
            return httpx.Response(self.delete_status, json={})
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
            self.modes.append(m)
            return httpx.Response(200, json={"mode": m, "effective": m})
        if p == "/v1/local/setup":
            return httpx.Response(200, json={"models": {}})
        return httpx.Response(404, json={})


@pytest.fixture
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


def test_atelier_session_removes_the_shell_and_the_kernel_memory_tool(mocked, tmp_path):
    k = mocked()
    h, _ = _open(contract.RunSpec(run_id="r1", cwd=str(tmp_path), shell=False))
    body = k.sessions[0]
    assert {"terminal", "process_start", "memory_note"} <= set(body["deny_tools"])
    assert k.modes == ["default"] and h.kernel_mode == "default"
    assert h.opened_with == body


def test_the_deny_variable_absent_empty_or_set_and_the_atelier_list_stays(mocked, tmp_path, monkeypatch):
    """SOKKAN_KERNEL_DENY_TOOLS: absent = memory_note, set EMPTY = no extra list, set = that
    list; the Atelier's list (every shell tool + memory_note) is the code's, whatever it says.
    The compose file keeps the same meaning (`${…-memory_note}`, not `:-`)."""
    monkeypatch.delenv("SOKKAN_KERNEL_DENY_TOOLS", raising=False)
    assert kernel._always_denied() == ["memory_note"]
    monkeypatch.setenv("SOKKAN_KERNEL_DENY_TOOLS", "")
    assert kernel._always_denied() == []
    monkeypatch.setenv("SOKKAN_KERNEL_DENY_TOOLS", "web_search, memory_note")
    assert kernel._always_denied() == ["web_search", "memory_note"]
    compose = (HERE.parent / "docker-compose.yml").read_text()
    assert "SOKKAN_KERNEL_DENY_TOOLS: ${SOKKAN_KERNEL_DENY_TOOLS-memory_note}" in compose
    for env in ("", None):
        k = mocked()                      # (clears the variable)
        if env is not None:
            monkeypatch.setenv("SOKKAN_KERNEL_DENY_TOOLS", env)
        _open(contract.RunSpec(run_id="ra", cwd=str(tmp_path), shell=False))
        assert set(kernel.ATELIER_DENIED) <= set(k.sessions[0]["deny_tools"]), env
        k2 = mocked()
        if env is not None:
            monkeypatch.setenv("SOKKAN_KERNEL_DENY_TOOLS", env)
        _open(contract.RunSpec(run_id="rc", cwd=str(tmp_path)))
        got = k2.sessions[0].get("deny_tools") or []
        assert got == ([] if env == "" else ["memory_note"]), (env, got)


def test_cockpit_session_gets_the_kernel_mode_of_the_cockpit_rule(mocked, tmp_path):
    k = mocked()
    h, _ = _open(contract.RunSpec(run_id="r1", cwd=str(tmp_path),
                                  approval_mode="bypassPermissions"))
    assert k.modes == ["auto"] and "terminal" not in k.sessions[0]["deny_tools"]
    k2 = mocked()
    _open(contract.RunSpec(run_id="r2", cwd=str(tmp_path), approval_mode="acceptEdits"))
    assert k2.modes == ["accept_edits"]


def test_masked_opening_body(mocked, tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_KERNEL_MCP_HEADERS", "1")
    mocked()
    h, _ = _open(contract.RunSpec(run_id="r1", cwd=str(tmp_path), mcp_servers={
        "sokkan-memory": {"url": "http://m", "headers": {"Authorization": "Bearer secret"}}}))
    assert h.opened_with["mcp_headers"] == {"sokkan-memory": {"Authorization": "***"}}
    assert "secret" not in json.dumps(h.opened_with)


def test_a_deny_name_the_kernel_did_not_match_is_said(mocked, tmp_path):
    mocked(unknown=["memory_note", "ssh_run"])     # ssh_run: best effort, not expected
    h, evs = _open(contract.RunSpec(run_id="r1", cwd=str(tmp_path), shell=False))
    assert h.deny_unmatched == ["memory_note"]
    errs = [e for e in evs if isinstance(e, contract.Error)]
    assert any(e.code == "deny_tools_unmatched" and e.recoverable for e in errs)
    mocked()                                        # older kernel: no field, nothing said
    h2, _ = _open(contract.RunSpec(run_id="r2", cwd=str(tmp_path), shell=False))
    assert h2.deny_unmatched == []


def test_a_refusal_with_a_reason_is_a_failed_result():
    h = kernel.KernelHarness()
    h._turn, h._thread = "t1", "th_1"
    h._on_tool_result({"call_id": "c1", "name": "terminal", "is_error": False,
                       "reason": "deny_rule", "preview": "Refusé : règle deny terminal()."})
    h._on_tool_result({"call_id": "c2", "name": "terminal", "is_error": False,
                       "preview": "total 8"})
    a, b = h._mail.get_nowait(), h._mail.get_nowait()
    assert a.ok is False and b.ok is True


def test_the_alias_folder_belongs_to_the_run_folder_owner(tmp_path):
    cwd = tmp_path / "work"
    hexdir = cwd / "documents" / "0123456789abcdef"
    hexdir.mkdir(parents=True)
    os.chmod(cwd, 0o775)
    h = kernel.KernelHarness()
    h._spec = contract.RunSpec(run_id="r1", cwd=str(cwd))
    text = h._alias_paths(f"read {hexdir}/Devis.txt")
    assert ".sokkan-kernel" in text
    base = cwd / ".sokkan-kernel"
    run_dir = next(base.iterdir())
    for d in (base, run_dir):
        st = os.stat(d)
        assert stat.S_IMODE(st.st_mode) == 0o775
        assert (st.st_uid, st.st_gid) == (os.stat(cwd).st_uid, os.stat(cwd).st_gid)


# ---- the cockpit tells the engine a wider mode only when no « never » rule can fire --------
def test_engine_approval_mode_stays_default_when_a_never_rule_could_fire():
    import agentchat
    s = object.__new__(agentchat.AgentSession)
    s.mode, s.policy, s.sandboxed, s._simple_work = "bypassPermissions", None, None, False
    s.sid = "s-none"
    s._has_documents = lambda: False
    assert s._engine_approval_mode() == "bypassPermissions"
    s._has_documents = lambda: True
    assert s._engine_approval_mode() == "default"
    s._has_documents = lambda: False
    s._simple_work = True
    assert s._engine_approval_mode() == "default"
    s._simple_work, s.policy = False, {"agent": "a"}
    assert s._engine_approval_mode() == "default"
    s.policy, s.mode = None, "default"
    assert s._engine_approval_mode() == "default"


# ---- kernel host-2a: a gate refusal says `status: "refused"` (ok / is_error unchanged) -----
def test_adapter_reads_status_refused_and_stays_compatible():
    h = kernel.KernelHarness()
    h._turn, h._thread = "t1", "th_1"
    h._on_tool_result({"call_id": "c1", "name": "terminal", "ok": True, "is_error": False,
                       "status": "refused", "preview": "Refusé : règle deny terminal()."})
    h._on_tool_result({"call_id": "c2", "name": "terminal", "is_error": False,
                       "reason": "deny_rule", "preview": "Refusé."})        # older kernel
    h._on_tool_result({"call_id": "c3", "name": "terminal", "is_error": True,
                       "preview": "exit 1"})                                 # ran, failed
    h._on_tool_result({"call_id": "c4", "name": "terminal", "is_error": False,
                       "preview": "total 8"})                                # ran
    a, b, c, d = (h._mail.get_nowait() for _ in range(4))
    assert (a.ok, a.refused) == (False, True) and (b.ok, b.refused) == (False, True)
    assert (c.ok, c.refused) == (False, False) and (d.ok, d.refused) == (True, False)
    assert a.to_dict()["refused"] is True


def test_bench_reads_status_refused_for_shell_ran_and_parasites():
    neutral = "Action non permise ici."         # a refusal text the regex does not know
    for res in ({"refused": True}, {"status": "refused"}):
        p = _Play([{"type": "tool.called", "call_id": "c1", "name": "terminal",
                    "classification": "shell", "input": {"command": "cp a b"}},
                   {"type": "tool.result", "call_id": "c1", "ok": True, "output": neutral, **res}])
        assert p.shell_ran() == 0
        p2 = _Play([{"type": "tool.called", "call_id": "c1", "name": "terminal",
                     "classification": "shell", "input": {"command": "cp a b"}},
                    {"type": "tool.result", "call_id": "c1", "ok": False, "output": neutral,
                     **res}])
        f = p2.failures()
        assert f["unexpected_tool_failures"] == 0 and f["refused_by_engine"] == 1
    # without the status, the same failed result is a failure line
    p3 = _Play([{"type": "tool.result", "call_id": "c1", "ok": False, "output": neutral}])
    assert p3.failures()["unexpected_tool_failures"] == 1


# ---- decision 17.11: the SAME rule for both engines, read through the simple view ---------
# (3.6.7: a call a rule of the space turns away is not shown; a real failure still is)
def _call(cid, name, cls, inp, ok, out, **kw):
    return [{"type": "tool.called", "call_id": cid, "name": name, "classification": cls,
             "input": inp},
            {"type": "tool.result", "call_id": cid, "ok": ok, "output": out, **kw}]


def test_the_simple_view_decides_a_failure_line_for_every_engine():
    from harness import refusals
    legacy = _Play(_call("a", "Bash", "shell", {"command": "mkdir x"}, False, policy.NO_SHELL)
                   + _call("b", "Bash", "shell", {"command": "mkdir x"}, False,
                           refusals.removed("Bash", "shell", "No such tool available: Bash."))
                   + _call("c", "Read", "file_read", {"file_path": "/w/a.txt"}, True, "hello"),
                   engine="legacy")
    kern = _Play(_call("a", "terminal", "shell", {"command": "mkdir x"}, False,
                       refusals.removed("terminal", "shell", "Refusé : règle deny terminal()."),
                       refused=True)
                 + _call("b", "read_file", "file_read", {"path": "/workspace/.sokkan-kernel/r/"},
                         False, "not a regular file (directory)")
                 + _call("c", "read_file", "file_read", {"path": "/w/a.txt"}, True, "hello"))
    lv, kv = legacy.view_lines(), kern.view_lines()
    assert (lv["fail"], lv["discreet"], lv["done"]) == (0, 2, 1)
    # the kernel's rule refusal is hidden like the CLI's; a read of a folder is a real failure
    assert (kv["fail"], kv["discreet"], kv["done"]) == (1, 1, 1)
    # an engine's own refusal of a tool the cockpit did NOT remove stays a failure line
    other = _Play(_call("a", "terminal", "shell", {"command": "x"}, False, "Refusé : hardline"))
    assert other.view_lines()["fail"] == 1


def test_thread_state_is_the_threadmodel_rule():
    src = (HERE.parent / "frontend" / "lib" / "simple" / "threadModel.ts").read_text()
    # the copy in run.py follows this line of the front; a change there must change the copy
    assert ('e.ok ? "done" : e.reason === "denied" ? "denied" : e.reason === "policy" ? '
            '"skipped" : "fail"') in src
    assert run.thread_state({"ok": True}) == "done"
    assert run.thread_state({"ok": False}) == "fail"
    assert run.thread_state({"ok": False, "reason": "policy"}) == "skipped"
    assert run.thread_state({"ok": False, "reason": "denied"}) == "denied"


def test_kernel_adapter_says_a_removed_tool_in_the_cockpit_words():
    from harness import refusals
    h = kernel.KernelHarness()
    h._turn, h._thread, h._removed = "t1", "th_1", {"terminal", "memory_note"}
    h._on_tool_result({"call_id": "c1", "name": "terminal", "is_error": False,
                       "status": "refused", "preview": "Refusé : règle deny terminal()."})
    h._on_tool_result({"call_id": "c2", "name": "write_file", "is_error": False,
                       "reason": "hardline", "preview": "Refusé : hardline."})
    a, b = h._mail.get_nowait(), h._mail.get_nowait()
    assert a.refused and a.output.startswith(refusals.NO_SHELL) and "deny terminal" in a.output
    assert b.refused and b.output == "Refusé : hardline."


def test_legacy_adapter_says_a_removed_tool_in_the_cockpit_words():
    from claude_agent_sdk.types import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
    from harness import legacy, refusals
    h = legacy.LegacyHarness(client=object())
    h._removed = {"Bash"}
    h.translate(AssistantMessage(content=[ToolUseBlock(id="u1", name="Bash",
                                                       input={"command": "mkdir x"})],
                                 model="m"))
    evs = h.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="u1", content="<tool_use_error>Error: No such tool available: Bash.",
        is_error=True)]))
    r = [e for e in evs if isinstance(e, contract.ToolResult)][0]
    assert r.refused and r.output.startswith(refusals.NO_SHELL) and not r.ok


def test_the_view_hides_a_rule_of_the_space_and_keeps_a_real_failure(tmp_path):
    import plain
    v = plain.View()
    out = []
    for e in ({"type": "tool_use", "id": "t1", "tool": "Bash", "input": {"command": "mkdir x"}},
              {"type": "tool_result", "tool_use_id": "t1", "text": policy.NO_SHELL, "is_error": True},
              {"type": "tool_use", "id": "t2", "tool": "Read", "input": {"file_path": "/nope"}},
              {"type": "tool_result", "tool_use_id": "t2", "text": "File does not exist.",
               "is_error": True}):
        out += v.feed(e)
    done = [e for e in out if e["type"] == "activity_done"]
    assert done[0]["reason"] == "policy" and "reason" not in done[1]


def _open_then_stop(spec):
    async def go():
        h = kernel.KernelHarness()
        stream = h.start(spec)
        await stream.__anext__()
        await h.stop()
        evs = []
        while not h._mail.empty():
            evs.append(h._mail.get_nowait())
        return h, evs
    return asyncio.run(go())


def test_the_end_of_a_run_closes_the_kernel_session_and_its_sandbox(mocked, tmp_path):
    """Kernel host-2: `DELETE /v1/local/sessions/{thread}` removes the session's sandbox (the
    thread stays, a resumed run reopens it). The adapter calls it once, at stop()."""
    k = mocked()
    h, evs = _open_then_stop(contract.RunSpec(run_id="r1", cwd=str(tmp_path), shell=False))
    assert k.deleted == ["th_1"] and h.session_closed is True
    ended = [e for e in evs if isinstance(e, contract.RunEnded)]
    assert ended and ended[0].resumable and ended[0].engine_session == "th_1"


def test_a_kernel_without_the_route_still_ends_the_run(mocked, tmp_path):
    k = mocked(delete_status=404)
    h, evs = _open_then_stop(contract.RunSpec(run_id="r1", cwd=str(tmp_path)))
    assert k.deleted == ["th_1"] and h.session_closed is False
    assert any(isinstance(e, contract.RunEnded) for e in evs)
