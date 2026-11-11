"""The journal of human validations (backend/hitl_journal.py): every approval asked and every
decision is durable — who, what (secrets masked), when asked, when decided, how long, from
where — and an automatic approval never looks like a person's.

The decisions go through the real paths: `AgentSession._can_use_tool` + `resolve_permission`,
the cockpit's WebSocket, a delegated share (`/api/shares/{id}/decide`), Microsoft Teams
(`teams.bot._decide_tool`). No model, no SDK process: the tool callback is called directly.
Nothing here is real data (people are `…@x`, secrets are made up).
"""
import asyncio
import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

SID = "a" * 32
RADIO_SID = "b" * 32
VAULT_SECRET = "hunter2-VAULT-value-9f8e7d6c5b4a"
PASTED_KEY = "sk-live-0123456789abcdefABCDEF"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import agentchat
    import app as a
    import audit
    import auth
    import board
    import iam
    import projects
    import sharing
    import vault

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    for f in ("MULTI_PROJECT", "SHARED_REVIEW"):
        monkeypatch.setenv(f"SOKKAN_FEATURE_{f}", "1")
    monkeypatch.delenv("SOKKAN_EDITION", raising=False)
    monkeypatch.delenv("SOKKAN_FEATURE_STUDY_MODE", raising=False)
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    for e, r in (("bob@x", "dev"), ("dave@x", "dev"), ("carol@x", "viewer"), ("admin@x", "admin")):
        iam.upsert_user(e, r)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(sharing, "DB", tmp_path / "shares.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    projects.create("radio", "Radio player", created_by="admin@x")
    projects.grant("radio", "user", "rita@x", "admin")
    projects.grant("radio", "user", "ray@x", "dev")
    board.add_sdk_session(SID, "backend", title="default plan", project="default", owner="bob@x")
    board.add_sdk_session(RADIO_SID, "backend", title="radio work", project="radio",
                          owner="ray@x")
    monkeypatch.setattr(agentchat, "_registry", {})
    who = {"email": "bob@x"}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    c = TestClient(a.app)

    def as_(email, project=None):
        who["email"] = email
        c.headers.pop("x-sokkan-project", None)
        if project:
            c.headers["x-sokkan-project"] = project
        return c

    return {"as": as_, "tmp": tmp_path, "client": c, "who": who}


def _rows(**kw):
    import hitl_journal
    return hitl_journal.query(**kw)


def _session(sid=SID, user="bob@x", **kw):
    import agentchat
    return agentchat.AgentSession(sid, cwd="/tmp", user=user, **kw)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _ask(sess, tool="Bash", inp=None):
    """Start a tool call and wait until the session is waiting for a person."""
    task = asyncio.ensure_future(sess._can_use_tool(tool, inp or {"command": "rm -rf build"}, None))
    for _ in range(200):
        await asyncio.sleep(0)
        if sess._perms or sess._questions or task.done():
            break
    return task


async def _ask_and_decide(sess, decision, actor="bob@x", origin="owner", wait=0.0, **kw):
    import hitl_journal
    task = await _ask(sess, **kw)
    pid = next(iter(sess._perms))
    if wait:
        await asyncio.sleep(wait)
    if actor is not None:
        hitl_journal.attribute(pid, actor, origin)
    sess.resolve_permission(pid, decision)
    return pid, await task


# ---- what a row holds -------------------------------------------------------------------
def test_an_owner_allow_is_written_with_who_what_when_and_how_long(world):
    sess = _session()
    t0 = time.time()
    pid, res = _run(_ask_and_decide(sess, {"decision": "allow"}, wait=0.05))
    assert isinstance(res, PermissionResultAllow)
    (r,) = _rows(session_id=SID)
    assert r["request_id"] == pid and r["kind"] == "permission" and r["decision"] == "allow"
    assert (r["project"], r["owner"], r["actor"], r["origin"]) == ("default", "bob@x", "bob@x",
                                                                   "owner")
    assert r["tool"] == "Bash" and r["title"] == "rm -rf build"
    assert json.loads(r["input_summary"]) == {"command": "rm -rf build"}
    assert t0 <= r["requested_at"] < r["decided_at"] <= time.time()
    assert r["delay_ms"] >= 50 and r["delay_ms"] == round((r["decided_at"] - r["requested_at"]) * 1000)
    assert (r["auto"], r["edited"], r["mode"]) == (0, 0, "default")


def test_a_pending_request_is_already_on_disk_before_anyone_decides(world):
    sess = _session()

    async def go():
        task = await _ask(sess)
        (r,) = _rows(session_id=SID)
        assert r["decision"] == "pending" and r["decided_at"] is None and r["actor"] == ""
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _run(go())


def test_a_deny_keeps_the_reason(world):
    sess = _session()
    _pid, res = _run(_ask_and_decide(sess, {"decision": "deny", "message": "not on a Friday"}))
    assert isinstance(res, PermissionResultDeny)
    (r,) = _rows(session_id=SID)
    assert r["decision"] == "deny" and r["message"] == "not on a Friday" and r["actor"] == "bob@x"


def test_an_allow_with_a_modified_input_is_an_edit_and_keeps_what_ran(world):
    sess = _session()
    _pid, res = _run(_ask_and_decide(
        sess, {"decision": "allow", "updated_input": {"command": "rm -rf build/tmp"}}))
    assert res.updated_input == {"command": "rm -rf build/tmp"}
    (r,) = _rows(session_id=SID)
    assert r["decision"] == "edit" and r["edited"] == 1
    assert json.loads(r["input_summary"]) == {"command": "rm -rf build"}        # what was asked
    assert json.loads(r["edited_summary"]) == {"command": "rm -rf build/tmp"}   # what ran


def test_an_allow_that_sends_the_same_input_back_is_not_an_edit(world):
    sess = _session()
    _run(_ask_and_decide(sess, {"decision": "allow", "updated_input": {"command": "rm -rf build"}}))
    assert _rows(session_id=SID)[0]["decision"] == "allow"


def test_the_answer_to_a_question_is_journaled(world):
    import hitl_journal
    sess = _session()

    async def go():
        task = await _ask(sess, tool="AskUserQuestion",
                          inp={"questions": [{"question": "Which env?", "options": ["stg", "prod"]}]})
        qid = next(iter(sess._questions))
        hitl_journal.attribute(qid, "bob@x", "owner")
        sess.resolve_question(qid, {"Which env?": "stg"})
        return await task
    res = _run(go())
    assert res.updated_input["answers"] == {"Which env?": "stg"}
    (r,) = _rows(session_id=SID)
    assert (r["kind"], r["decision"], r["tool"], r["actor"]) == ("question", "answer",
                                                                 "AskUserQuestion", "bob@x")
    assert json.loads(r["message"]) == {"Which env?": "stg"} and r["delay_ms"] is not None


def test_a_change_of_approval_mode_is_journaled(world):
    sess = _session()
    _run(sess.set_mode("bypassPermissions", actor="bob@x"))
    _run(sess.set_mode("bypassPermissions", actor="bob@x"))     # no change: no second row
    (r,) = _rows(session_id=SID)
    assert (r["kind"], r["decision"], r["title"], r["mode"], r["actor"], r["origin"]) == (
        "mode", "mode_change", "default → bypassPermissions", "bypassPermissions", "bob@x", "owner")


# ---- an automatic approval never looks like a person's -----------------------------------
def test_auto_approvals_are_told_apart_from_a_validation(world):
    sess = _session()
    _run(sess.set_mode("acceptEdits", actor="bob@x"))
    res = _run(sess._can_use_tool("Edit", {"file_path": "/tmp/x.py", "old_string": "a",
                                           "new_string": "b"}, None))
    assert isinstance(res, PermissionResultAllow)
    _run(_ask_and_decide(sess, {"decision": "allow"}))            # Bash still asks in acceptEdits
    rows = {r["tool"]: r for r in _rows(session_id=SID) if r["kind"] == "permission"}
    auto, human = rows["Edit"], rows["Bash"]
    assert (auto["decision"], auto["auto"], auto["auto_reason"], auto["actor"], auto["origin"]) == (
        "auto_allow", 1, "mode:acceptEdits", "", "system")
    assert (human["decision"], human["auto"], human["actor"], human["origin"]) == (
        "allow", 0, "bob@x", "owner")
    sess2 = _session()
    _run(sess2.set_mode("bypassPermissions", actor="bob@x"))
    _run(sess2._can_use_tool("Bash", {"command": "make deploy"}, None))
    last = _rows(session_id=SID)[-1]
    assert (last["decision"], last["auto_reason"], last["delay_ms"]) == (
        "auto_allow", "mode:bypassPermissions", 0)


def test_what_an_agent_policy_refuses_is_an_automatic_deny(world):
    sess = _session(policy={"agent": "nightly", "tools": ["Read"], "mcp": [], "secrets": []})
    res = _run(sess._can_use_tool("Bash", {"command": "curl example.org"}, None))
    assert isinstance(res, PermissionResultDeny)
    res = _run(sess._can_use_tool("AskUserQuestion", {"questions": []}, None))
    assert isinstance(res, PermissionResultDeny)
    a, b = _rows(session_id=SID)
    assert (a["decision"], a["auto"], a["auto_reason"], a["agent_run"]) == (
        "auto_deny", 1, "policy:tool_not_allowed", "nightly")
    assert (b["kind"], b["decision"], b["auto_reason"]) == ("question", "auto_deny",
                                                            "policy:unattended_question")


def test_a_cancelled_request_is_closed_not_left_pending(world):
    sess = _session()

    async def go():
        task = await _ask(sess)
        await sess.close()                    # the session is dropped while it waits
        return await task
    assert isinstance(_run(go()), PermissionResultDeny)
    (r,) = _rows(session_id=SID)
    assert r["decision"] == "cancelled" and r["actor"] == "" and r["origin"] == "system"


# ---- no secret in the journal -------------------------------------------------------------
def test_the_summary_masks_vault_values_and_pasted_credentials(world):
    sess = _session()
    sess.redact_values = {"DEPLOY_TOKEN": VAULT_SECRET}
    cmd = (f"curl -H 'Authorization: Bearer {VAULT_SECRET}' https://api.example.org "
           f"&& export OPENAI_KEY={PASTED_KEY} && mysql --password=Tr0ub4dor&3 "
           "&& git push https://deploy:s3cr3tpass@git.example.org/r.git")
    _run(_ask_and_decide(sess, {"decision": "deny", "message": f"no: {PASTED_KEY}"},
                         inp={"command": cmd}))
    (r,) = _rows(session_id=SID)
    blob = json.dumps(r)
    for secret in (VAULT_SECRET, PASTED_KEY, "Tr0ub4dor", "&3", "s3cr3tpass"):
        assert secret not in blob
    assert "[secret:DEPLOY_TOKEN]" in r["input_summary"]            # the vault's own masking
    assert "curl -H" in r["input_summary"] and "git.example.org" in r["input_summary"]
    import audit
    assert all(VAULT_SECRET not in json.dumps(e) and PASTED_KEY not in json.dumps(e)
               for e in audit.recent(100))


def test_scrub_patterns():
    import hitl_journal
    s = hitl_journal.scrub
    assert "abc123def456ghi789" not in s("token: abc123def456ghi789")
    assert "ghp_" not in s("ghp_0123456789abcdefghijABCDEFGHIJ")
    assert "eyJhbGciOiJIUzI1NiJ9" not in s("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.sig")
    assert "MIIEvQ" not in s("-----BEGIN PRIVATE KEY-----\nMIIEvQ\n-----END PRIVATE KEY-----")
    assert s("ls -la /srv/data") == "ls -la /srv/data"              # nothing to mask: unchanged


# ---- survives a restart -------------------------------------------------------------------
def test_the_journal_survives_the_death_of_the_process(world):
    """A request is written by ANOTHER process that then dies without deciding; this one
    reads it back from disk, and the start-up pass closes it as interrupted."""
    import audit
    import hitl_journal
    sess = _session()
    _run(_ask_and_decide(sess, {"decision": "allow"}))
    code = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(Path(audit.__file__).parent)!r})\n"
        "import audit, hitl_journal\n"
        "from pathlib import Path\n"
        f"audit.DB = Path({str(audit.DB)!r})\n"
        f"hitl_journal.requested('orphan-1', 'permission', session_id={SID!r}, tool='Write',\n"
        "                       title='/srv/app/config.yml', input_data={'file_path': '/srv/app/config.yml'})\n"
        "os._exit(9)\n")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "SOKKAN_DATA_DIR": str(world["tmp"])})
    assert p.returncode == 9, p.stderr
    # read with a plain connection: what is on disk, not what a module remembers
    con = sqlite3.connect(audit.DB)
    on_disk = con.execute("SELECT request_id, decision, tool FROM hitl ORDER BY id").fetchall()
    con.close()
    assert [r[1:] for r in on_disk] == [("allow", "Bash"), ("pending", "Write")]
    assert hitl_journal.mark_interrupted() == 1
    a, b = _rows(session_id=SID)
    assert a["decision"] == "allow"                                  # untouched
    assert (b["decision"], b["origin"], b["actor"]) == ("interrupted", "system", "")
    assert hitl_journal.mark_interrupted() == 0


def test_a_decision_is_written_once(world):
    import hitl_journal
    sess = _session()
    pid, _ = _run(_ask_and_decide(sess, {"decision": "allow"}))
    assert hitl_journal.decided(pid, "deny", actor="dave@x") is None
    assert _rows(session_id=SID)[0]["decision"] == "allow"


# ---- the paths a decision comes from ------------------------------------------------------
def _waiting(world, sid=SID, user="bob@x", pid="p-ws-1", tool="Bash", inp=None):
    """A live session registered in the API, waiting for one approval (as _can_use_tool
    leaves it), without a running event loop of its own."""
    import agentchat
    import hitl_journal
    sess = _session(sid, user)
    inp = inp or {"command": "make release"}
    fut = asyncio.new_event_loop().create_future()
    sess._perms[pid] = fut
    hitl_journal.requested(pid, "permission", session_id=sid, tool=tool,
                           title=agentchat._tool_title(tool, inp), input_data=inp, owner=user)
    sess.events.append({"type": "permission", "id": pid, "tool": tool,
                        "title": agentchat._tool_title(tool, inp), "input": inp})
    agentchat._registry[sid] = sess
    return sess, fut


def test_the_cockpit_websocket_names_the_owner(world):
    sess, fut = _waiting(world)
    c = world["as"]("bob@x")
    with c.websocket_connect(f"/api/agent/ws/{SID}") as ws:
        ws.send_json({"type": "permission", "id": "p-ws-1", "decision": "allow"})
        ws.send_json({"type": "mode", "mode": "acceptEdits"})
        for _ in range(50):
            if ws.receive_json().get("type") == "perm_mode":
                break
    assert fut.result()["decision"] == "allow"
    perm, mode = _rows(session_id=SID)
    assert (perm["decision"], perm["actor"], perm["origin"]) == ("allow", "bob@x", "owner")
    assert (mode["decision"], mode["actor"], mode["origin"]) == ("mode_change", "bob@x", "owner")
    import audit
    line = next(e for e in audit.recent(100) if e["action"] == "hitl.allow")
    assert line["user"] == "bob@x" and line["resource"] == f"session:{SID}" and "Bash" in line["detail"]


def test_another_dev_of_the_project_is_a_project_member_not_the_owner(world):
    _sess, _fut = _waiting(world)
    c = world["as"]("dave@x")
    with c.websocket_connect(f"/api/agent/ws/{SID}") as ws:
        ws.send_json({"type": "permission", "id": "p-ws-1", "decision": "deny", "message": "wait"})
        for _ in range(50):
            if ws.receive_json().get("type") == "permission_resolved":
                break
    (r,) = _rows(session_id=SID)
    assert (r["decision"], r["owner"], r["actor"], r["origin"]) == ("deny", "bob@x", "dave@x",
                                                                    "project_member")


def test_a_viewer_cannot_decide_and_nothing_is_written(world):
    _sess, fut = _waiting(world)
    c = world["as"]("carol@x")
    with c.websocket_connect(f"/api/agent/ws/{SID}") as ws:
        ws.send_json({"type": "permission", "id": "p-ws-1", "decision": "allow"})
        ws.send_json({"type": "user", "text": "hello"})
        for _ in range(50):
            if ws.receive_json().get("type") == "error":
                break
    assert not fut.done() and _rows(session_id=SID)[0]["decision"] == "pending"


def test_a_delegated_share_decision_names_the_delegate_and_keeps_its_audit_line(world):
    """No regression: the `share.hitl.*` line of the action journal is still written, and
    the validations journal now says the same decision came through a share."""
    _sess, fut = _waiting(world, pid="p-share-1")
    bob = world["as"]("bob@x")
    sid = bob.post("/api/shares", json={"kind": "session", "target": SID, "principal_kind": "user",
                                        "principal": "dave@x", "access": "write"}).json()["id"]
    assert bob.post(f"/api/shares/{sid}/approver", json={"approver": True}).status_code == 200
    r = world["as"]("dave@x").post(f"/api/shares/{sid}/decide",
                                   json={"permission_id": "p-share-1", "decision": "deny"})
    assert r.status_code == 200 and fut.result()["decision"] == "deny"
    (row,) = _rows(session_id=SID)
    assert (row["decision"], row["actor"], row["origin"], row["owner"]) == ("deny", "dave@x",
                                                                            "share", "bob@x")
    assert "dave@x" in row["message"]
    import audit
    actions = [e["action"] for e in audit.recent(200)]
    assert actions.count("share.hitl.deny") == 1          # as before
    assert "hitl.deny" not in actions                     # not written twice


def test_a_teams_decision_names_the_person_and_teams(world, monkeypatch):
    """teams.bot._decide_tool on a live session: the row says who, and that it came from Teams."""
    import agentchat
    import agents
    from teams import bot
    sess, fut = _waiting(world, sid=RADIO_SID, user="ray@x", pid="p-teams-1")
    monkeypatch.setattr(agents, "get_run", lambda rid: {"id": rid, "agent_id": 7,
                                                        "session_id": RADIO_SID})
    monkeypatch.setattr(agents, "get", lambda aid: {"id": aid, "name": "nightly",
                                                    "project": "radio", "owner": "ray@x"})
    monkeypatch.setattr(agents, "can_manage", lambda pu, a: True)
    assert agentchat.peek(RADIO_SID) is sess
    title, verdict = bot._decide_tool({"email": "ray@x", "role": "dev", "project": "radio"},
                                      {"ref": "3:p-teams-1", "project": "radio", "spec": "{}"},
                                      "approve", "ray@x")
    assert "allowed" in verdict and fut.result() == {"decision": "allow", "message": None}
    (r,) = _rows(session_id=RADIO_SID)
    assert (r["decision"], r["actor"], r["origin"], r["project"]) == ("allow", "ray@x", "teams",
                                                                      "radio")


# ---- export -------------------------------------------------------------------------------
def _seed_export(world):
    import hitl_journal
    s1 = _session(SID, "bob@x")
    _run(_ask_and_decide(s1, {"decision": "allow"}))
    _run(_ask_and_decide(s1, {"decision": "deny", "message": "no"}, actor="dave@x",
                         origin="project_member"))
    s2 = _session(RADIO_SID, "ray@x")
    _run(_ask_and_decide(s2, {"decision": "allow"}, actor="ray@x"))
    return hitl_journal


def test_export_json_and_csv_filtered_by_project_person_and_period(world):
    _seed_export(world)
    adm = world["as"]("admin@x")
    everything = adm.get("/api/hitl/journal").json()
    assert everything["count"] == 3
    e = everything["entries"][0]
    assert e["requested_at_iso"].endswith("Z") and e["decided_at_iso"] >= e["requested_at_iso"]
    assert e["auto"] is False and isinstance(e["delay_ms"], int)
    assert [x["project"] for x in adm.get("/api/hitl/journal?project=radio").json()["entries"]] == ["radio"]
    by_dave = adm.get("/api/hitl/journal?actor=dave@x").json()["entries"]
    assert [(x["actor"], x["decision"]) for x in by_dave] == [("dave@x", "deny")]
    # bob: the validations he gave AND the approvals asked on his sessions
    assert adm.get("/api/hitl/journal?actor=bob@x").json()["count"] == 2
    tomorrow = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert adm.get(f"/api/hitl/journal?since={tomorrow}").json()["count"] == 0
    assert adm.get(f"/api/hitl/journal?since={today}&until={tomorrow}").json()["count"] == 3
    assert adm.get("/api/hitl/journal?decision=deny").json()["count"] == 1
    assert adm.get("/api/hitl/journal?since=yesterday").status_code == 400
    r = adm.get("/api/hitl/journal?project=default&format=csv")
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [(x["actor"], x["decision"], x["origin"], x["tool"]) for x in rows] == [
        ("bob@x", "allow", "owner", "Bash"), ("dave@x", "deny", "project_member", "Bash")]
    assert {"session_id", "project", "owner", "requested_at_iso", "decided_at_iso", "delay_ms",
            "auto", "auto_reason", "edited", "input_summary"} <= set(rows[0])


def test_export_is_for_admins_of_the_instance_or_of_the_project(world):
    _seed_export(world)
    assert world["as"]("bob@x").get("/api/hitl/journal").status_code == 403       # a dev
    assert world["as"]("carol@x").get("/api/hitl/journal").status_code == 403     # a viewer
    rita = world["as"]("rita@x")                                                   # admin of radio
    assert rita.get("/api/hitl/journal?project=default").status_code == 403
    own = rita.get("/api/hitl/journal").json()["entries"]
    assert [x["project"] for x in own] == ["radio"]
    assert rita.get("/api/hitl/journal?project=radio").json()["count"] == 1
    import audit
    assert any(e["action"] == "hitl.journal.read" for e in audit.recent(50))
