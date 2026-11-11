"""doc_ingest — when passes run (the agents scheduler ticks them), where the documents may be
sent (a local model only), and how the feature is declared."""
import asyncio
import re
import time
from pathlib import Path

import httpx
import pytest

from docingest_helpers import FakeLLM, answer, write_sources

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(tmp_path / "memory"))
    for v in ("SOKKAN_DOC_INGEST_DIR", "SOKKAN_DOC_INGEST_CONFIG", "SOKKAN_MEMORY_QUARANTINE_DIR",
              "SOKKAN_DOC_INGEST_AUTO_RESUME", "SOKKAN_DOC_INGEST_ALLOW_REMOTE_LLM"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SOKKAN_DOC_INGEST_CONCURRENCY", "1")
    import audit
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    from docingest import config, scheduler, state
    state._ready.clear()
    scheduler._live.clear()
    docs = tmp_path / "docs"
    (docs / "pv").mkdir(parents=True)
    (docs / "pv/pv.md").write_text("# PV du comité\n\nSéance ordinaire du comité de direction.\n")
    write_sources(config.home(), [
        {"id": "nas", "kind": "folder", "root": str(docs), "allow": ["pv"],
         "schedule": "30 1 * * *", "timezone": "UTC"},
        {"id": "manuel", "kind": "folder", "root": str(docs), "allow": ["pv"]},
        {"id": "eteinte", "kind": "folder", "root": str(docs), "allow": ["pv"],
         "schedule": "30 1 * * *", "enabled": False}])
    started = []

    def fake_start(source_id, kind="manual", requested_by="", **kw):
        started.append((source_id, kind, requested_by))
        return {"started": True}
    monkeypatch.setattr(scheduler, "start_pass", fake_start)
    return {"started": started, "docs": docs, "tmp": tmp_path}


T0 = 1_794_000_000.0        # some instant; the cron is evaluated in UTC


def _next_0130(after):
    import cronexpr
    return cronexpr.parse("30 1 * * *").next_after(after, "UTC")


def test_a_scheduled_source_runs_once_at_its_time(world):
    from docingest import scheduler, state
    assert scheduler.tick(T0) == [] and world["started"] == []          # first tick: plans
    due = state.schedule_get("nas")["next_run_at"]
    assert due == _next_0130(T0)
    assert state.schedule_get("manuel") is None and state.schedule_get("eteinte") is None
    assert scheduler.tick(due - 1) == []
    assert scheduler.tick(due + 5) == ["nas"]
    assert world["started"] == [("nas", "incremental", "scheduler")]
    assert scheduler.tick(due + 20) == [], "one occurrence, one pass"
    assert state.schedule_get("nas")["next_run_at"] == _next_0130(due + 5)


def test_an_occurrence_missed_for_hours_is_not_caught_up_at_noon(world):
    from docingest import scheduler, state
    scheduler.tick(T0)
    due = state.schedule_get("nas")["next_run_at"]
    assert scheduler.tick(due + scheduler.MISFIRE_S + 60) == []
    assert state.schedule_get("nas")["next_run_at"] > due + scheduler.MISFIRE_S


def test_an_interrupted_pass_is_resumed_unless_a_person_stopped_it(world, monkeypatch):
    from docingest import scheduler, state
    p = state.pass_open("manuel", "default", "initial")
    state.pass_update(p["id"], status="interrupted", error="")
    assert scheduler.tick(T0) == ["manuel"]
    assert world["started"] == [("manuel", "incremental", "scheduler (resume)")]
    state.pass_update(p["id"], status="interrupted", error="stopped by admin@x")
    assert scheduler.tick(T0 + 30) == []
    # a pass left `running` by a dead process: resumed once its heartbeat is stale
    state.pass_update(p["id"], status="running", error="", heartbeat=T0)
    assert scheduler.tick(T0 + 60) == []
    assert scheduler.tick(T0 + state.STALE_S + 1) == ["manuel"]
    monkeypatch.setenv("SOKKAN_DOC_INGEST_AUTO_RESUME", "0")
    assert scheduler.tick(T0 + state.STALE_S + 30) == []


def test_real_start_stop_and_one_pass_per_source(world, monkeypatch):
    import threading

    from docingest import scheduler, state
    monkeypatch.undo()                 # the real start_pass …
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(world["tmp"] / "data"))
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(world["tmp"] / "memory"))
    monkeypatch.setenv("SOKKAN_DOC_INGEST_CONCURRENCY", "1")
    state._ready.clear()
    for i in range(4):
        (world["docs"] / f"pv/pv{i}.md").write_text(f"# PV numéro {i}\n\nSéance {i} du comité.\n")
    gate = threading.Event()

    def slow(doc, user):
        gate.wait(5)
        return answer(reference={"slug": "pv", "title": "PV", "description": "PV du comité",
                                 "body": "Compte rendu de la séance ordinaire du comité de direction."})
    scheduler.start_pass("manuel", "manual", "admin@x", client=FakeLLM(slow),
                         memory_neighbours=lambda *a: [])
    with pytest.raises(RuntimeError, match="already running"):
        scheduler.start_pass("manuel", "manual", "admin@x", client=FakeLLM())
    assert scheduler.stop_pass("manuel", "admin@x") is True
    gate.set()
    scheduler._live["manuel"]["thread"].join(10)
    p = state.pass_list(source="manuel")[0]
    assert p["status"] == "interrupted" and p["error"] == "stopped by admin@x"
    assert scheduler.stop_pass("manuel") is False
    scheduler._live.clear()


def test_the_agents_scheduler_ticks_it_even_without_claude_credentials(world, monkeypatch):
    """The nightly pass talks to the local model: an instance without Claude credentials
    (scheduler « held ») still runs it. Off = the agents loop never touches it."""
    import agents
    import agents_runtime
    from docingest import scheduler
    monkeypatch.setattr(agents, "DB", world["tmp"] / "agents.db")
    agents.init(force=True)
    seen = []
    monkeypatch.setattr(scheduler, "tick", lambda now=None: seen.append(now) or [])
    monkeypatch.setattr(agents_runtime, "credentials_ok", lambda: False)
    rt = agents_runtime.Runtime()
    monkeypatch.setenv("SOKKAN_FEATURE_DOC_INGEST", "0")
    asyncio.run(rt.tick(T0))
    assert seen == []
    monkeypatch.setenv("SOKKAN_FEATURE_DOC_INGEST", "1")
    monkeypatch.setenv("SOKKAN_FEATURE_AGENTS", "1")
    asyncio.run(rt.tick(T0))
    assert seen == [T0]
    monkeypatch.setattr(scheduler, "tick", lambda now=None: 1 / 0)
    asyncio.run(rt.tick(T0))            # a failure of the ingestion never breaks the agents


# ---- the documents only go to a local model ------------------------------------------------------
@pytest.mark.parametrize("url, local", [
    ("http://127.0.0.1:8004/v1", True), ("http://localhost:8004/v1", True),
    ("http://10.0.3.7:8004/v1", True), ("http://192.168.1.20/v1", True),
    ("http://100.124.110.23:8004/v1", True),          # CGNAT / Tailscale range
    ("http://[::1]:8004/v1", True),
    ("https://8.8.8.8/v1", False), ("https://1.1.1.1/v1", False),
    ("http://no-such-host.invalid/v1", False), ("", False),
])
def test_only_a_local_endpoint_gets_the_documents(world, monkeypatch, url, local):
    from docingest import llmclient
    monkeypatch.setenv("SOKKAN_DOC_INGEST_LLM_URL", url)
    monkeypatch.setenv("SOKKAN_DOC_INGEST_LLM_MODEL", "qwen")
    assert (llmclient.ready() is None) is local
    if not local and url:
        with pytest.raises(llmclient.NotLocal):
            llmclient.Client()
        from docingest import config, runner
        with pytest.raises(RuntimeError, match="private network"):
            runner.Pass(config.get("manuel")).run()         # no pass, nothing read
        monkeypatch.setenv("SOKKAN_DOC_INGEST_ALLOW_REMOTE_LLM", "1")   # explicit choice
        assert llmclient.ready() is None


def test_the_client_asks_for_strict_json_without_reasoning_and_counts_tokens(world, monkeypatch):
    from docingest import agent, llmclient
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        sent["url"], sent["body"] = str(request.url), json.loads(request.content)
        sent["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer())}}],
            "usage": {"prompt_tokens": 4321, "completion_tokens": 210}})
    c = llmclient.Client("http://127.0.0.1:9/v1", "qwen3.8-27b", key="k")
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    out, use = c.complete("sys", "user", agent.SCHEMA)
    assert out["verdict"] == "note" and use == {"tokens_in": 4321, "tokens_out": 210}
    b = sent["body"]
    assert sent["url"] == "http://127.0.0.1:9/v1/chat/completions" and sent["auth"] == "Bearer k"
    assert b["response_format"]["type"] == "json_schema"
    assert b["response_format"]["json_schema"]["strict"] is True
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in b and "tool_choice" not in b, "the model is given no tool"
    for content, finish, exc in (("pas du json", "stop", llmclient.LLMInvalid),
                                 ('{"verdict": "no', "length", llmclient.LLMTruncated),
                                 ("[1, 2]", "stop", llmclient.LLMInvalid)):
        c._http = httpx.Client(transport=httpx.MockTransport(lambda r, x=content, f=finish: httpx.Response(
            200, json={"choices": [{"finish_reason": f, "message": {"content": x}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}})))
        with pytest.raises(exc) as e:
            c.complete("s", "u", agent.SCHEMA)
        assert e.value.usage == {"tokens_in": 10, "tokens_out": 5}
    c._http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(llmclient.LLMError, match="500"):
        c.complete("s", "u", agent.SCHEMA)


def test_an_invalid_answer_is_retried_once_then_consigned_with_its_tokens(world, monkeypatch):
    from docingest import config, llmclient, runner, state
    n = {"calls": 0}

    def script(doc, user):
        n["calls"] += 1
        return llmclient.LLMInvalid("the model did not answer valid JSON",
                                    {"tokens_in": 500, "tokens_out": 50})
    p = runner.Pass(config.get("manuel"), client=FakeLLM(script), memory_neighbours=lambda *a: []).run()
    assert n["calls"] == 2 and p["status"] == "done" and p["counters"]["errors"] == 1
    assert p["counters"]["tokens_in"] == 1000 and p["counters"]["tokens_out"] == 100
    assert "valid JSON" in state.events(p["id"], "error")[0]["detail"]
    # an error is read again at the next pass (the file did not change, the outcome may)
    q = runner.Pass(config.get("manuel"), client=FakeLLM(), memory_neighbours=lambda *a: []).run()
    assert q["counters"]["noted"] == 1


# ---- the feature as declared ------------------------------------------------------------------------
def test_feature_is_off_by_default_with_its_dependencies(monkeypatch):
    import features
    f = features.BY_ID["doc_ingest"]
    assert f.defaults == {"community": False, "enterprise": False} and f.status == "experimental"
    assert f.requires == ("agents", "memory_quarantine") and f.doc == "docs/enterprise/DOC-INGEST.md"
    for ed in ("community", "enterprise"):
        assert features.enabled("doc_ingest", {"SOKKAN_EDITION": ed}) is False
    assert features.enabled("doc_ingest", {"SOKKAN_FEATURE_DOC_INGEST": "1"}) is True
    off = features.resolve({"SOKKAN_FEATURE_DOC_INGEST": "1", "SOKKAN_FEATURE_AGENTS": "0"})["doc_ingest"]
    assert off.enabled is False and off.problem and "requires `agents`" in off.reason


def test_every_variable_the_package_reads_is_declared_everywhere():
    """compose passes it, the registry lists it, the doc explains it."""
    read = set()
    for f in (ROOT / "backend" / "docingest").glob("*.py"):
        read |= set(re.findall(r"\"(SOKKAN_DOC_INGEST_[A-Z0-9_]+)\"", f.read_text()))
    assert len(read) >= 15
    import features
    declared = set(features.BY_ID["doc_ingest"].config)
    compose = (ROOT / "docker-compose.yml").read_text()
    doc = (ROOT / "docs" / "enterprise" / "DOC-INGEST.md").read_text()
    assert read == declared, sorted(read ^ declared)
    assert not [v for v in sorted(read) if f"{v}: ${{{v}:-}}" not in compose]
    assert not [v for v in sorted(read) if v not in doc]
    assert "SOKKAN_FEATURE_DOC_INGEST: ${SOKKAN_FEATURE_DOC_INGEST:-}" in compose


def test_the_api_route_is_project_scoped():
    import projectgate
    assert "/api/doc-ingest" in projectgate._PROJECT_PREFIXES


def test_nothing_in_the_package_can_write_to_a_source_or_call_out():
    """Static guard: no write-mode open on a source path, no network client besides the one
    in llmclient, no shell."""
    pkg = ROOT / "backend" / "docingest"
    for f in pkg.glob("*.py"):
        src = f.read_text()
        assert "shell=True" not in src and "os.system(" not in src, f.name
        if f.name != "llmclient.py":
            assert "httpx" not in src and "requests" not in src and "urllib.request" not in src, f.name
    for name in ("sources.py", "extract.py", "extract_worker.py", "runner.py", "agent.py"):
        src = (pkg / name).read_text()
        assert not re.search(r"open\([^)]*[\"'](?:w|a|r\+|wb|ab)[\"']", src), name
        assert ".write_text(" not in src and ".write_bytes(" not in src and "unlink" not in src, name
    assert time.time() > 0


def test_a_model_that_restarts_is_waited_for_then_the_pass_stops_without_blaming_documents(world):
    """Connection errors and 503 are retried with a backoff; if the model stays down the pass
    ends `interrupted` (auto-resumed later) and no document is marked as failed."""
    from docingest import agent, config, llmclient, runner, state
    n = {"calls": 0}

    def flaky(request):
        import json
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.ConnectError("refused")
        if n["calls"] == 2:
            return httpx.Response(503, text="loading")
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer(
                reference={"slug": "pv", "title": "PV", "description": "PV du comité",
                           "body": "Séance ordinaire du comité de direction, compte rendu."}))}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    waits = []
    c = llmclient.Client("http://127.0.0.1:9/v1", "qwen")
    c._http = httpx.Client(transport=httpx.MockTransport(flaky))
    c.sleep = waits.append
    out, _use = c.complete("s", "u", agent.SCHEMA)
    assert out["verdict"] == "note" and n["calls"] == 3 and waits == [5, 15]

    def down(request):
        raise httpx.ConnectError("refused")
    c._http = httpx.Client(transport=httpx.MockTransport(down))
    waits.clear()
    with pytest.raises(llmclient.LLMUnavailable):
        c.complete("s", "u", agent.SCHEMA)
    assert waits == list(llmclient.RETRY_WAITS)
    p = runner.Pass(config.get("manuel"), client=c, memory_neighbours=lambda *a: []).run()
    assert p["status"] == "interrupted" and "model unreachable" in p["error"]
    assert p["counters"]["errors"] == 0 and state.file_get("manuel", "pv/pv.md") is None
    assert [e["outcome"] for e in state.events(p["id"])] == ["deferred"]
    # the model is back: the SAME pass resumes and reads the document
    n["calls"] = 5
    c._http = httpx.Client(transport=httpx.MockTransport(flaky))
    q = runner.Pass(config.get("manuel"), client=c, memory_neighbours=lambda *a: []).run()
    assert q["id"] == p["id"] and q["status"] == "done" and q["counters"]["noted"] == 1
