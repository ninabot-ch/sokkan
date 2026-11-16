"""4.0 lot 0c: four-eyes on a session's gates, through the contract's gate.

A person's decision on a session gate goes through ONE call, `AgentSession.decide(gate_id,
Decision)`, whatever the engine and whoever decides (the rail, a reviewer the session was
shared with — `shared_review` —, Microsoft Teams). With `four_eyes` on and the approval
delegated to a reviewer, an allow needs another person than the session's owner. The journal
of validations, the engine and the run journal get the person who really decided.

Played on both kinds of engine: the fake engine of the conformance package (its gates are
born on its side, it is opaque: it sees the decider as a handle) and the CLI's adapter (the
gate the CLI's permission callback raises).
"""
import asyncio
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness import contract  # noqa: E402
from harness_helpers import ask  # noqa: E402
from test_ui_features import DEFAULT_SID, _share, world  # noqa: E402,F401 — the fixture


@pytest.fixture()
def four(world, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SOKKAN_FEATURE_AGENTS", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_FOUR_EYES", "1")
    monkeypatch.delenv("SOKKAN_AGENTS_APPROVAL", raising=False)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    world["loop"] = loop
    yield world
    loop.call_soon_threadsafe(loop.stop)
    t.join(2)


def _delegate(w, to="dave@x"):
    bob = w["as"]("bob@x")
    sid = _share(bob, access="write", principal=to).json()["id"]
    assert bob.post(f"/api/shares/{sid}/approver", json={"approver": True}).json()["approver"]
    return sid


def _session(w, monkeypatch, engine=None, model=None):
    import agentchat
    sess = agentchat.AgentSession(DEFAULT_SID, cwd=str(w["tmp"]), user="bob@x")
    monkeypatch.setattr(agentchat, "peek", lambda sid: sess if sid == DEFAULT_SID else None)
    if engine is not None:
        sess.attach(engine, contract.RunSpec(run_id=DEFAULT_SID, cwd=str(w["tmp"]),
                                             user="bob@x"))
    return sess


def _wait_gate(sess):
    for _ in range(500):
        g = sess.pending_gates()
        if g:
            return g[0]["id"]
        threading.Event().wait(0.01)
    raise AssertionError("no gate is waiting")


def _hitl(pid):
    import hitl_journal
    con = hitl_journal._con()
    try:
        con.row_factory = __import__("sqlite3").Row
        return dict(con.execute("SELECT * FROM hitl WHERE request_id = ?", (pid,)).fetchone())
    finally:
        con.close()


def test_on_an_engine_that_raises_its_own_gates_the_owner_cannot_approve_alone(four, monkeypatch):
    import run_journal
    from harness import identity
    from harness.conformance import driver, reference
    model = reference.ReferenceModel()
    model.script.append([driver.shell("touch reviewed.txt"), driver.say("Done.")])
    seen = []

    class Engine(reference.ReferenceHarness):
        async def decide(self, gate_id, decision):
            seen.append(decision)
            return await super().decide(gate_id, decision)

    sess = _session(four, monkeypatch, Engine(model))
    share = _delegate(four)
    turn = asyncio.run_coroutine_threadsafe(sess.handle_user("tidy up"), four["loop"])
    pid = _wait_gate(sess)
    # the owner, from the rail: refused, the request keeps waiting
    out = sess.decide_threadsafe(pid, contract.Decision.allow(by="bob@x", via="rail"))
    assert out == "four_eyes" and [g["id"] for g in sess.pending_gates()] == [pid]
    assert "another person" in sess.four_eyes_refusal(pid, contract.Decision.allow(by="bob@x"))
    # an unknown decider is not another person either
    assert sess.decide_threadsafe(pid, contract.Decision.allow()) == "four_eyes"
    # the reviewer, through the share
    inbox = four["as"]("dave@x").get("/api/shares/inbox").json()
    assert [p["id"] for p in next(s for s in inbox if s["id"] == share)["pending"]] == [pid]
    r = four["as"]("dave@x").post(f"/api/shares/{share}/decide",
                                  json={"permission_id": pid, "decision": "allow"})
    assert r.status_code == 200, r.text
    turn.result(10)
    assert (four["tmp"] / "reviewed.txt").exists()
    # the engine got the reviewer — as a handle, it is an opaque engine — through the share
    (d,) = seen
    assert d.kind == "allow_once" and d.via == "share"
    assert d.by.startswith("usr-") and identity.resolve(d.by) == ("user", "dave@x")
    # the journals name the person who really decided
    row = _hitl(pid)
    assert (row["decision"], row["actor"], row["origin"]) == ("allow", "dave@x", "share")
    asyncio.run_coroutine_threadsafe(sess.close(), four["loop"]).result(5)
    (res,) = [e for e in run_journal.events(DEFAULT_SID) if e["type"] == "gate.resolved"]
    assert (res["decision"], res["by"], res["via"]) == ("allow_once", "dave@x", "share")


def test_on_the_cli_adapter_the_reviewer_decides_and_the_decision_says_who(four, monkeypatch):
    sess = _session(four, monkeypatch)
    share = _delegate(four)
    fut = asyncio.run_coroutine_threadsafe(ask(sess, "Bash", {"command": "make deploy"}),
                                           four["loop"])
    pid = _wait_gate(sess)
    assert sess.decide_threadsafe(pid, contract.Decision.allow(by="bob@x", via="rail")) == "four_eyes"
    # Teams by the owner: the same rule
    assert sess.decide_threadsafe(pid, contract.Decision.allow(by="bob@x", via="teams")) == "four_eyes"
    r = four["as"]("dave@x").post(f"/api/shares/{share}/decide",
                                  json={"permission_id": pid, "decision": "allow"})
    assert r.status_code == 200, r.text
    d = fut.result(5)
    assert (d.kind, d.by, d.via) == ("allow_once", "dave@x", "share")
    row = _hitl(pid)
    assert (row["decision"], row["actor"], row["origin"]) == ("allow", "dave@x", "share")


def test_a_refusal_is_anybodys_and_an_approval_mode_does_not_stand_in(four, monkeypatch):
    sess = _session(four, monkeypatch)
    _delegate(four)
    sess.mode = "bypassPermissions"
    fut = asyncio.run_coroutine_threadsafe(ask(sess, "Bash", {"command": "rm -rf build"}),
                                           four["loop"])
    pid = _wait_gate(sess)                  # bypass did not auto-allow: a person decides
    assert sess.decide_threadsafe(pid, contract.Decision.deny("no", by="bob@x", via="rail")) == "ok"
    d = fut.result(5)
    assert (d.kind, d.by) == ("deny", "bob@x")
    assert _hitl(pid)["actor"] == "bob@x"


def test_without_a_delegated_review_or_without_four_eyes_the_owner_decides(four, monkeypatch):
    sess = _session(four, monkeypatch)
    fut = asyncio.run_coroutine_threadsafe(ask(sess, "Bash", {"command": "make"}), four["loop"])
    pid = _wait_gate(sess)
    assert sess.decide_threadsafe(pid, contract.Decision.allow(by="bob@x", via="rail")) == "ok"
    assert fut.result(5).kind == "allow_once"
    _delegate(four)
    monkeypatch.setenv("SOKKAN_FEATURE_FOUR_EYES", "0")
    fut = asyncio.run_coroutine_threadsafe(ask(sess, "Bash", {"command": "make"}), four["loop"])
    pid = _wait_gate(sess)
    assert sess.decide_threadsafe(pid, contract.Decision.allow(by="bob@x", via="rail")) == "ok"
    assert fut.result(5).kind == "allow_once"


def test_a_gate_the_engine_settles_itself_is_journalled_as_such(four, monkeypatch):
    from harness.conformance import driver, reference
    model = reference.ReferenceModel()
    model.script.append([driver.shell("touch never.txt"), driver.say("Gave up.")])
    engine = reference.ReferenceHarness(model)
    sess = _session(four, monkeypatch, engine)
    turn = asyncio.run_coroutine_threadsafe(sess.handle_user("go"), four["loop"])
    pid = _wait_gate(sess)

    async def expire():
        return engine.expire(pid)
    assert asyncio.run_coroutine_threadsafe(expire(), four["loop"]).result(5)
    turn.result(10)
    assert not (four["tmp"] / "never.txt").exists()
    row = _hitl(pid)
    assert (row["decision"], row["actor"], row["origin"]) == ("deny", "", "system")
    assert "settled by the engine (timeout)" in row["message"]
    assert sess.decide(pid, contract.Decision.allow(by="dave@x", via="share")) == "unknown"
    asyncio.run_coroutine_threadsafe(sess.close(), four["loop"]).result(5)
