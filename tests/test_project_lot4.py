"""SOKKAN 3.2 lot 4 (feature `project_vault_budgets`) — vault, budgets, agent names,
CortHeXis review and journal per project.

Same world as test_project_isolation.py (real middleware → projectgate → route), plus
carol@x, `maintainer` of `radio` (= its admin). Each scope filter has a test that goes red
without it (checked by mutation: vault namespace, vault routes, usage totals, project
budget stop, agent names per project, assignee lookup, MCP resolve, review corpus,
proposals, journal)."""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from test_project_isolation import DEFAULT_SID, RADIO_SID, world  # noqa: F401 — fixture


@pytest.fixture()
def lot4(world, monkeypatch):  # noqa: F811
    import projects

    monkeypatch.setenv("SOKKAN_FEATURE_MULTI_PROJECT", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_PROJECT_VAULT_BUDGETS", "1")
    projects.grant("radio", "user", "carol@x", "maintainer")
    return world


def _off(monkeypatch):
    monkeypatch.setenv("SOKKAN_FEATURE_PROJECT_VAULT_BUDGETS", "0")


# ---- vault ------------------------------------------------------------------------------

def test_vault_is_per_project(lot4):
    import vault

    carol = lot4["as"]("carol@x", "radio")
    r = carol.post("/api/vault", json={"name": "RADIO_TOKEN", "value": "radio-value"})
    assert r.status_code == 200 and r.json()["names"] == ["RADIO_TOKEN"]
    # the same name in two projects = two secrets
    assert carol.post("/api/vault", json={"name": "DEFAULT_DB_PASSWORD",
                                          "value": "radio-db"}).status_code == 200
    assert carol.get("/api/vault").json()["names"] == ["DEFAULT_DB_PASSWORD", "RADIO_TOKEN"]
    bob_admin = lot4["as"]("admin@x")
    assert bob_admin.get("/api/vault").json()["names"] == ["DEFAULT_DB_PASSWORD"]
    alice = lot4["as"]("alice@x", "radio")
    assert alice.get("/api/vault/session").json()["names"] == ["DEFAULT_DB_PASSWORD", "RADIO_TOKEN"]
    assert alice.get("/api/vault").status_code == 403          # dev: names for sessions only
    assert lot4["as"]("bob@x").get("/api/vault/session").json()["names"] == ["DEFAULT_DB_PASSWORD"]
    # values: each project its own
    assert vault.session_env(None, project="radio") == {"RADIO_TOKEN": "radio-value",
                                                        "DEFAULT_DB_PASSWORD": "radio-db"}
    assert vault.session_env(None, project="default") == {
        "DEFAULT_DB_PASSWORD": "s3cret-of-the-default-team"}
    assert vault.session_env(["RADIO_TOKEN"], project="default") == {}
    # deleting in radio leaves default alone
    carol = lot4["as"]("carol@x", "radio")
    assert carol.delete("/api/vault/DEFAULT_DB_PASSWORD").status_code == 200
    assert vault.names("default") == ["DEFAULT_DB_PASSWORD"]


def test_vault_shared_and_off_get_nothing(lot4, monkeypatch):
    import vault

    with pytest.raises(ValueError):
        vault.set_secret("X", "y", project="shared")
    assert vault.names("shared") == [] and vault.session_env(None, project="shared") == {}
    assert vault.session_env(None, project="") == {}
    vault.set_secret("RADIO_TOKEN", "v", project="radio")
    _off(monkeypatch)                                           # lot 3: radio gets nothing
    assert vault.session_env(None, project="radio") == {}
    carol = lot4["as"]("carol@x", "radio")
    assert carol.get("/api/vault").json() == {"names": [], "project": "radio", "enabled": False}
    assert carol.post("/api/vault", json={"name": "A", "value": "b"}).status_code == 400


def test_session_and_agent_get_their_project_secrets_only(lot4):
    import agentchat
    import agents
    import agents_mcp
    import vault

    vault.set_secret("RADIO_TOKEN", "radio-value", project="radio")
    s = agentchat.AgentSession(RADIO_SID, user="alice@x", secrets=["RADIO_TOKEN",
                                                                   "DEFAULT_DB_PASSWORD"])
    assert vault.session_env(s._secret_names(), project=agentchat.session_project(RADIO_SID)) \
        == {"RADIO_TOKEN": "radio-value"}
    # an agent of radio cannot name a default secret (API and MCP)
    alice = lot4["as"]("alice@x", "radio")
    r = alice.post("/api/agents", json={"name": "leak", "purpose": "p", "deliverable": "d",
                                        "secrets": ["DEFAULT_DB_PASSWORD"]})
    assert r.status_code == 400 and "not in the vault" in r.json()["detail"]
    import os
    os.environ["SOKKAN_SESSION_PROJECT"] = "radio"
    os.environ["SOKKAN_SESSION_USER"] = "alice@x"
    try:
        out = agents_mcp.create_agent("leak2", "p", "d", secrets=["DEFAULT_DB_PASSWORD"])
        assert out["ok"] is False and "not in the vault" in out["error"]
        ok = agents_mcp.create_agent("radio-uses", "p", "d", secrets=["RADIO_TOKEN"])
        assert ok["ok"] is True
    finally:
        os.environ.pop("SOKKAN_SESSION_PROJECT")
        os.environ.pop("SOKKAN_SESSION_USER")
    # the run's redaction reads the agent's project vault
    a = agents.get_by_name("radio-uses", "radio")
    con = sqlite3.connect(agents.DB)
    con.execute("INSERT INTO runs(agent_id, trigger, session_id, created_at) VALUES(?,?,?,?)",
                (a["id"], "manual", "run-sid", time.time()))
    con.commit()
    con.close()
    assert agents.secrets_for_session("run-sid") == {"RADIO_TOKEN": "radio-value"}


def test_flat_vault_is_migrated_to_default(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    import vault

    key = Fernet.generate_key()
    (tmp_path / "vault.key").write_bytes(key)
    flat = {"OLD": Fernet(key).encrypt(b"old-value").decode()}
    (tmp_path / "vault.json").write_text(json.dumps(flat))
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    assert vault.names() == ["OLD"]
    assert vault.session_env(None) == {"OLD": "old-value"}
    d = json.loads((tmp_path / "vault.json").read_text())
    assert d["format"] == 2 and list(d["projects"]) == ["default"] and d["instance"] == {}
    assert json.loads((tmp_path / "vault.json.v1.bak").read_text()) == flat
    assert vault.names("radio") == []


# ---- budgets and cost totals -----------------------------------------------------------

def _transcript(d, sid, input_tokens):
    d.mkdir(parents=True, exist_ok=True)
    line = {"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {"model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": input_tokens, "output_tokens": 0}}}
    (d / f"{sid}.jsonl").write_text(json.dumps(line) + "\n")


@pytest.fixture()
def spend(lot4, monkeypatch):
    import budgets
    import usage

    tmp = lot4["tmp"]
    claude = tmp / "claude"
    monkeypatch.setattr(usage, "DB", tmp / "usage.db")
    monkeypatch.setattr(usage, "_claude_dir", str(claude))
    monkeypatch.setattr(usage, "PROJECT_DIR", claude / "projects" / "-workspace")
    work = tmp / "projects" / "radio" / "work"
    work.mkdir(parents=True, exist_ok=True)
    _transcript(claude / "projects" / "-workspace", "default-cli", 5_000_000)       # $15
    _transcript(claude / "projects" / str(work).replace("/", "-"), "radio-run", 1_000_000)  # $3
    budgets._cache.clear()
    return lot4


def test_cost_totals_are_per_project(spend):
    radio = spend["as"]("alice@x", "radio").get("/api/usage").json()
    assert radio["totals"]["today"]["cost"] == pytest.approx(3.0)
    assert radio["totals"]["all"]["cost"] == pytest.approx(3.0)
    assert [s["session_id"] for s in radio["sessions"]] == ["radio-run"]
    assert radio["project_budget"]["state"] == "ok"
    default = spend["as"]("bob@x").get("/api/usage").json()
    assert default["totals"]["today"]["cost"] == pytest.approx(15.0)
    assert [s["session_id"] for s in default["sessions"]] == ["default-cli"]


def test_project_budget_warns_then_stops(spend):
    import budgets

    carol = spend["as"]("carol@x", "radio")
    assert spend["as"]("alice@x", "radio").put("/api/budgets", json={"day": 1}).status_code == 403
    carol = spend["as"]("carol@x", "radio")
    r = carol.put("/api/budgets", json={"day": 3.5, "currency": "USD"})
    assert r.status_code == 200 and r.json()["state"] == "warn"
    assert budgets.check("radio")[0] == "warn"
    assert budgets.check("default") == ("ok", "")     # radio's spend is not default's
    r = carol.put("/api/budgets", json={"day": 0, "month": 2.5})
    assert r.json()["state"] == "stop" and "monthly budget reached" in r.json()["message"]
    # CHF ceiling: 3 USD = 2.31 CHF at 1.30 → a 2.5 CHF ceiling is not reached yet
    r = carol.put("/api/budgets", json={"currency": "CHF"})
    assert r.json()["state"] == "warn" and r.json()["spent_month"] == pytest.approx(3 / 1.3, 1e-3)


def test_project_budget_stop_blocks_new_turns_and_runs(spend, monkeypatch):
    import agentchat
    import agents
    import agents_runtime
    import budgets

    budgets.set_budget("radio", day=2)
    events: list[dict] = []
    started: list[str] = []

    async def fake_start(self):
        started.append(self.sid)
        raise RuntimeError("would start the model")

    monkeypatch.setattr(agentchat.AgentSession, "ensure_started", fake_start)
    s = agentchat.AgentSession(RADIO_SID, user="alice@x")
    monkeypatch.setattr(s, "_emit", events.append)
    asyncio.new_event_loop().run_until_complete(s.handle_user("hello"))
    assert started == [] and "budget reached" in events[-1]["message"]
    d = agentchat.AgentSession(DEFAULT_SID, user="bob@x")    # default: no ceiling, runs
    with pytest.raises(RuntimeError):
        asyncio.new_event_loop().run_until_complete(d.handle_user("hello"))
    assert started == [DEFAULT_SID]
    # an agent run of radio does not start
    a = agents.get_by_name("radio-check", "radio")
    run = agents.enqueue_run(a["id"], "manual", requested_by="alice@x")
    monkeypatch.setattr(agentchat, "get_or_create", lambda *x, **k: pytest.fail("run started"))
    rt = agents_runtime.Runtime()
    asyncio.new_event_loop().run_until_complete(rt._execute(a, run))
    r = agents.get_run(run["id"])
    assert r["status"] == "budget" and "budget reached" in r["error"]


# ---- agent names ----------------------------------------------------------------------

def test_agent_names_are_unique_per_project(lot4, monkeypatch):
    import agents
    import agents_mcp
    import board

    alice = lot4["as"]("alice@x", "radio")
    r = alice.post("/api/agents", json={"name": "default-audit", "purpose": "radio audit",
                                        "deliverable": "d"})
    assert r.status_code == 200, r.text            # the name exists in default: no leak
    assert agents.get_by_name("default-audit", "radio")["purpose"] == "radio audit"
    assert agents.get_by_name("default-audit", "default")["purpose"] == "p"
    r = alice.post("/api/agents", json={"name": "default-audit", "purpose": "x",
                                        "deliverable": "d"})
    assert r.status_code == 400                    # but unique inside radio
    # MCP and board resolve a name in the session's / card's project
    monkeypatch.setenv("SOKKAN_SESSION_PROJECT", "radio")
    monkeypatch.setenv("SOKKAN_SESSION_USER", "alice@x")
    assert agents_mcp.get_agent("default-audit")["purpose"] == "radio audit"
    with pytest.raises(ValueError):
        board.validate_assignee("agent:radio-check", "default")
    assert board.validate_assignee("agent:radio-check", "radio") == "agent:radio-check"
    # feature off: names unique per instance again (3.1 rule)
    _off(monkeypatch)
    r = lot4["as"]("bob@x").post("/api/agents", json={"name": "radio-check", "purpose": "p",
                                                      "deliverable": "d"})
    assert r.status_code == 400 and "already exists" in r.json()["detail"]


def test_agents_table_rebuild_keeps_ids_and_runs(tmp_path, monkeypatch):
    import agents

    db = tmp_path / "agents31.db"
    con = sqlite3.connect(db)
    con.executescript(agents._SCHEMA)        # the 3.1 table: name UNIQUE per instance
    con.execute("INSERT INTO agents(id, name, owner, created_at, updated_at) "
                "VALUES(7, 'nightly', 'bob@x', 1, 1)")
    con.execute("INSERT INTO runs(agent_id, trigger, created_at) VALUES(7, 'manual', 1)")
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "DB", db)
    agents.init(force=True)
    agents.init(force=True)                  # idempotent
    con = sqlite3.connect(db)
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='agents'").fetchone()[0]
    assert "UNIQUE(project, name)" in sql and "NOT NULL UNIQUE" not in sql
    assert con.execute("SELECT id, name, project FROM agents").fetchall() == [
        (7, "nightly", "default")]
    con.execute("INSERT INTO agents(name, owner, created_at, updated_at, project) "
                "VALUES('nightly', 'a@x', 1, 1, 'radio')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO agents(name, owner, created_at, updated_at, project) "
                    "VALUES('nightly', 'a@x', 1, 1, 'radio')")
    assert con.execute("SELECT r.agent_id, a.name FROM runs r JOIN agents a ON a.id = r.agent_id"
                       ).fetchall() == [(7, "nightly")]
    con.close()
    assert (tmp_path / "agents31.db.pre-lot4.bak").exists()


# ---- CortHeXis review per project ---------------------------------------------------------

def _note(d, name, body):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name.replace('-', '_')}.md").write_text(
        f"---\nname: {name}\ndescription: >-\n  {name} description\ntype: project\n---\n{body}\n")


@pytest.fixture()
def review(lot4, monkeypatch):
    import corthexis
    from core import review as rv

    tmp = lot4["tmp"]
    monkeypatch.setattr(corthexis, "_pg", lambda: True)
    monkeypatch.setattr(corthexis, "source", lambda project="default": rv.SqliteSource(
        tmp / "no-index.db"))
    monkeypatch.setattr(corthexis, "DATA_DIR", tmp)
    monkeypatch.setattr(corthexis, "PROPOSALS", tmp / "corthexis-proposals.json")
    monkeypatch.setattr(corthexis, "_reports", {})
    monkeypatch.setattr(corthexis, "_project_history", {})
    monkeypatch.setenv("CORTHEXIS_REVIEW_CHAIN", "0")
    _note(tmp / "memory", "default-secret-plan", "See [[nowhere-default]].")
    _note(tmp / "projects" / "radio" / "memory", "radio-note", "See [[ghost-radio]].")
    return lot4


def test_review_runs_on_the_project_corpus(review):
    rep = review["as"]("alice@x", "radio").get("/api/corthexis/review").json()
    text = json.dumps(rep["report"])
    assert rep["project"] == "radio" and "radio-note" in text
    assert "default-secret-plan" not in text and "nowhere-default" not in text


def test_proposals_are_per_project(review):
    import corthexis

    recs = {"pd": {"id": "pd", "title": "default fix", "status": "pending", "created_at": 1},
            "pr": {"id": "pr", "title": "radio fix", "status": "pending", "created_at": 2,
                   "project": "radio"}}
    corthexis.PROPOSALS.write_text(json.dumps(recs))
    alice = review["as"]("alice@x", "radio")
    assert [p["id"] for p in alice.get("/api/corthexis/proposals").json()] == ["pr"]
    assert alice.post("/api/corthexis/proposals/pd/refuse").status_code == 404
    assert alice.post("/api/corthexis/proposals/pr/refuse").json()["status"] == "refused"
    bob = review["as"]("bob@x")
    assert [p["id"] for p in bob.get("/api/corthexis/proposals").json()] == ["pd"]
    assert bob.post("/api/corthexis/proposals/pr/approve").status_code == 404


def test_review_off_stays_default_only(review, monkeypatch):
    _off(monkeypatch)
    alice = review["as"]("alice@x", "radio")
    assert alice.get("/api/corthexis/review").json()["report"] == {}
    assert alice.get("/api/corthexis/proposals").json() == []
    assert alice.post("/api/corthexis/review/run").status_code == 409


# ---- journal ----------------------------------------------------------------------------

def test_project_admin_sees_the_project_journal_only(lot4, monkeypatch):
    import audit

    carol = lot4["as"]("carol@x", "radio")
    carol.post("/api/vault", json={"name": "RADIO_TOKEN", "value": "v"})
    lot4["as"]("bob@x").post("/api/board/card", json={"title": "default secret card"})
    audit.log("system", "instance.thing", "", "instance-level")
    j = lot4["as"]("carol@x", "radio").get("/api/audit")
    assert j.status_code == 200
    rows = j.json()
    assert rows and {r["project"] for r in rows} == {"radio"}
    assert "vault.set" in {r["action"] for r in rows}
    assert "default secret card" not in json.dumps(rows)
    assert lot4["as"]("alice@x", "radio").get("/api/audit").status_code == 403   # dev
    assert lot4["as"]("carol@x", "default").get("/api/audit").status_code == 403
    full = lot4["as"]("admin@x").get("/api/audit").json()
    assert {"radio", "default", ""} <= {r["project"] for r in full}
    _off(monkeypatch)
    assert lot4["as"]("carol@x", "radio").get("/api/audit").status_code == 403


def test_run_skipped_and_agent_paused_when_the_owner_left_the_project(lot4, monkeypatch):
    import agentchat
    import agents
    import agents_runtime
    import projects

    a = agents.get_by_name("radio-check", "radio")
    assert agents_runtime.owner_check(a) is None
    projects.revoke("radio", "team", "sso:radio-devs")        # alice is no longer in radio
    projects.grant("radio", "user", "alice@x", "viewer")       # …or only a viewer
    run = agents.enqueue_run(a["id"], "manual", requested_by="carol@x")
    monkeypatch.setattr(agentchat, "get_or_create", lambda *x, **k: pytest.fail("run started"))
    asyncio.new_event_loop().run_until_complete(agents_runtime.Runtime()._execute(a, run))
    r = agents.get_run(run["id"])
    assert r["status"] == "skipped" and "no longer dev" in r["error"]
    assert agents.get(a["id"])["status"] == "paused"
    assert agents_runtime.owner_check(agents.get_by_name("default-audit")) is None
