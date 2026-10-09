"""3.1.1 — SOKKAN_AGENTS_INCIDENTS: a failed agent run opens its incident in Operate,
the links Operate ⇄ Crew, and an alert-triggered run linked back to its incident."""
import pytest

from test_agents_runtime import DEV, _agent, _drain, _run, env  # noqa: F401 — fixture

pytestmark = pytest.mark.usefixtures("model_credentials")  # 3.2: a configured instance


@pytest.fixture()
def obs(tmp_path, monkeypatch):
    import observability
    monkeypatch.setattr(observability, "DB", str(tmp_path / "incidents.db"))
    return observability


def _fail(env, obs, a):  # noqa: F811
    env["script"].clear()
    env["script"].update({"is_error": True, "text": "API error", "subtype": "error_during_execution"})
    env["agents"].request_run(DEV, a["id"])

    async def go():
        env["rt"].start_queued()
        await _drain(env["rt"])
    _run(go())
    return env["agents"].list_runs(DEV, a["id"])[0]


def _succeed(env, a):  # noqa: F811
    env["script"].clear()
    env["agents"].request_run(DEV, a["id"])

    async def go():
        env["rt"].start_queued()
        await _drain(env["rt"])
    _run(go())
    return env["agents"].list_runs(DEV, a["id"])[0]


def test_off_by_default_no_incident(env, obs, monkeypatch):  # noqa: F811
    monkeypatch.delenv("SOKKAN_AGENTS_INCIDENTS", raising=False)
    a = _agent(env["agents"], trigger="manual", schedule="")
    run = _fail(env, obs, a)
    assert run["status"] == "failed" and "incident" not in run["outputs"]
    assert obs.incidents() == []
    assert len(env["sent"]) == 1 and env["sent"][0][3] == "agent"  # the usual agent ping


def test_failure_opens_one_incident_then_joins_it_then_resolves(env, obs, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SOKKAN_AGENTS_INCIDENTS", "1")
    a = _agent(env["agents"], trigger="manual", schedule="")
    r1 = _fail(env, obs, a)
    incs = obs.incidents()
    assert len(incs) == 1
    i = incs[0]
    assert i["agent_id"] == a["id"] and i["agent_name"] == "nightly-cve-audit"
    assert i["runs"] == [r1["id"]] and i["status"] == "open" and i["occurrences"] == 1
    assert i["session_id"] == r1["session_id"] and "failed" in i["summary"]
    assert r1["outputs"]["incident"] == i["id"]
    # notified ONCE, through Operate's channel (kind alert), with a link to the incident
    assert len(env["sent"]) == 1 and env["sent"][0][3] == "alert"
    assert f"incident={i['id']}" in env["sent"][0][2]
    # second failure: same incident, no storm, no second incident ping
    r2 = _fail(env, obs, a)
    incs = obs.incidents()
    assert len(incs) == 1 and incs[0]["runs"] == [r1["id"], r2["id"]]
    assert incs[0]["occurrences"] == 2 and r2["outputs"]["incident"] == i["id"]
    assert [s[3] for s in env["sent"]].count("alert") == 1
    # next run fine → resolved
    _succeed(env, a)
    assert obs.incidents()[0]["status"] == "resolved"
    # a new failure after the resolution opens a NEW incident
    _fail(env, obs, a)
    assert len(obs.incidents()) == 2


@pytest.mark.parametrize("script,status", [
    ({"subtype": "error_max_budget_usd", "text": ""}, "budget"),
    ({"text": "Partial.\nDELIVERY: incomplete — feed down"}, "incomplete"),
])
def test_budget_opens_incomplete_does_not(env, obs, monkeypatch, script, status):  # noqa: F811
    monkeypatch.setenv("SOKKAN_AGENTS_INCIDENTS", "1")
    a = _agent(env["agents"], trigger="manual", schedule="", budget_usd=0.2, model="haiku")  # 3.4.5: priced, starts
    env["script"].update(script)
    env["agents"].request_run(DEV, a["id"])

    async def go():
        env["rt"].start_queued()
        await _drain(env["rt"])
    _run(go())
    assert env["agents"].list_runs(DEV, a["id"])[0]["status"] == status
    assert len(obs.incidents()) == (1 if status == "budget" else 0)


def test_one_incident_per_agent(env, obs, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SOKKAN_AGENTS_INCIDENTS", "1")
    a = _agent(env["agents"], trigger="manual", schedule="")
    b = _agent(env["agents"], name="pr-review", trigger="manual", schedule="")
    _fail(env, obs, a)
    _fail(env, obs, b)
    _fail(env, obs, a)
    by = {i["agent_name"]: i for i in obs.incidents()}
    assert set(by) == {"nightly-cve-audit", "pr-review"}
    assert by["nightly-cve-audit"]["occurrences"] == 2 and by["pr-review"]["occurrences"] == 1


def test_runs_by_incident_for_the_ops_link(env, obs):  # noqa: F811
    ag = env["agents"]
    a = _agent(ag, trigger="event", schedule="", event="alert:Postgres*")
    rid = obs.record_incident("PostgresSlowQueries", "p95 > 2 s", "warning")
    runs = env["rt"].fire_event("alert", "PostgresSlowQueries", {"incident": rid})
    by = ag.runs_by_incident(DEV, [rid])
    assert [r["id"] for r in by[rid]] == runs and by[rid][0]["agent_name"] == a["name"]
    assert by[rid][0]["agent_id"] == a["id"]
    # someone who cannot see the agent gets no link
    assert ag.runs_by_incident({"email": "other@x.ch", "role": "dev"}, [rid]) == {}
