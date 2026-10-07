"""SOKKAN 3.2 lot 2 — scheduler guard, secrets by name by default, 8 h cockpit sessions.

The guard comes from two incidents of 07.10.2026: an instance started on a data directory
holding a queued run started that run at boot, with the credentials it found (the host's
Claude CLI login). Without model credentials explicitly configured for the instance:
no run starts, nothing is caught up at boot, nothing piles up for later.
"""
import asyncio
import json
import time

import jwt
import pytest

DEV = {"email": "dev@x.ch", "role": "dev"}


@pytest.fixture()
def bare(tmp_path, monkeypatch):
    """An instance with NO explicit model credentials — but a Claude CLI login on disk."""
    import agentchat
    import agents
    import audit
    import board
    import llm
    import notify

    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SOKKAN_INFER_BASE_URL",
              "SOKKAN_INFER_TOKEN", "SOKKAN_AGENTS_USE_CLI_LOGIN", "SOKKAN_DEMO_CREW",
              "SOKKAN_DEMO_BANNER"):
        monkeypatch.delenv(k, raising=False)
    cli = tmp_path / "claude"
    cli.mkdir()
    (cli / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "x"}}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cli))
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(notify, "send", lambda *a, **k: {})
    started = []

    def no_session(*a, **k):
        started.append(a)
        raise AssertionError("a run started a session without credentials")

    monkeypatch.setattr(agentchat, "get_or_create", no_session)
    return {"agents": agents, "llm": llm, "started": started, "tmp": tmp_path}


def _cron_agent(agents, name="nightly-cve", cron="*/15 * * * *"):
    a = agents.create(DEV, {"name": name, "purpose": "check deps", "deliverable": "a report",
                            "trigger": "cron", "schedule": cron}, activate=True)
    return agents.get(a["id"])


def test_cli_login_is_not_an_explicit_credential(bare, monkeypatch):
    llm = bare["llm"]
    assert llm.unattended_credentials() is None            # the login file does not count
    monkeypatch.setenv("SOKKAN_AGENTS_USE_CLI_LOGIN", "1")  # unless the operator says so
    assert llm.unattended_credentials() == "cli-login"
    monkeypatch.delenv("SOKKAN_AGENTS_USE_CLI_LOGIN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert llm.unattended_credentials() == "env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    llm.CONFIG.write_text(json.dumps({"mode": "byok", "anthropic_api_key": "sk-ant-x"}))
    assert llm.unattended_credentials() == "byok"


def test_boot_without_credentials_runs_nothing_and_catches_nothing_up(bare, monkeypatch):
    import agents_runtime

    agents = bare["agents"]
    a = _cron_agent(agents)
    # the data directory this instance starts on: a run left queued, a schedule overdue
    old = agents.enqueue_run(a["id"], "manual", "dev@x.ch")
    con = agents._con()
    con.execute("UPDATE runs SET created_at = ? WHERE id = ?", (time.time() - 3600, old["id"]))
    con.execute("UPDATE agents SET next_run_at = ? WHERE id = ?", (time.time() - 1800, a["id"]))
    con.close()

    rt = agents_runtime.Runtime()

    async def boot_and_tick():
        rt.start()
        await rt.tick()
        await rt.tick()
        await rt.stop()

    asyncio.run(boot_and_tick())
    assert not bare["started"] and not rt.tasks
    runs = {r["id"]: r for r in agents.list_runs(DEV, a["id"], 50)}
    assert runs[old["id"]]["status"] == "skipped"
    assert "no model credentials" in runs[old["id"]]["error"]
    assert not agents.runs_with_status("queued") and not agents.runs_with_status("running")
    skipped_sched = [r for r in runs.values() if r["trigger"] == "schedule"]
    assert len(skipped_sched) == 1 and skipped_sched[0]["status"] == "skipped"
    assert agents.get(a["id"])["next_run_at"] > time.time()   # moved forward, no catch-up
    assert rt.state()["held"] and "no model credentials" in rt.state()["reason"]

    # credentials arrive: the scheduler resumes from the NEXT occurrence, nothing replayed
    monkeypatch.setenv("SOKKAN_AGENTS_USE_CLI_LOGIN", "1")
    asyncio.run(rt.tick())
    assert not bare["started"] and not agents.runs_with_status("queued")
    assert rt.state() == {"running": True, "held": False, "reason": None,
                          "credentials": "cli-login"}


def test_manual_and_event_runs_are_refused_without_credentials(bare, monkeypatch):
    import agents_runtime

    agents = bare["agents"]
    a = _cron_agent(agents)
    with pytest.raises(agents.AgentError, match="no model credentials"):
        agents.request_run(DEV, a["id"])
    assert not agents.runs_with_status("queued")
    ev = agents.create(DEV, {"name": "on-alert", "purpose": "triage", "deliverable": "notes",
                             "trigger": "event", "event": "alert:*"}, activate=True)
    rt = agents_runtime.Runtime()
    assert rt.fire_event("alert", "disk-full") == []
    runs = agents.list_runs(DEV, ev["id"], 10)
    assert [r["status"] for r in runs] == ["skipped"]
    # even a queued row written by someone else never starts
    agents.enqueue_run(a["id"], "manual", "x")
    rt.start_queued()
    assert not rt.tasks and not bare["started"]
    monkeypatch.setenv("SOKKAN_AGENTS_USE_CLI_LOGIN", "1")
    agents.update_run(agents.runs_with_status("queued")[0]["id"], status="cancelled")
    assert agents.request_run(DEV, a["id"])["status"] == "queued"


def test_crew_api_says_the_scheduler_is_held(bare, monkeypatch):
    import agents_runtime
    import app

    monkeypatch.setattr(agents_runtime, "_runtime", agents_runtime.Runtime())
    st = app._scheduler_state()
    assert st["held"] and not st["running"] and "credentials" not in st
    monkeypatch.setattr(agents_runtime, "_runtime", None)
    assert app._scheduler_state()["running"] is False


# ---- cockpit session: 8 h ------------------------------------------------------------

def test_session_cookie_lives_8_hours_counted_from_issue(monkeypatch):
    import session

    assert session.TTL == 8 * 3600
    monkeypatch.setenv("SOKKAN_SESSION_TTL_S", "999999")
    assert session._ttl() == 24 * 3600                   # bounded
    monkeypatch.setenv("SOKKAN_SESSION_TTL_S", "10")
    assert session._ttl() == 300

    class Req:
        def __init__(self, tok):
            self.cookies = {session.COOKIE: tok}

    assert session.email_from_request(Req(session.make("a@x.ch"))) == "a@x.ch"
    # a 24 h cookie issued by 3.1 nine hours ago: still unexpired, but over the 3.2 limit
    now = int(time.time())
    old = jwt.encode({"email": "a@x.ch", "iat": now - 9 * 3600, "exp": now + 15 * 3600},
                     session.SECRET, algorithm="HS256")
    assert session.email_from_request(Req(old)) is None
