"""Agents (3.1 "Crew up") — store, validation, lifecycle, access rules, cron, deck."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.usefixtures("model_credentials")  # 3.2: a configured instance

ZH = ZoneInfo("Europe/Zurich")
OWNER = {"email": "owner@localhost", "role": "owner"}
DEV = {"email": "dev@x.ch", "role": "dev"}
DEV2 = {"email": "other@x.ch", "role": "dev"}
VIEWER = {"email": "v@x.ch", "role": "viewer"}
ADMIN = {"email": "admin@x.ch", "role": "admin"}


@pytest.fixture()
def ag(tmp_path, monkeypatch):
    import agents as a

    monkeypatch.setattr(a, "DB", tmp_path / "agents.db")
    a.init(force=True)
    return a


def _base(**kw):
    d = dict(name="nightly-cve-audit", purpose="Audit npm dependencies of /workspace for CVEs",
             deliverable="A table of vulnerable packages with fix versions",
             done_criteria="every direct dependency checked")
    d.update(kw)
    return d


def _ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ZH).timestamp()


# ---- cron ------------------------------------------------------------------------
def test_cron_nightly_is_zurich_wall_clock_summer_and_winter():
    import cronexpr

    c = cronexpr.parse("0 2 * * *")
    summer = c.next_after(_ts(2026, 7, 1, 12, 0))
    winter = c.next_after(_ts(2026, 12, 1, 12, 0))
    assert datetime.fromtimestamp(summer, ZH).strftime("%m-%d %H:%M") == "07-02 02:00"
    assert datetime.fromtimestamp(winter, ZH).strftime("%m-%d %H:%M") == "12-02 02:00"
    # 02:00 Zurich = 00:00 UTC in summer, 01:00 UTC in winter
    assert datetime.fromtimestamp(summer, timezone.utc).hour == 0
    assert datetime.fromtimestamp(winter, timezone.utc).hour == 1


def test_cron_spring_forward_gap_is_skipped_and_autumn_hour_runs_once():
    import cronexpr

    c = cronexpr.parse("30 2 * * *")
    # 2026-03-29: 02:00 → 03:00 in Zurich, 02:30 does not exist → next is the 30th
    nxt = c.next_after(_ts(2026, 3, 28, 12, 0))
    assert datetime.fromtimestamp(nxt, ZH).strftime("%m-%d %H:%M") == "03-30 02:30"
    # 2026-10-25: 03:00 → 02:00, 02:30 happens twice; it fires once
    first = c.next_after(_ts(2026, 10, 24, 12, 0))
    assert datetime.fromtimestamp(first, ZH).strftime("%m-%d %H:%M") == "10-25 02:30"
    second = c.next_after(first)
    assert datetime.fromtimestamp(second, ZH).strftime("%m-%d") == "10-26"


def test_cron_fields_names_steps_and_vixie_day_or():
    import cronexpr

    mon = cronexpr.parse("0 8 * * mon")
    t = mon.next_after(_ts(2026, 10, 7, 9, 0))  # a Wednesday
    assert datetime.fromtimestamp(t, ZH).strftime("%a %H:%M") == "Mon 08:00"
    every15 = cronexpr.parse("*/15 * * * *")
    t = every15.next_after(_ts(2026, 10, 7, 9, 1))
    assert datetime.fromtimestamp(t, ZH).minute == 15
    # dom AND dow restricted → either matches (1st of month OR Friday)
    c = cronexpr.parse("0 0 1 * fri")
    t = c.next_after(_ts(2026, 10, 7, 9, 0))
    assert datetime.fromtimestamp(t, ZH).strftime("%a") == "Fri"
    assert cronexpr.parse("@daily").next_after(0) > 0
    for bad in ("", "0 2 * *", "61 * * * *", "0 2 * * 8", "*/0 * * * *", "0 2 31-1 * *"):
        with pytest.raises(cronexpr.CronError):
            cronexpr.parse(bad)


# ---- validation --------------------------------------------------------------------
def test_create_defaults_and_human_draft_vs_activate(ag):
    a = ag.create(DEV, _base())
    assert a["status"] == "draft" and a["owner"] == "dev@x.ch"
    assert a["tools"] == ag.DEFAULT_TOOLS and a["mcp"] == ["sokkan-memory"]
    assert a["outputs"] == ["card"] and "failure" in a["notify_on"]
    b = ag.create(DEV, _base(name="weekly-report", trigger="cron", schedule="0 8 * * mon"),
                  activate=True)
    assert b["status"] == "active" and b["approved_by"] == "dev@x.ch"
    assert b["next_run_at"] and b["next_run_at"] > 0


@pytest.mark.parametrize("bad,msg", [
    ({"name": "Bad Name"}, "kebab-case"),
    ({"purpose": ""}, "purpose is required"),
    ({"deliverable": ""}, "deliverable is required"),
    ({"trigger": "cron"}, "needs a schedule"),
    ({"trigger": "cron", "schedule": "every night"}, "5 fields"),
    ({"trigger": "once"}, "needs once_at"),
    ({"trigger": "event"}, "needs an event"),
    ({"trigger": "event", "event": "webhook"}, "event:"),
    ({"secrets": ["ghp_abcdefghijklmnopqrstuvwxyz0123"]}, "NAMES only"),
    ({"secrets": ["not a name"]}, "NAMES only"),
    ({"purpose": "use token sk-ant-api03-abcdefghijklmnopqrstuv to call"}, "credential"),
    ({"tools": ["Read"], "auto_approve": ["Bash(npm audit:*)"]}, "subset of tools"),
    ({"mcp": ["evil-server"]}, "mcp:"),
    ({"outputs": ["slack"]}, "outputs"),
    ({"budget_usd": -1}, "between"),
    ({"timezone": "Mars/Olympus"}, "unknown timezone"),
    ({"playbook": "nope"}, "unknown playbook"),
])
def test_validation_messages(ag, bad, msg):
    with pytest.raises(ag.AgentError, match=msg):
        ag.create(DEV, _base(**bad))


def test_secrets_must_exist_in_vault_when_known(ag):
    with pytest.raises(ag.AgentError, match="not in the vault: NPM_TOKEN"):
        ag.create(DEV, _base(secrets=["NPM_TOKEN"]), known_secrets=["GITHUB_TOKEN"])
    a = ag.create(DEV, _base(secrets=["GITHUB_TOKEN"]), known_secrets=["GITHUB_TOKEN"])
    assert a["secrets"] == ["GITHUB_TOKEN"]


def test_duplicate_name_refused(ag):
    ag.create(DEV, _base())
    with pytest.raises(ag.AgentError, match="already exists"):
        ag.create(DEV2, _base())


# ---- lifecycle -----------------------------------------------------------------------
def test_session_proposal_is_pending_until_a_human_approves(ag):
    a = ag.create(DEV, _base(trigger="cron", schedule="0 2 * * *"), proposal=True,
                  created_by="session:abc")
    assert a["status"] == "pending" and a["next_run_at"] is None
    assert a["created_by"] == "session:abc"
    with pytest.raises(ag.AgentError, match="only an active"):
        ag.request_run(DEV, a["id"])
    a = ag.approve(DEV, a["id"])
    assert a["status"] == "active" and a["next_run_at"] and a["approved_by"] == "dev@x.ch"


def test_reject_proposal_back_to_draft(ag):
    a = ag.create(DEV, _base(), proposal=True)
    assert ag.reject(DEV, a["id"])["status"] == "draft"


def test_session_change_on_active_agent_is_pending_and_old_version_keeps_running(ag):
    a = ag.create(DEV, _base(trigger="cron", schedule="0 2 * * *"), activate=True)
    b = ag.update(DEV, a["id"], {"schedule": "0 3 * * *", "model": "opus"}, from_session=True)
    assert b["status"] == "active" and b["schedule"] == "0 2 * * *"
    assert b["pending_change"] == {"schedule": "0 3 * * *", "model": "opus"}
    c = ag.approve(DEV, a["id"])
    assert c["schedule"] == "0 3 * * *" and c["model"] == "opus" and c["pending_change"] is None
    d = ag.update(DEV, a["id"], {"model": "haiku"}, from_session=True)
    assert ag.reject(DEV, a["id"])["model"] == "opus" and d["pending_change"]


def test_human_edit_applies_directly(ag):
    a = ag.create(DEV, _base(trigger="cron", schedule="0 2 * * *"), activate=True)
    b = ag.update(DEV, a["id"], {"schedule": "15 4 * * *"})
    assert b["schedule"] == "15 4 * * *" and b["pending_change"] is None
    nxt = datetime.fromtimestamp(b["next_run_at"], ZH)
    assert (nxt.hour, nxt.minute) == (4, 15)


def test_pause_resume_archive(ag):
    a = ag.create(DEV, _base(trigger="cron", schedule="0 2 * * *"), activate=True)
    p = ag.set_status(DEV, a["id"], "paused")
    assert p["status"] == "paused" and p["next_run_at"] is None
    r = ag.set_status(DEV, a["id"], "active", from_session=True)
    assert r["status"] == "active" and r["next_run_at"]
    x = ag.set_status(DEV, a["id"], "archived")
    assert x["status"] == "archived"
    with pytest.raises(ag.AgentError, match="read-only"):
        ag.update(DEV, a["id"], {"model": "opus"})


def test_session_cannot_resume_a_never_approved_agent(ag):
    a = ag.create(DEV, _base())
    ag._write(a["id"], status="paused")
    with pytest.raises(ag.Forbidden):
        ag.set_status(DEV, a["id"], "active", from_session=True)


# ---- access ------------------------------------------------------------------------------
def test_owner_isolation_and_admin_sees_all(ag):
    mine = ag.create(DEV, _base())
    ag.create(DEV2, _base(name="other-agent"))
    assert [a["name"] for a in ag.list_agents(DEV)] == ["nightly-cve-audit"]
    assert len(ag.list_agents(ADMIN)) == 2
    assert ag.list_agents(VIEWER) == []
    with pytest.raises(ag.NotFound):
        ag.update(DEV2, mine["id"], {"model": "opus"})
    with pytest.raises(ag.NotFound):
        ag.approve(DEV2, mine["id"])
    with pytest.raises(ag.Forbidden):
        ag.create(VIEWER, _base(name="viewer-agent"))
    assert ag.approve(ADMIN, mine["id"])["approved_by"] == "admin@x.ch"


# ---- runs -------------------------------------------------------------------------------
def test_scheduled_occurrence_cannot_be_inserted_twice(ag):
    a = ag.create(DEV, _base(), activate=True)
    assert ag.enqueue_run(a["id"], "schedule", scheduled_for=1000.0)
    assert ag.enqueue_run(a["id"], "schedule", scheduled_for=1000.0) is None


def test_claim_is_atomic(ag):
    a = ag.create(DEV, _base(), activate=True)
    r = ag.request_run(DEV, a["id"])
    assert ag.claim_run(r["id"], "runner-a") is True
    assert ag.claim_run(r["id"], "runner-b") is False
    assert ag.get_run(r["id"])["runner"] == "runner-a"
    with pytest.raises(ag.AgentError, match="already queued or running"):
        ag.request_run(DEV, a["id"])


def test_redact_replaces_values_longest_first():
    import agents

    out = agents.redact("token=abcd1234XYZ and abcd1234",
                        {"LONG": "abcd1234XYZ", "SHORT": "abcd1234", "TINY": "ab"})
    assert out == "token=[secret:LONG] and [secret:SHORT]"


def test_deck_state_columns(ag):
    idle = ag.create(DEV, _base(name="idle-one"))
    pend = ag.create(DEV, _base(name="pend-one"), proposal=True)
    armed = ag.create(DEV, _base(name="armed-one", trigger="cron", schedule="0 2 * * *"),
                      activate=True)
    running = ag.create(DEV, _base(name="running-one"), activate=True)
    broken = ag.create(DEV, _base(name="broken-one"), activate=True)
    ag.request_run(DEV, running["id"])
    r = ag.request_run(DEV, broken["id"])
    ag.update_run(r["id"], status="failed", error="boom")
    deck = {a["name"]: a for a in ag.list_agents(DEV)}
    assert deck[idle["name"]]["deck"] == "idle"
    assert deck[pend["name"]]["deck"] == "idle" and deck[pend["name"]]["needs_approval"]
    assert deck[armed["name"]]["deck"] == "armed"
    assert deck[running["name"]]["deck"] == "running"
    assert deck[broken["name"]]["deck"] == "error"
    # a success clears the error
    r2 = ag.request_run(DEV, broken["id"])
    ag.update_run(r2["id"], status="succeeded")
    assert {a["name"]: a for a in ag.list_agents(DEV)}["broken-one"]["deck"] == "idle"


def test_nina_kb_unfolds_the_agents_section_for_an_agent_question():
    import assistant

    kb = assistant._kb_for("I want an agent that audits our dependencies every night")
    assert "Agents — l'onglet Crew" in kb and "sokkan-agent" in kb
    kb = assistant._kb_for("je veux créer un agent récurrent")
    assert "Agents — l'onglet Crew" in kb
