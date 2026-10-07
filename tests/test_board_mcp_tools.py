"""3.2 — the board is drivable from a session (POC requirement: sessions act on,
change, move and close cards). Covers the board store (close ≠ delete, comments,
links to objects that exist, search, assignee), the sokkan-board MCP tools, the
identity they sign with (set by the API in the env, never chosen by the model),
the HITL policy (reads auto-approved, writes gated, agent runs follow their
policy) and the web routes the CardModal uses."""
import asyncio
import inspect

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agents
    import audit
    import board
    import iam
    import observability
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(observability, "DB", str(tmp_path / "incidents.db"))
    monkeypatch.setattr(observability, "DATA_DIR", str(tmp_path))
    for k in ("SOKKAN_SESSION_ID", "SOKKAN_SESSION_USER", "SOKKAN_AGENT_RUN",
              "SOKKAN_AGENT_NAME", "SOKKAN_AGENT_RUN_ID", "TMUX_PANE"):
        monkeypatch.delenv(k, raising=False)
    iam.upsert_user("dev@x.ch", "dev")
    iam.upsert_user("viewer@x.ch", "viewer")
    import board_mcp
    return board_mcp


def _as_session(monkeypatch, sid="a" * 32, user="dev@x.ch", tag="backend"):
    import board
    board.add_sdk_session(sid, tag, title="work")
    monkeypatch.setenv("SOKKAN_SESSION_ID", sid)
    monkeypatch.setenv("SOKKAN_SESSION_USER", user)
    return sid


# ---------- store ----------

def test_close_is_not_delete_and_reopen_brings_it_back(env):
    import board
    c = board.add_card("ship it", user="u")
    closed = board.close_card(c["id"], user="u", resolution="merged in #42")
    assert closed["bucket"] == "Done" and closed["closed_at"] and closed["closed_by"] == "u"
    assert board.get_card(c["id"]) is not None  # still there
    again = board.close_card(c["id"], user="other")
    assert again["closed_by"] == "u"  # closing twice is a no-op
    reo = board.reopen_card(c["id"], user="u", bucket="Doing", reason="regression")
    assert reo["bucket"] == "Doing" and reo["closed_at"] is None and reo["closed_by"] == ""
    acts = [e["action"] for e in board.card_events(c["id"])]
    assert acts[:2] == ["reopened", "closed"] and acts.count("closed") == 1
    with pytest.raises(ValueError):
        board.reopen_card(c["id"], bucket="Done")


def test_moving_to_done_closes_and_leaving_done_reopens(env):
    import board
    c = board.add_card("t")
    assert board.update_card(c["id"], user="u", bucket="Done")["closed_at"]
    assert board.update_card(c["id"], user="u", bucket="Review")["closed_at"] is None
    assert board.add_card("born done", bucket="Done", user="u")["closed_at"]


def test_archive_then_reopen_restores(env):
    import board
    c = board.add_card("old")
    a = board.archive_card(c["id"], user="u", reason="duplicate of #1")
    assert a["archived"] == 1
    assert all(x["id"] != c["id"] for col in board.list_cards().values() for x in col)
    assert board.reopen_card(c["id"], user="u")["archived"] == 0


def test_comments_are_kept_with_their_origin(env):
    import board
    c = board.add_card("t")
    o = {"session_id": "s1", "session_tag": "backend", "via": "mcp"}
    k = board.add_comment(c["id"], "first line\nmore", author="dev@x.ch", origin=o)
    assert k["author"] == "dev@x.ch" and k["session_id"] == "s1" and k["via"] == "mcp"
    ev = board.card_events(c["id"])[0]
    assert ev["action"] == "commented" and ev["detail"] == "first line"
    assert ev["session_id"] == "s1" and ev["session_tag"] == "backend"
    with pytest.raises(ValueError):
        board.add_comment(c["id"], "  ")
    assert board.add_comment(99999, "x") is None
    board.delete_card(c["id"])
    assert board.card_comments(c["id"]) == []


def test_links_only_to_objects_that_exist(env):
    import agents
    import board
    import observability
    c = board.add_card("t")
    board.add_sdk_session("s" * 32, "infra", title="incident work")
    a = agents.create({"email": "dev@x.ch", "role": "dev"}, dict(
        name="nightly", purpose="Check things every night carefully.",
        deliverable="A short report on the checks.", trigger="manual"))
    run = agents.enqueue_run(a["id"], "manual", requested_by="dev@x.ch")
    iid = observability.record_incident("API 5xx", "spike")
    for kind, ref in (("session", "s" * 32), ("agent", "nightly"), ("run", run["id"]),
                      ("incident", iid)):
        out = board.link_card(c["id"], kind, ref, user="u")
        assert out["added"], (kind, out)
    assert board.link_card(c["id"], "agent", a["id"], user="u")["added"] is False  # same agent
    for kind, ref in (("session", "nope"), ("agent", "ghost"), ("run", 999), ("incident", 999)):
        with pytest.raises(ValueError):
            board.link_card(c["id"], kind, ref)
    with pytest.raises(ValueError):
        board.link_card(c["id"], "pr", "1")
    links = {li["kind"]: li for li in board.card_links(c["id"])}
    assert links["agent"]["label"] == "nightly" and links["agent"]["href"] == f"/?tab=crew&agent={a['id']}"
    assert links["run"]["href"] == f"/?tab=crew&agent={a['id']}&run={run['id']}"
    assert links["incident"]["href"] == f"/?tab=operate&incident={iid}"
    assert board.link_card(c["id"], "agent", "nightly", remove=True)["removed"] is True
    assert "agent" not in {li["kind"] for li in board.card_links(c["id"])}


def test_spawned_session_counts_as_a_link_and_a_vanished_one_is_flagged(env):
    import board
    c = board.add_card("t")
    board.update_card(c["id"], session_id="gone" * 8)
    li = board.card_links(c["id"])
    assert li[0]["kind"] == "session" and li[0]["missing"] is True


def test_search_text_comments_and_filters(env):
    import board
    a = board.add_card("Fix login redirect", description="OIDC callback loops", tag="auth")
    b = board.add_card("Pricing page", tag="frontend", bucket="Review")
    board.add_comment(b["id"], "the Stripe webhook is flaky", author="u")
    board.update_card(a["id"], assignee="dev@x.ch")
    ids = lambda **kw: [c["id"] for c in board.search_cards(**kw)]  # noqa: E731
    assert ids(query="oidc") == [a["id"]]
    assert ids(query="stripe webhook") == [b["id"]]  # found through a comment
    assert ids(tag="frontend") == [b["id"]] and ids(bucket="Review") == [b["id"]]
    assert ids(assignee="DEV@x.ch") == [a["id"]] and ids(assignee="none") == [b["id"]]
    board.archive_card(b["id"])
    assert ids(query="stripe") == [] and ids(query="stripe", include_archived=True) == [b["id"]]


def test_assignee_must_exist(env):
    import agents
    import board
    agents.create({"email": "dev@x.ch", "role": "dev"}, dict(
        name="triage", purpose="Triage the new issues every morning.",
        deliverable="A list of triaged issues.", trigger="manual"))
    assert board.validate_assignee("Dev@X.ch") == "dev@x.ch"
    assert board.validate_assignee("agent:triage") == "agent:triage"
    assert board.validate_assignee("") == ""
    for bad in ("nobody@x.ch", "agent:ghost"):
        with pytest.raises(ValueError):
            board.validate_assignee(bad)


# ---------- MCP tools ----------

def test_tools_sign_with_the_session_identity(env, monkeypatch):
    import audit
    import board
    sid = _as_session(monkeypatch)
    m = env
    c = m.create_card("Write the runbook", tag="docs")
    m.update_card(c["id"], title="Write the DR runbook", priority=0, due="2026-11-01",
                  assignee="dev@x.ch")
    m.move_card(c["id"], "Doing")
    m.comment_card(c["id"], "draft pushed")
    m.link_card(c["id"], "session", "self")
    m.close_card(c["id"], resolution="published")
    d = m.get_card(c["id"])
    assert d["title"] == "Write the DR runbook" and d["priority"] == 0 and d["due"] == "2026-11-01"
    assert d["assignee"] == "dev@x.ch" and d["bucket"] == "Done" and d["closed_by"] == "dev@x.ch"
    assert d["comments"][0]["author"] == "dev@x.ch" and d["comments"][0]["session_id"] == sid
    assert any(li["kind"] == "session" and li["ref"] == sid for li in d["links"])
    for ev in d["events"]:
        assert ev["user"] == "dev@x.ch" and ev["session_id"] == sid, ev
        assert ev["session_tag"] == "backend" and ev["via"] == "mcp"
    assert {e["action"] for e in d["events"]} >= {
        "created", "edited", "assigned", "moved", "commented", "linked", "closed"}
    acts = {e["action"] for e in audit.recent(50)}
    assert {"board.card.update", "board.card.comment", "board.card.close",
            "board.card.link"} <= acts
    r = m.reopen_card(c["id"], bucket="Review", reason="typo found")
    assert r["bucket"] == "Review"
    assert m.archive_card(c["id"], reason="superseded")["archived"] == 1
    assert m.search_cards(query="runbook")["count"] == 0
    assert m.search_cards(query="runbook", include_archived=True)["cards"][0]["id"] == c["id"]
    assert board.get_card(c["id"]) is not None


def test_no_write_tool_lets_the_model_choose_the_author(env):
    for name in ("create_card", "move_card", "update_card", "close_card", "reopen_card",
                 "archive_card", "comment_card", "link_card"):
        params = set(inspect.signature(getattr(env, name)).parameters)
        assert not params & {"user", "author", "actor", "who", "session_id", "origin"}, name


def test_agent_run_signs_as_the_agent(env, monkeypatch):
    sid = _as_session(monkeypatch, sid="r" * 32, user="dev@x.ch", tag="agent")
    monkeypatch.setenv("SOKKAN_AGENT_RUN", "1")
    monkeypatch.setenv("SOKKAN_AGENT_NAME", "nightly")
    monkeypatch.setenv("SOKKAN_AGENT_RUN_ID", "7")
    c = env.create_card("from the agent")
    k = env.comment_card(c["id"], "run report")
    assert k["author"] == "agent:nightly" and k["via"] == "agent-run #7" and k["session_id"] == sid


def test_viewer_session_cannot_write_but_can_read(env, monkeypatch):
    import board
    c = board.add_card("t")
    _as_session(monkeypatch, user="viewer@x.ch")
    for call in (lambda: env.comment_card(c["id"], "x"), lambda: env.close_card(c["id"]),
                 lambda: env.update_card(c["id"], title="y"), lambda: env.move_card(c["id"], "Doing"),
                 lambda: env.create_card("z"), lambda: env.archive_card(c["id"])):
        assert "read-only" in call()["error"]
    assert env.get_card(c["id"])["id"] == c["id"] and env.search_cards()["count"] == 1


def test_validation_errors_are_returned_not_raised(env, monkeypatch):
    import board
    _as_session(monkeypatch)
    c = board.add_card("t")
    assert "tag" in env.update_card(c["id"], tag="nope")["error"]
    assert "priority" in env.update_card(c["id"], priority=9)["error"]
    assert "YYYY" in env.update_card(c["id"], due="tomorrow")["error"]
    assert "unknown user" in env.update_card(c["id"], assignee="x@y.z")["error"]
    assert "nothing" in env.update_card(c["id"])["error"]
    assert "not found" in env.get_card(999)["error"]
    assert "not found" in env.close_card(999)["error"]
    assert "not found" in env.link_card(c["id"], "incident", "999")["error"]
    assert "bucket" in env.search_cards(bucket="Later")["error"]
    assert "empty" in env.comment_card(c["id"], " ")["error"]


# ---------- HITL policy ----------

def test_reads_are_auto_approved_writes_are_gated():
    import agentchat
    reads = {"get_card", "search_cards", "list_board", "list_tags"}
    writes = {"create_card", "move_card", "update_card", "close_card", "reopen_card",
              "archive_card", "comment_card", "link_card", "open_preview"}
    for t in reads:
        assert f"mcp__sokkan-board__{t}" in agentchat.SAFE_TOOLS
    for t in writes:
        assert f"mcp__sokkan-board__{t}" not in agentchat.SAFE_TOOLS


def test_agent_run_follows_its_policy():
    import agentchat
    granted = agentchat.AgentSession("x" * 32, policy={
        "agent": "a", "run": 1, "tools": ["Read", "mcp__sokkan-board__comment_card"], "mcp": ["sokkan-memory", "sokkan-board"],
        "auto_approve": ["mcp__sokkan-board__comment_card"]})
    allowed = granted._allowed_tools()
    assert "mcp__sokkan-board__get_card" in allowed and "mcp__sokkan-board__comment_card" in allowed
    assert "mcp__sokkan-board__close_card" not in allowed  # permitted, but asks a human
    assert granted._tool_permitted("mcp__sokkan-board__close_card")
    denied = agentchat.AgentSession("y" * 32, policy={
        "agent": "b", "run": 2, "tools": ["Read"], "mcp": ["sokkan-memory"]})
    assert "mcp__sokkan-board__get_card" not in denied._allowed_tools()
    res = asyncio.new_event_loop().run_until_complete(
        denied._can_use_tool("mcp__sokkan-board__close_card", {"card_id": 1}, None))
    assert "not in this agent's allowed tools" in res.message
    servers = agentchat.mcp_servers_for("y" * 32, "dev@x.ch", only=["sokkan-memory"],
                                        agent_run={"agent": "b", "run": 2})
    assert "sokkan-board" not in servers


# ---------- web routes used by the CardModal ----------

def test_web_routes_comment_close_reopen_and_detail(env, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import board
    import iam
    who = {"email": "dev@x.ch"}

    def fake_user(request=None):
        u = iam.get_user(who["email"])
        return {"email": u["email"], "role": u["role"], "name": u["name"]}

    monkeypatch.setattr(a.auth, "current_user", fake_user)
    a.app.dependency_overrides[a.current_user] = fake_user
    client = TestClient(a.app)
    c = board.add_card("t")
    try:
        _web_flow(client, c, who)
    finally:
        a.app.dependency_overrides.clear()


def _web_flow(client, c, who):
    r = client.post(f"/api/board/card/{c['id']}/comment", json={"body": "LGTM"})
    assert r.status_code == 200, r.text
    assert r.json()["author"] == "dev@x.ch"
    assert client.post(f"/api/board/card/{c['id']}/close", json={"resolution": "ok"}).json()["bucket"] == "Done"
    assert client.post(f"/api/board/card/{c['id']}/reopen", json={"bucket": "Done"}).status_code == 400
    assert client.post(f"/api/board/card/{c['id']}/reopen", json={"bucket": "Doing"}).json()["bucket"] == "Doing"
    assert client.patch(f"/api/board/card/{c['id']}", json={"assignee": "ghost@x.ch"}).status_code == 400
    d = client.get(f"/api/board/card/{c['id']}").json()
    assert d["comments"][0]["body"] == "LGTM" and d["comments"][0]["via"] == "web"
    assert {"commented", "closed", "reopened"} <= {e["action"] for e in d["events"]}
    assert all(e["via"] == "web" for e in d["events"] if e["action"] != "created")
    assert client.post("/api/board/card/999/comment", json={"body": "x"}).status_code == 404
    who["email"] = "viewer@x.ch"
    assert client.post(f"/api/board/card/{c['id']}/comment", json={"body": "x"}).status_code == 403
    assert client.post(f"/api/board/card/{c['id']}/close", json={}).status_code == 403
