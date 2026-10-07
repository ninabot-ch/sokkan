"""3.1.1 — the Crew of the public demo: seed (idempotent, no copy-paste) and the
simulated runtime (a card breathes, nothing calls a model)."""
import asyncio
import copy

import pytest

DEMO_OWNER = "maya.ops@example.com"


def _spec():
    base = dict(tools=["Read", "Grep"], owner=DEMO_OWNER)
    return {"agents": [
        {**base, "name": "nightly-cve-audit", "status": "active", "trigger": "cron",
         "schedule": "0 2 * * *", "secrets": ["GITHUB_TOKEN"],
         "purpose": "Audit the dependencies for known vulnerabilities.",
         "deliverable": "A table of vulnerable packages.",
         "done_criteria": "Every direct dependency has been checked.",
         "runs": [{"occurrence": k, "status": "succeeded", "duration_s": 120, "cost_usd": 0.2,
                   "tokens_in": 1000, "tokens_out": 100, "num_turns": 4,
                   "deliverable": "Clean.\nDELIVERY: done"} for k in (2, 1, 0)]},
        {**base, "name": "pr-review", "status": "active", "trigger": "cron",
         "schedule": "*/30 * * * *", "loop": True,
         "purpose": "Review the open pull requests labelled needs-review.",
         "deliverable": "Per PR: a verdict and a draft review.",
         "done_criteria": "Every labelled PR has a draft review or a skipped line.",
         "live": {"duration_s": 60, "cost_usd": 0.4, "tokens_in": 5000, "tokens_out": 300,
                  "num_turns": 9, "deliverable": "2 PRs reviewed.\nDELIVERY: done",
                  "steps": [{"at": 1, "kind": "tool", "tool": "Bash", "input": "gh pr list"}]},
         "runs": [{"ago_h": 1, "status": "succeeded", "duration_s": 60,
                   "deliverable": "1 PR reviewed.\nDELIVERY: done"}]},
        {**base, "name": "staging-smoke-check", "status": "active", "trigger": "event",
         "event": "alert:Staging*", "purpose": "Run the smoke suite on staging.",
         "deliverable": "A pass/fail table per check.",
         "done_criteria": "All 12 checks have a result.",
         "runs": [{"ago_h": 2, "status": "incomplete", "duration_s": 300,
                   "error": "3 checks could not run", "deliverable": "partial"}]},
        {**base, "name": "release-notes-drafter", "status": "pending", "trigger": "manual",
         "created_by": "session:" + "e" * 32, "purpose": "Draft the release notes.",
         "deliverable": "A CHANGELOG section.", "done_criteria": "Every merged PR is listed.",
         "runs": []},
    ]}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agentchat
    import agents
    import audit
    monkeypatch.setattr(agents, "DB", tmp_path / "agents.db")
    agents.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")

    def no_session(*a, **k):
        raise AssertionError("the demo runtime must never open an SDK session")
    monkeypatch.setattr(agentchat, "get_or_create", no_session)
    import demo_crew
    return demo_crew


def _deck():
    import agents
    return {a["name"]: a["deck"] for a in agents.list_agents({"email": "", "role": "owner"})}


def test_only_on_the_demo(env, monkeypatch):
    monkeypatch.delenv("SOKKAN_DEMO_CREW", raising=False)
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "1")
    assert not env.enabled()
    monkeypatch.setenv("SOKKAN_DEMO_CREW", "1")
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "0")
    assert env.requested() and not env.enabled()  # not the demo → ignored
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "1")
    assert env.enabled()
    import agents_runtime
    assert agents_runtime.demo_mode()


def test_runtime_start_picks_the_demo_runtime_only_on_the_demo(env, monkeypatch):
    import agents_runtime
    monkeypatch.setenv("SOKKAN_DEMO_CREW", "1")
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "0")

    async def go():
        rt = agents_runtime.start()
        kind = type(rt).__name__
        await rt.stop()
        return kind
    assert asyncio.new_event_loop().run_until_complete(go()) == "Runtime"
    monkeypatch.setenv("SOKKAN_DEMO_BANNER", "1")
    assert asyncio.new_event_loop().run_until_complete(go()) == "DemoRuntime"


def test_seed_covers_the_deck_and_is_idempotent(env):
    import agents
    rep = env.seed(_spec())
    assert {r["name"] for r in rep} == {"nightly-cve-audit", "pr-review", "staging-smoke-check",
                                        "release-notes-drafter"}
    deck = _deck()
    assert deck["nightly-cve-audit"] == "armed" and deck["staging-smoke-check"] == "error"
    assert deck["release-notes-drafter"] == "idle"
    a = agents.get_by_name("release-notes-drafter")
    assert a["status"] == "pending" and a["created_by"].startswith("session:")
    cve = agents.get_by_name("nightly-cve-audit")
    runs = agents.list_runs({"email": "", "role": "owner"}, cve["id"])
    assert len(runs) == 3 and all(r["scheduled_for"] for r in runs)
    # cron history sits on the cron occurrences, oldest first
    assert sorted(r["scheduled_for"] for r in runs) == [r["scheduled_for"] for r in reversed(runs)]
    n_agents = len(agents.list_agents({"email": "", "role": "owner"}))
    env.seed(_spec())
    env.seed(_spec())
    assert len(agents.list_agents({"email": "", "role": "owner"})) == n_agents
    assert len(agents.list_runs({"email": "", "role": "owner"}, cve["id"])) == 3
    assert _deck() == deck


def test_seed_refuses_a_copy_pasted_done_criteria(env):
    spec = _spec()
    spec["agents"][1]["done_criteria"] = spec["agents"][0]["done_criteria"]
    with pytest.raises(env.SeedError, match="copy-paste"):
        env.check(spec)
    spec = _spec()
    spec["agents"][1]["done_criteria"] = ""
    with pytest.raises(env.SeedError, match="done_criteria"):
        env.check(spec)


def test_seed_never_takes_a_secret_value(env):
    spec = _spec()
    spec["agents"][0]["secrets"] = ["ghp_abcdefghijklmnopqrstuvwxyz0123"]
    with pytest.raises(env.SeedError, match="NAMES"):
        env.check(spec)


def test_seed_keeps_real_agents_alone(env):
    import agents
    real = agents.create({"email": "dev@x.ch", "role": "dev"},
                         {"name": "my-own-agent", "purpose": "p", "deliverable": "d"})
    run = agents.enqueue_run(real["id"], "manual", "dev@x.ch")
    env.seed(_spec())
    assert agents.get(real["id"])["name"] == "my-own-agent" and agents.get_run(run["id"])


def test_demo_runtime_loop_breathes_without_a_model(env):
    import agents
    env.seed(_spec())
    rt = env.DemoRuntime()
    t0 = 1_900_000_000.0
    pr = agents.get_by_name("pr-review")
    rt.step(t0)
    live = agents.active_run(pr["id"])
    assert live and live["runner"] == env.SIM_RUNNER and live["context"]["simulated"]
    assert live["context"]["steps"][0]["tool"] == "Bash"
    assert _deck()["pr-review"] == "running"
    rt.step(t0 + 30)  # still going
    assert agents.active_run(pr["id"])["id"] == live["id"]
    rt.step(t0 + 61)  # done → succeeded with the recorded deliverable, and a new one starts
    done = agents.get_run(live["id"])
    assert done["status"] == "succeeded" and done["deliverable"].startswith("2 PRs reviewed")
    assert 0.3 < done["cost_usd"] < 0.5 and done["tokens_in"] > 0
    nxt = agents.active_run(pr["id"])
    assert nxt and nxt["id"] != live["id"]
    assert _deck()["pr-review"] == "running"


def test_demo_runtime_fires_due_crons_as_simulated_runs(env):
    import agents
    env.seed(_spec())
    rt = env.DemoRuntime()
    cve = agents.get_by_name("nightly-cve-audit")
    occ = cve["next_run_at"]
    before = len(agents.list_runs({"email": "", "role": "owner"}, cve["id"]))
    rt.step(occ + 1)
    live = agents.active_run(cve["id"])
    assert live and live["scheduled_for"] == occ and live["context"]["simulated"]
    assert _deck()["nightly-cve-audit"] == "running"
    assert agents.get(cve["id"])["next_run_at"] > occ
    rt.step(occ + 1 + 200)
    runs = agents.list_runs({"email": "", "role": "owner"}, cve["id"])
    assert len(runs) == before + 1 and runs[0]["status"] == "succeeded"
    assert runs[0]["deliverable"].startswith("Clean.")
    assert _deck()["nightly-cve-audit"] == "armed"


def test_demo_runtime_cancels_real_runs_and_ignores_alerts(env):
    import agents
    env.seed(_spec())
    rt = env.DemoRuntime()
    smoke = agents.get_by_name("staging-smoke-check")
    r = agents.enqueue_run(smoke["id"], "manual", "admin@x.ch")
    assert rt.fire_event("alert", "StagingDeployFinished") == []
    rt.step(1_900_000_000.0)
    assert agents.get_run(r["id"])["status"] == "cancelled"
    assert _deck()["staging-smoke-check"] == "error"


def test_demo_runtime_prunes_its_history(env, monkeypatch):
    import agents
    monkeypatch.setattr(env, "KEEP_RUNS", 5)
    spec = copy.deepcopy(_spec())
    spec["agents"][1]["live"]["duration_s"] = 1
    env.seed(spec)
    rt = env.DemoRuntime()
    t = 1_900_000_000.0
    for i in range(20):
        rt.step(t + i * 2)
    pr = agents.get_by_name("pr-review")
    assert len(agents.list_runs({"email": "", "role": "owner"}, pr["id"])) <= 6
