"""3.1.1 — SOKKAN_CREW_VIEWER_READONLY: a viewer sees Crew, changes nothing."""
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp()
os.environ.update(
    SOKKAN_DATA_DIR=_TMP, CLAUDE_CONFIG_DIR=f"{_TMP}/claude",
    SOKKAN_MEMORY_DIR=f"{_TMP}/memory", SOKKAN_AGENT_CWD=_TMP,
    SOKKAN_LOCAL_TOKEN="", SOKKAN_OWNER_EMAIL="owner@localhost",
    SOKKAN_UPDATE_CHECK="0",
)

SECRET_VALUE = "ghp_value_never_shown_123456"
AGENT = {"name": "nightly-cve-audit", "purpose": "Audit npm deps for CVEs",
         "deliverable": "Table of vulnerable packages", "trigger": "cron",
         "schedule": "0 2 * * *", "secrets": ["GITHUB_TOKEN"]}


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
    monkeypatch.setenv("SOKKAN_MEMORY_QUARANTINE_DIR", str(tmp_path / "quarantine"))
    vault.set_secret("GITHUB_TOKEN", SECRET_VALUE)
    who = {"email": "dev@x.ch"}

    def fake_user(request=None):
        u = iam.get_user(who["email"])
        return {"email": u["email"], "role": u["role"], "name": u["name"]}

    monkeypatch.setattr(a.auth, "current_user", fake_user)
    a.app.dependency_overrides[a.current_user] = fake_user
    c = TestClient(a.app)
    c.who = who
    # one active agent with a finished run, one proposal waiting
    aid = c.post("/api/agents", json={**AGENT, "activate": True}).json()["id"]
    run = agents.enqueue_run(aid, "manual", "dev@x.ch")
    agents.update_run(run["id"], status="succeeded", deliverable="All clean.\nDELIVERY: done")
    pid = c.post("/api/agents/proposals", json={**AGENT, "name": "pr-review",
                                                "purpose": "Review PRs",
                                                "deliverable": "Draft reviews"}).json()["id"]
    c.ids = {"agent": aid, "run": run["id"], "proposal": pid}
    yield c
    a.app.dependency_overrides.clear()


def _writes(ids):
    aid, rid, pid = ids["agent"], ids["run"], ids["proposal"]
    out = [("post", "/api/agents", {**AGENT, "name": "sneaky", "activate": True}),
           ("post", "/api/agents/proposals", {**AGENT, "name": "sneaky2"}),
           ("patch", f"/api/agents/{aid}", {"purpose": "changed"}),
           ("post", f"/api/agents/runs/{rid}/cancel", None)]
    for action in ("approve", "reject", "pause", "resume", "archive", "run"):
        out.append(("post", f"/api/agents/{aid}/{action}", None))
        out.append(("post", f"/api/agents/{pid}/{action}", None))
    return out


def test_flag_off_viewer_sees_nothing(client, monkeypatch):
    monkeypatch.delenv("SOKKAN_CREW_VIEWER_READONLY", raising=False)
    client.who["email"] = "viewer@x.ch"
    for url in ("/api/agents", "/api/agents/meta", f"/api/agents/{client.ids['agent']}",
                f"/api/agents/{client.ids['agent']}/runs", f"/api/agents/runs/{client.ids['run']}"):
        assert client.get(url).status_code == 403, url
    assert client.get("/api/features").json()["agents_viewer_readonly"] is False


def test_flag_on_viewer_reads_everything(client, monkeypatch):
    monkeypatch.setenv("SOKKAN_CREW_VIEWER_READONLY", "1")
    client.who["email"] = "viewer@x.ch"
    lst = client.get("/api/agents")
    assert lst.status_code == 200
    names = {a["name"]: a for a in lst.json()["agents"]}
    assert set(names) == {"nightly-cve-audit", "pr-review"}
    prop = names["pr-review"]
    assert prop["needs_approval"] and prop["approval"]["can_approve"] is False
    assert prop["approval"]["reason"] == "read-only"
    aid = client.ids["agent"]
    assert client.get(f"/api/agents/{aid}").json()["secrets"] == ["GITHUB_TOKEN"]
    runs = client.get(f"/api/agents/{aid}/runs").json()
    assert runs[0]["deliverable"].startswith("All clean")
    assert client.get(f"/api/agents/runs/{client.ids['run']}").status_code == 200
    meta = client.get("/api/agents/meta").json()
    assert meta["secrets"] == ["GITHUB_TOKEN"]  # names: yes
    assert meta["read_only"] is True and meta["self_activation"] is False
    blob = str(lst.json()) + str(meta) + str(runs)
    assert SECRET_VALUE not in blob  # values: never
    assert client.get("/api/features").json()["agents_viewer_readonly"] is True


def test_flag_on_every_write_is_403_for_a_viewer(client, monkeypatch):
    monkeypatch.setenv("SOKKAN_CREW_VIEWER_READONLY", "1")
    client.who["email"] = "viewer@x.ch"
    before = client.get(f"/api/agents/{client.ids['agent']}").json()
    for method, url, body in _writes(client.ids):
        r = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        assert r.status_code == 403, (method, url, r.status_code, r.text)
    after = client.get(f"/api/agents/{client.ids['agent']}").json()
    assert after["purpose"] == before["purpose"] and after["status"] == "active"
    assert len(client.get(f"/api/agents/{client.ids['agent']}/runs").json()) == 1


def test_flag_on_no_secret_value_and_no_quarantine_for_a_viewer(client, monkeypatch):
    monkeypatch.setenv("SOKKAN_CREW_VIEWER_READONLY", "1")
    import quarantine
    quarantine.write("agent-x-latest", "desc", "body of a quarantined note", {"agent": "x"})
    client.who["email"] = "viewer@x.ch"
    assert client.get("/api/vault").status_code == 403
    assert client.get("/api/vault/session").status_code == 403
    assert client.get("/api/memory/quarantine").status_code == 403
    assert client.get("/api/memory/quarantine/agent-x-latest").status_code == 403
    assert client.post("/api/memory/quarantine/agent-x-latest/approve").status_code == 403
    assert client.post("/api/memory/quarantine/agent-x-latest/reject").status_code == 403


def test_flag_on_a_dev_reads_others_agents_but_cannot_touch_them(client, monkeypatch):
    monkeypatch.setenv("SOKKAN_CREW_VIEWER_READONLY", "1")
    client.who["email"] = "other@x.ch"
    aid = client.ids["agent"]
    assert client.get(f"/api/agents/{aid}").status_code == 200
    assert client.patch(f"/api/agents/{aid}", json={"purpose": "x"}).status_code == 403
    assert client.post(f"/api/agents/{aid}/pause").status_code == 403
    assert client.post(f"/api/agents/runs/{client.ids['run']}/cancel").status_code == 403
    # their own agents stay fully theirs
    r = client.post("/api/agents", json={**AGENT, "name": "mine", "secrets": []})
    assert r.status_code == 200 and client.post(f"/api/agents/{r.json()['id']}/archive").status_code == 200


def test_mcp_cannot_write_on_a_read_only_agent(client, monkeypatch):
    monkeypatch.setenv("SOKKAN_CREW_VIEWER_READONLY", "1")
    monkeypatch.setenv("SOKKAN_SESSION_ID", "sess-9")
    monkeypatch.setenv("SOKKAN_SESSION_USER", "other@x.ch")
    monkeypatch.delenv("SOKKAN_AGENT_RUN", raising=False)
    import agents_mcp
    assert "error" not in agents_mcp.get_agent("nightly-cve-audit")
    assert "error" in agents_mcp.pause_agent("nightly-cve-audit")
    assert "error" in agents_mcp.update_agent("nightly-cve-audit", {"purpose": "x"})


def test_ops_feed_links_incidents_and_agent_runs(client, monkeypatch, tmp_path):
    import agents
    import observability
    monkeypatch.setattr(observability, "DB", str(tmp_path / "incidents.db"))
    rid = observability.record_incident("StagingDown", "502", "critical")
    run = agents.enqueue_run(client.ids["agent"], "event", "event:alert",
                             context={"incident": rid})
    iid, _ = observability.agent_incident(client.ids["agent"], "nightly-cve-audit",
                                          client.ids["run"], "failed", "boom")
    feed = {i["id"]: i for i in client.get("/api/observability").json()["incidents"]}
    assert [r["id"] for r in feed[rid]["agent_runs"]] == [run["id"]]
    assert feed[iid]["agent_visible"] is True and feed[iid]["runs"] == [client.ids["run"]]
    client.who["email"] = "viewer@x.ch"
    monkeypatch.delenv("SOKKAN_CREW_VIEWER_READONLY", raising=False)
    feed = {i["id"]: i for i in client.get("/api/observability").json()["incidents"]}
    assert feed[rid]["agent_runs"] == [] and feed[iid]["agent_visible"] is False
    monkeypatch.setenv("SOKKAN_CREW_VIEWER_READONLY", "1")
    feed = {i["id"]: i for i in client.get("/api/observability").json()["incidents"]}
    assert feed[rid]["agent_runs"][0]["agent_name"] == "nightly-cve-audit"
    assert feed[iid]["agent_visible"] is True
    # the viewer still cannot change an incident
    assert client.post(f"/api/observability/incident/{iid}", json={"status": "resolved"}).status_code == 403
