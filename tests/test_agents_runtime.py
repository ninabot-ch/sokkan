"""Agents scheduler + executor with a fake session (no CLI, no model)."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.usefixtures("model_credentials")  # 3.2: a configured instance

ZH = ZoneInfo("Europe/Zurich")
DEV = {"email": "dev@x.ch", "role": "dev"}


def _ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ZH).timestamp()


class FakeSession:
    """Stands in for agentchat.AgentSession: records the prompt, plays a script."""

    instances: list = []

    def __init__(self, sid, user="", model=None, policy=None, script=None):
        self.sid, self.user, self.model, self.policy = sid, user, model, policy
        self.events, self.cost_usd, self.tokens_in, self.tokens_out, self.num_turns = \
            [], 0.0, 0, 0, 0
        self.last_result = None
        self.prompt = ""
        self.script = script or {}
        self.interrupted = False
        FakeSession.instances.append(self)

    async def handle_user(self, text):
        self.prompt = text
        if self.script.get("wait_approval"):
            self.policy["on_wait"](True)
        if self.script.get("sleep"):
            await asyncio.sleep(self.script["sleep"])
        if self.script.get("wait_approval"):
            self.policy["on_wait"](False)
        self.cost_usd, self.tokens_in, self.tokens_out, self.num_turns = 0.0123, 1200, 80, 3
        self.last_result = {"text": self.script.get("text", "All good.\nDELIVERY: done"),
                            "is_error": self.script.get("is_error", False),
                            "subtype": self.script.get("subtype", "success")}

    async def interrupt(self):
        self.interrupted = True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agentchat
    import agents
    import agents_runtime
    import audit
    import board
    import notify
    import vault

    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(agents_runtime, "DATA_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a) or {})
    script: dict = {}
    FakeSession.instances = []

    def fake_goc(sid, user="", model=None, policy=None, **_):
        return FakeSession(sid, user, model, policy, script)

    async def fake_drop(_sid):
        return None

    monkeypatch.setattr(agentchat, "get_or_create", fake_goc)
    monkeypatch.setattr(agentchat, "drop", fake_drop)
    notes = []
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("SOKKAN_MEMORY_QUARANTINE_DIR", str(tmp_path / "quarantine"))
    rt = agents_runtime.Runtime(recall=lambda q, sid: "=== Project memory (auto-recalled) ===\n- [x]")
    return {"agents": agents, "rt": rt, "sent": sent, "script": script, "notes": notes,
            "board": board, "vault": vault, "tmp": tmp_path}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _drain(rt):
    while rt.tasks:
        await asyncio.gather(*list(rt.tasks.values()), return_exceptions=True)


def _agent(ag, **kw):
    d = dict(name="nightly-cve-audit", purpose="Audit deps", deliverable="CVE table",
             trigger="cron", schedule="0 2 * * *")
    d.update(kw)
    return ag.create(DEV, d, activate=True)


def test_due_cron_runs_once_and_rearms(env):
    ag, rt = env["agents"], env["rt"]
    a = _agent(ag)
    occ = a["next_run_at"]

    async def go():
        await rt.tick(now=occ + 5)
        await rt.tick(now=occ + 6)  # second tick, same occurrence: nothing new
        await _drain(rt)
    _run(go())
    runs = ag.list_runs(DEV, a["id"])
    assert len(runs) == 1 and runs[0]["status"] == "succeeded"
    assert runs[0]["scheduled_for"] == occ and runs[0]["trigger"] == "schedule"
    assert runs[0]["cost_usd"] == pytest.approx(0.0123) and runs[0]["tokens_in"] == 1200
    nxt = ag.get(a["id"])["next_run_at"]
    assert nxt > occ + 6 and datetime.fromtimestamp(nxt, ZH).hour == 2


def test_two_runtimes_never_double_run(env):
    import agents_runtime

    ag, rt = env["agents"], env["rt"]
    rt2 = agents_runtime.Runtime()
    a = _agent(ag)
    occ = a["next_run_at"]
    a_snapshot = ag.get(a["id"])

    async def go():
        # both loops see the agent due at the same instant (stale read for the 2nd)
        rt.schedule_due(occ + 1)
        import agents as m
        orig = m.due_agents
        m.due_agents = lambda now: [a_snapshot]
        try:
            rt2.schedule_due(occ + 1)
        finally:
            m.due_agents = orig
        rt.start_queued()
        rt2.start_queued()
        await _drain(rt)
        await _drain(rt2)
    _run(go())
    assert len([r for r in ag.list_runs(DEV, a["id"]) if r["status"] == "succeeded"]) == 1


def test_missed_while_down_one_catch_up_only(env, monkeypatch):
    import agents_runtime

    ag, rt = env["agents"], env["rt"]
    a = _agent(ag, schedule="0 * * * *")  # hourly
    occ = a["next_run_at"]
    monkeypatch.setattr(agents_runtime, "MISFIRE_S", 6 * 3600)

    async def go():
        await rt.tick(now=occ + 3 * 3600 + 60)  # down for 3 h: 4 occurrences missed
        await _drain(rt)
    _run(go())
    runs = ag.list_runs(DEV, a["id"])
    assert len(runs) == 1 and runs[0]["scheduled_for"] == occ + 3 * 3600


def test_missed_too_long_ago_is_skipped(env, monkeypatch):
    import agents_runtime

    ag, rt = env["agents"], env["rt"]
    a = _agent(ag)
    monkeypatch.setattr(agents_runtime, "MISFIRE_S", 3600)
    _run(rt.tick(now=a["next_run_at"] + 2 * 3600))
    runs = ag.list_runs(DEV, a["id"])
    assert [r["status"] for r in runs] == ["skipped"]
    assert "missed" in runs[0]["error"]


def test_restart_marks_running_runs_interrupted_and_notifies(env):
    import agents_runtime

    ag = env["agents"]
    a = _agent(ag)
    r = ag.request_run(DEV, a["id"])
    ag.claim_run(r["id"], "old-process")
    agents_runtime.Runtime().recover()
    assert ag.get_run(r["id"])["status"] == "interrupted"
    assert any("interrupted" in s[0] for s in env["sent"])


def test_run_prompt_carries_mission_recall_and_secret_names_not_values(env):
    ag, rt, vault = env["agents"], env["rt"], env["vault"]
    vault.set_secret("GITHUB_TOKEN", "ghp_supersecretvalue1234567890")
    vault.set_secret("OTHER", "unrelated-value-9999")
    a = _agent(ag, name="pr-review", trigger="manual", schedule="", secrets=["GITHUB_TOKEN"],
               auto_approve=["Bash(gh pr list:*)"], budget_usd=0.5, model="haiku")
    env["script"]["text"] = ("Found 2 PRs. Used ghp_supersecretvalue1234567890 to call.\n"
                             "DELIVERY: done")
    ag.request_run(DEV, a["id"])

    async def go():
        rt.start_queued()
        await _drain(rt)
    _run(go())
    s = FakeSession.instances[-1]
    assert "Audit deps" in s.prompt and "CVE table" in s.prompt
    assert "$GITHUB_TOKEN" in s.prompt and "ghp_supersecret" not in s.prompt
    assert "auto-recalled" in s.prompt and "DELIVERY: done" in s.prompt
    assert s.policy["secrets"] == ["GITHUB_TOKEN"] and s.policy["budget_usd"] == 0.5
    assert s.policy["auto_approve"] == ["Bash(gh pr list:*)"] and s.model == "haiku"
    assert s.user == "dev@x.ch" and "sokkan-agents" in s.policy["mcp"]
    run = ag.list_runs(DEV, a["id"])[0]
    assert run["status"] == "succeeded"
    assert "ghp_supersecret" not in run["deliverable"]
    assert "[secret:GITHUB_TOKEN]" in run["deliverable"]
    card = env["board"].get_card(run["outputs"]["card"])
    assert card["bucket"] == "Review" and "ghp_" not in card["description"]


@pytest.mark.parametrize("script,status,notified", [
    ({"text": "Could not reach the registry.\nDELIVERY: incomplete — registry down"},
     "incomplete", True),
    ({"is_error": True, "text": "API error", "subtype": "error_during_execution"}, "failed", True),
    ({"subtype": "error_max_budget_usd", "text": ""}, "budget", True),
])
def test_end_states_and_notifications(env, script, status, notified):
    ag, rt = env["agents"], env["rt"]
    env["script"].update(script)
    a = _agent(ag, trigger="manual", schedule="", budget_usd=0.2)
    ag.request_run(DEV, a["id"])

    async def go():
        rt.start_queued()
        await _drain(rt)
    _run(go())
    run = ag.list_runs(DEV, a["id"])[0]
    assert run["status"] == status
    assert bool(env["sent"]) is notified
    assert ag.list_agents(DEV)[0]["deck"] == "error"


def test_budget_below_one_call_does_not_start_the_run(env):
    """3.4.4: one call to Opus cost $0.2008 for a $0.10 budget in prod — the first call
    writes the whole prompt to the cache. A budget below that estimate is refused up front."""
    import agentcost
    ag, rt = env["agents"], env["rt"]
    before = len(FakeSession.instances)
    a = _agent(ag, trigger="manual", schedule="", budget_usd=0.005, model="haiku")
    ag.request_run(DEV, a["id"])

    async def go():
        rt.start_queued()
        await _drain(rt)
    _run(go())
    run = ag.list_runs(DEV, a["id"])[0]
    assert run["status"] == "budget" and len(FakeSession.instances) == before  # no session
    assert "$0.005 is below the cost of one call to haiku" in run["error"]
    assert "Raise the budget to at least $0.04" in run["error"] and env["sent"]
    # a budget that holds the first call runs; what it measured calibrates the next estimate
    m = agentcost.metering("haiku")
    assert m["first_call_usd"] == pytest.approx(20_000 * 1.25 / 1e6)
    agentcost.record_first_call(m, 0.09)
    assert agentcost.metering("haiku")["first_call_usd"] == pytest.approx(0.09)
    b = _agent(ag, name="enough", trigger="manual", schedule="", budget_usd=0.5, model="haiku")
    ag.request_run(DEV, b["id"])
    _run(go())
    assert ag.list_runs(DEV, b["id"])[0]["status"] == "succeeded"


def test_budget_on_the_cli_default_model_is_estimated(env, monkeypatch):
    """3.4.5: an agent without a model runs on the CLI's default — 3.4.4 had no estimate
    (null) and a $0.10 run spent $0.1963 on its first call in prod. The estimate now takes
    the dearest Claude model of the table, so that run does not start."""
    import agentcost
    for k in ("SOKKAN_AGENT_MODEL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(agentcost.llm, "session_model", lambda: "")
    m = agentcost.metering(None)
    assert m["basis"] == "sdk" and m["model"] in ("", "default")
    assert m["first_call_usd"] and m["first_call_usd"] > 0.10
    ag, rt = env["agents"], env["rt"]
    before = len(FakeSession.instances)
    a = _agent(ag, name="nomodel", trigger="manual", schedule="", budget_usd=0.10, model="")
    ag.request_run(DEV, a["id"])

    async def go():
        rt.start_queued()
        await _drain(rt)
    _run(go())
    run = ag.list_runs(DEV, a["id"])[0]
    assert run["status"] == "budget" and len(FakeSession.instances) == before


def test_timeout_interrupts_the_session(env):
    ag, rt = env["agents"], env["rt"]
    env["script"]["sleep"] = 5
    a = _agent(ag, trigger="manual", schedule="", max_minutes=1)
    ag._write(a["id"], max_minutes=1)
    import agents_runtime
    ag.request_run(DEV, a["id"])

    async def go():
        orig = agents_runtime.asyncio.wait_for

        async def fast_wait_for(coro, timeout):  # 1 min → 0.05 s for the test
            return await orig(coro, 0.05 if timeout == 60 else timeout)
        agents_runtime.asyncio.wait_for = fast_wait_for
        try:
            rt.start_queued()
            await _drain(rt)
        finally:
            agents_runtime.asyncio.wait_for = orig
    _run(go())
    run = ag.list_runs(DEV, a["id"])[0]
    assert run["status"] == "timeout" and FakeSession.instances[-1].interrupted
    assert any("timeout" in s[0] for s in env["sent"])


def test_outputs_memory_file_and_notify(env):
    ag, rt = env["agents"], env["rt"]
    a = _agent(ag, name="weekly-report", trigger="manual", schedule="",
               outputs=["memory", "file", "notify"], notify_on=["failure"])
    env["script"]["text"] = "# Weekly ops report\nAll green.\nDELIVERY: done"
    ag.request_run(DEV, a["id"])

    async def go():
        rt.start_queued()
        await _drain(rt)
    _run(go())
    run = ag.list_runs(DEV, a["id"])[0]
    assert run["outputs"]["memory"] == "agent-weekly-report-latest"
    assert run["outputs"]["memory_quarantined"] is True
    # quarantined: NOT in the memory dir (never indexed, never recalled)
    assert not (env["tmp"] / "memory" / "agent-weekly-report-latest.md").exists()
    q = (env["tmp"] / "quarantine" / "agent-weekly-report-latest.md").read_text()
    assert '"agent": "weekly-report"' in q and "DELIVERY" not in q
    assert open(run["outputs"]["file"]).read().startswith("# Weekly ops report")
    assert len(env["sent"]) == 1 and "succeeded" in env["sent"][0][0]


def test_waiting_for_approval_is_visible(env):
    ag, rt = env["agents"], env["rt"]
    env["script"]["wait_approval"] = True
    env["script"]["sleep"] = 0.2
    a = _agent(ag, trigger="manual", schedule="")
    r = ag.request_run(DEV, a["id"])
    seen = {}

    async def go():
        rt.start_queued()
        await asyncio.sleep(0.1)
        seen["waiting"] = ag.get_run(r["id"])["waiting_approval"]
        seen["pending"] = ag.pending_approvals(DEV)["runs"]
        await _drain(rt)
    _run(go())
    assert seen["waiting"] is True and seen["pending"][0]["agent_name"] == a["name"]
    assert ag.get_run(r["id"])["waiting_approval"] is False


def test_one_live_run_per_agent_and_global_cap(env, monkeypatch):
    import agents_runtime

    ag, rt = env["agents"], env["rt"]
    monkeypatch.setattr(agents_runtime, "MAX_CONCURRENT", 1)
    env["script"]["sleep"] = 0.1
    a = _agent(ag, name="one", trigger="manual", schedule="")
    b = _agent(ag, name="two", trigger="manual", schedule="")
    ag.request_run(DEV, a["id"])
    ag.request_run(DEV, b["id"])

    async def go():
        rt.start_queued()
        assert len(rt.tasks) == 1
        await _drain(rt)
        rt.start_queued()
        await _drain(rt)
    _run(go())
    assert all(r["status"] == "succeeded" for x in (a, b) for r in ag.list_runs(DEV, x["id"]))


def test_alert_event_triggers_matching_agents(env):
    ag, rt = env["agents"], env["rt"]
    hit = _agent(ag, name="db-triage", trigger="event", schedule="", event="alert:Postgres*")
    _agent(ag, name="other", trigger="event", schedule="", event="alert:Disk*")
    anyalert = _agent(ag, name="any", trigger="event", schedule="", event="alert")
    ids = rt.fire_event("alert", "PostgresReplicationLag", {"severity": "critical"})
    assert len(ids) == 2
    ran = {ag.get_run(i)["agent_id"] for i in ids}
    assert ran == {hit["id"], anyalert["id"]}
    assert ag.get_run(ids[0])["context"]["severity"] == "critical"


def test_one_shot_runs_once(env):
    ag, rt = env["agents"], env["rt"]
    a = ag.create(DEV, dict(name="migrate-check", purpose="p", deliverable="d", trigger="once",
                            once_at=_ts(2026, 10, 8, 9, 0)), activate=True)

    async def go():
        await rt.tick(now=_ts(2026, 10, 8, 9, 1))
        await _drain(rt)
        await rt.tick(now=_ts(2026, 10, 9, 9, 1))
    _run(go())
    assert len(ag.list_runs(DEV, a["id"])) == 1
    assert ag.get(a["id"])["next_run_at"] is None


def test_parse_delivery():
    import agents_runtime as R

    assert R.parse_delivery("x\nDELIVERY: done") == ("done", "")
    assert R.parse_delivery("**DELIVERY:** incomplete — no access") == ("incomplete", "no access")
    assert R.parse_delivery("nothing") == (None, "")
