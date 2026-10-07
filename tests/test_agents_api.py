"""/api/agents + the sokkan-agents MCP server (proposal → human approval)."""
import os
import tempfile

import pytest

pytestmark = pytest.mark.usefixtures("model_credentials")  # 3.2: a configured instance

_TMP = tempfile.mkdtemp()
os.environ.update(
    SOKKAN_DATA_DIR=_TMP, CLAUDE_CONFIG_DIR=f"{_TMP}/claude",
    SOKKAN_MEMORY_DIR=f"{_TMP}/memory", SOKKAN_AGENT_CWD=_TMP,
    SOKKAN_LOCAL_TOKEN="", SOKKAN_OWNER_EMAIL="owner@localhost",
    SOKKAN_UPDATE_CHECK="0",
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import agents
    import app as a
    import audit
    import board
    import iam
    import vault

    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    iam.upsert_user("dev@x.ch", "dev")
    iam.upsert_user("other@x.ch", "dev")
    iam.upsert_user("viewer@x.ch", "viewer")
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    vault.set_secret("GITHUB_TOKEN", "ghp_value_never_shown_123456")
    who = {"email": "dev@x.ch"}

    def fake_user(request=None):
        u = iam.get_user(who["email"])
        return {"email": u["email"], "role": u["role"], "name": u["name"]}

    monkeypatch.setattr(a.auth, "current_user", fake_user)
    a.app.dependency_overrides[a.current_user] = fake_user
    c = TestClient(a.app)
    c.who = who
    yield c
    a.app.dependency_overrides.clear()


AGENT = {"name": "nightly-cve-audit", "purpose": "Audit npm deps for CVEs",
         "deliverable": "Table of vulnerable packages", "trigger": "cron",
         "schedule": "0 2 * * *", "secrets": ["GITHUB_TOKEN"]}


def test_form_create_activate_and_deck(client):
    r = client.post("/api/agents", json={**AGENT, "activate": True})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["status"] == "active" and a["deck"] == "armed" and a["owner"] == "dev@x.ch"
    lst = client.get("/api/agents").json()
    assert [x["name"] for x in lst["agents"]] == ["nightly-cve-audit"]
    meta = client.get("/api/agents/meta").json()
    assert meta["secrets"] == ["GITHUB_TOKEN"]
    assert "ghp_" not in r.text + str(lst) + str(meta)


def test_validation_is_a_400_with_a_message(client):
    r = client.post("/api/agents", json={**AGENT, "secrets": ["NOPE"]})
    assert r.status_code == 400 and "not in the vault" in r.json()["detail"]
    r = client.post("/api/agents", json={**AGENT, "schedule": "tonight"})
    assert r.status_code == 400 and "5 fields" in r.json()["detail"]


def test_actions_and_runs(client):
    aid = client.post("/api/agents", json={**AGENT, "activate": True}).json()["id"]
    r = client.post(f"/api/agents/{aid}/run")
    assert r.status_code == 200 and r.json()["run"]["status"] == "queued"
    assert client.post(f"/api/agents/{aid}/run").status_code == 400  # one at a time
    runs = client.get(f"/api/agents/{aid}/runs").json()
    assert len(runs) == 1
    rid = runs[0]["id"]
    assert client.get(f"/api/agents/runs/{rid}").json()["agent_name"] == "nightly-cve-audit"
    assert client.post(f"/api/agents/runs/{rid}/cancel").json()["ok"] is True
    assert client.post(f"/api/agents/{aid}/pause").json()["agent"]["status"] == "paused"
    assert client.post(f"/api/agents/{aid}/resume").json()["agent"]["status"] == "active"
    p = client.patch(f"/api/agents/{aid}", json={"schedule": "30 3 * * *", "model": "haiku"})
    assert p.status_code == 200 and p.json()["schedule"] == "30 3 * * *"
    assert client.post(f"/api/agents/{aid}/archive").json()["agent"]["status"] == "archived"
    assert client.post(f"/api/agents/{aid}/nope").status_code == 404


def test_owner_isolation_and_viewer_blocked(client):
    aid = client.post("/api/agents", json=AGENT).json()["id"]
    client.who["email"] = "other@x.ch"
    assert client.get(f"/api/agents/{aid}").status_code == 404
    assert client.post(f"/api/agents/{aid}/approve").status_code == 404
    assert client.get("/api/agents").json()["agents"] == []
    client.who["email"] = "owner@localhost"  # admin+ sees everything
    assert client.get(f"/api/agents/{aid}").status_code == 200
    client.who["email"] = "viewer@x.ch"
    assert client.get("/api/agents").status_code == 403


def test_nina_proposal_waits_for_approval(client):
    r = client.post("/api/agents/proposals", json={**AGENT, "name": "from-nina"})
    a = r.json()
    assert a["status"] == "pending" and a["needs_approval"] and a["created_by"].startswith("nina:")
    pend = client.get("/api/agents").json()["pending"]
    assert [x["name"] for x in pend["agents"]] == ["from-nina"]
    assert client.post(f"/api/agents/{a['id']}/approve").json()["agent"]["status"] == "active"


# ---- MCP server (called in-process: same functions the stdio server exposes) ----
@pytest.fixture()
def mcp(client, monkeypatch):
    monkeypatch.setenv("SOKKAN_SESSION_ID", "sess-123")
    monkeypatch.setenv("SOKKAN_SESSION_USER", "dev@x.ch")
    monkeypatch.delenv("SOKKAN_AGENT_RUN", raising=False)
    import agents_mcp
    return agents_mcp


def test_mcp_create_is_a_pending_proposal_owned_by_the_human(mcp, client):
    out = mcp.create_agent(name="log-triage", purpose="Sort yesterday's error logs",
                           deliverable="Top 10 errors with counts", trigger="cron",
                           schedule="0 7 * * *", secrets=["GITHUB_TOKEN"])
    assert out["ok"], out
    a = out["agent"]
    assert a["status"] == "pending" and a["owner"] == "dev@x.ch"
    assert a["created_by"] == "session:sess-123" and "approve" in out["next_step"]
    assert a["next_run_at"] is None  # never scheduled before approval
    # the human approves in Crew
    r = client.post(f"/api/agents/{a['id']}/approve")
    assert r.json()["agent"]["deck"] == "armed"


def test_mcp_refuses_secret_values_and_unknown_names(mcp):
    out = mcp.create_agent(name="x-agent", purpose="p", deliverable="d",
                           secrets=["ghp_abcdefghijklmnopqrstuvwxyz1234"])
    assert not out["ok"] and "NAMES only" in out["error"]
    out = mcp.create_agent(name="x-agent", purpose="p", deliverable="d", secrets=["MISSING"])
    assert not out["ok"] and "not in the vault" in out["error"]


def test_mcp_update_on_active_agent_is_pending_change(mcp, client):
    aid = client.post("/api/agents", json={**AGENT, "activate": True}).json()["id"]
    out = mcp.update_agent("nightly-cve-audit", {"schedule": "0 4 * * *"})
    assert out["ok"] and out["agent"]["pending_change"] == {"schedule": "0 4 * * *"}
    assert out["agent"]["schedule"] == "0 2 * * *"
    got = mcp.get_agent(str(aid))
    assert got["status"] == "active" and got["crew_status_hint"].startswith("active")


def test_mcp_run_now_list_runs_get_run_pause(mcp, client):
    client.post("/api/agents", json={**AGENT, "activate": True})
    out = mcp.run_agent_now("nightly-cve-audit")
    assert out["ok"] and out["run"]["trigger"] == "session"
    assert out["run"]["requested_by"] == "session:sess-123"
    runs = mcp.list_runs("nightly-cve-audit")
    assert len(runs) == 1 and mcp.get_run(runs[0]["id"])["status"] == "queued"
    assert mcp.pause_agent("nightly-cve-audit")["agent"]["status"] == "paused"
    assert mcp.resume_agent("nightly-cve-audit")["agent"]["status"] == "active"
    assert len(mcp.list_agents()) == 1


def test_mcp_is_read_only_inside_an_agent_run(mcp, client, monkeypatch):
    client.post("/api/agents", json={**AGENT, "activate": True})
    monkeypatch.setenv("SOKKAN_AGENT_RUN", "1")
    for out in (mcp.create_agent(name="breed", purpose="p", deliverable="d"),
                mcp.run_agent_now("nightly-cve-audit"),
                mcp.update_agent("nightly-cve-audit", {"model": "opus"}),
                mcp.archive_agent("nightly-cve-audit")):
        assert out["ok"] is False and "read-only" in out["error"]
    assert len(mcp.list_agents()) == 1  # reads still work


def test_mcp_other_user_cannot_see_or_touch(mcp, client, monkeypatch):
    client.post("/api/agents", json={**AGENT, "activate": True})
    monkeypatch.setenv("SOKKAN_SESSION_USER", "other@x.ch")
    assert mcp.list_agents() == []
    assert mcp.get_agent("nightly-cve-audit")["ok"] is False
    assert mcp.run_agent_now("nightly-cve-audit")["ok"] is False


def test_mcp_servers_get_the_caller_identity(tmp_path, monkeypatch):
    import agentchat
    import board

    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)

    servers = agentchat.mcp_servers_for("sid-1", "dev@x.ch")
    assert set(servers) >= {"sokkan-memory", "sokkan-board", "sokkan-agents"}
    # 3.2: + the session's project (a session SOKKAN does not know → the default project
    # while the instance has only that one)
    assert servers["sokkan-agents"]["env"] == {"SOKKAN_SESSION_ID": "sid-1",
                                               "SOKKAN_SESSION_USER": "dev@x.ch",
                                               "SOKKAN_SESSION_PROJECT": "default",
                                               "SOKKAN_SESSION_SCOPE": "default,shared"}
    run = agentchat.mcp_servers_for("sid-2", "dev@x.ch", only=["sokkan-memory",
                                                                "sokkan-agents"], agent_run=True)
    assert set(run) == {"sokkan-memory", "sokkan-agents"}
    assert run["sokkan-agents"]["env"]["SOKKAN_AGENT_RUN"] == "1"


def test_session_policy_denies_tools_outside_the_list():
    import asyncio

    import agentchat
    from claude_agent_sdk import PermissionResultDeny

    s = agentchat.AgentSession("sid", cwd="/tmp", policy={
        "tools": ["Read", "Bash"], "auto_approve": ["Bash(npm audit:*)"],
        "mcp": ["sokkan-memory"]})
    loop = asyncio.new_event_loop()
    res = loop.run_until_complete(s._can_use_tool("Write", {"file_path": "/x"}, None))
    assert isinstance(res, PermissionResultDeny) and "not in this agent" in res.message
    res = loop.run_until_complete(s._can_use_tool("mcp__sokkan-board__create_card", {}, None))
    assert isinstance(res, PermissionResultDeny)
    res = loop.run_until_complete(s._can_use_tool("AskUserQuestion", {"questions": []}, None))
    assert isinstance(res, PermissionResultDeny) and "unattended" in res.message
    allowed = s._allowed_tools()
    assert "Read" in allowed and "Bash(npm audit:*)" in allowed
    assert "mcp__sokkan-memory__memory_search" in allowed
    assert "mcp__sokkan-board__list_board" not in allowed  # board server not granted
    assert "Glob" not in allowed  # safe, but not in this agent's tools
    dis = s._disallowed_tools()
    assert "Write" in dis and "Glob" in dis and "Bash" not in dis and "Read" not in dis
