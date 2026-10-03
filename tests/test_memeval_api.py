"""API of the recall bench (CortHeXis → Bench) and of the Magnitude memory card
(backend/memeval.py), against a throw-away Postgres (SOKKAN_TEST_PG_DSN)."""
import json
import os
import tempfile
import time
import uuid

import pytest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("SOKKAN_DATA_DIR", _TMP)
os.environ.setdefault("CLAUDE_CONFIG_DIR", f"{_TMP}/claude")
os.environ.setdefault("SOKKAN_MEMORY_DIR", f"{_TMP}/memory")
os.environ.setdefault("SOKKAN_AGENT_CWD", _TMP)
os.environ.setdefault("SOKKAN_LOCAL_TOKEN", "")
os.environ.setdefault("SOKKAN_OWNER_EMAIL", "owner@localhost")
os.environ.setdefault("SOKKAN_UPDATE_CHECK", "0")

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import audit
    import board
    import iam

    saved = {"iam": iam.DB, "board": board.DB, "audit": audit.DB}
    iam.DB, board.DB, audit.DB = (tmp_path / "iam.db", tmp_path / "board.db",
                                  tmp_path / "audit.db")
    iam.init(force=True)
    board.init(force=True)
    monkeypatch.setenv("CORTHEXIS_MODELS_DIR", str(tmp_path / "models"))
    try:
        yield TestClient(a.app)
    finally:
        iam.DB, board.DB, audit.DB = saved["iam"], saved["board"], saved["audit"]


def test_without_the_3_0_store_the_bench_says_so(client, monkeypatch):
    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "sqlite")
    r = client.get("/api/memory/eval")
    assert r.status_code == 200 and r.json()["available"] is False
    assert client.get("/api/magnitude/memory/switch").json() == {"available": False}
    assert client.post("/api/memory/eval/run").status_code == 409
    assert client.get("/api/memory/eval/findings").json() == []


@pytest.fixture()
def pg(client, tmp_path, monkeypatch):
    """Store 3.0 filled with the café corpus of test_core_eval, bench and switch wired with
    the fake embedders."""
    if not DSN:
        pytest.skip("SOKKAN_TEST_PG_DSN not set (needs Postgres)")
    import memeval
    import store_backend
    from core import switch as sw
    from core.indexer import Indexer
    from test_core_eval import BowEmbedder, _corpus, _index_cfg

    name = "sokkan_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "postgres")
    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", DSN.rsplit("/", 1)[0] + "/" + name)
    monkeypatch.setattr(store_backend, "_store", None)
    st = store_backend.get_store()
    d = _corpus(tmp_path / "memory")
    Indexer(st, BowEmbedder(), _index_cfg(d), log=lambda m: None).run()
    monkeypatch.setattr(memeval, "embedder", lambda: BowEmbedder())
    monkeypatch.setattr(memeval, "paraphraser", lambda: None)
    monkeypatch.setattr(memeval, "_switcher", sw.Switcher(
        st, index_config=lambda: _index_cfg(d), embedder_factory=lambda t: BowEmbedder(),
        check=lambda e: [], min_questions=3))
    try:
        yield st
    finally:
        st.close()
        store_backend._store = None
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _wait(fn, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise TimeoutError


def test_questions_runs_and_audit(client, pg):
    import audit

    assert client.post("/api/memory/eval/questions",
                       json={"question": "where is the guest wifi code", "notes": ["nope"]}
                       ).status_code == 400
    for q, n in [("where is the guest wifi code", "wifi-password"),
                 ("when do the boats come in", "tide-tables"),
                 ("what is the roast of the house blend", "coffee-roaster")]:
        r = client.post("/api/memory/eval/questions", json={"question": q, "notes": [n]})
        assert r.status_code == 200 and r.json()["source"] == "client"
    qs = client.get("/api/memory/eval/questions").json()
    assert len(qs) == 3
    assert client.post(f"/api/memory/eval/questions/{qs[0]['id']}/status",
                       json={"status": "disabled"}).status_code == 200
    assert client.post("/api/memory/eval/run").json() == {"started": True}
    ov = _wait(lambda: (lambda o: o if o["last"] else None)(client.get("/api/memory/eval").json()))
    assert ov["available"] and ov["last"]["n_questions"] == 2
    assert ov["last"]["metrics"]["overall"]["mrr"] > 0.5
    assert ov["questions"]["client"] == {"active": 2, "disabled": 1}
    assert client.post("/api/memory/eval/generate").status_code == 409     # no LLM
    actions = [e["action"] for e in audit.recent()]
    assert "memory.eval.question" in actions and "memory.eval.run" in actions
    assert "memory.eval.question.disabled" in actions


def test_harvest_reads_the_session_transcripts(client, pg):
    import app as a

    a.PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "sessionId": "s9", "message": {"content": "olive trees: how often?"}},
        {"type": "assistant", "sessionId": "s9", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__sokkan-memory__memory_get",
             "input": {"note_name": "garden-watering"}}]}},
    ]
    (a.PROJECT_DIR / "s9.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    rep = client.post("/api/memory/eval/harvest").json()
    assert rep["new"] == 1
    qs = client.get("/api/memory/eval/questions?source=transcript").json()
    assert qs[0]["question"] == "olive trees: how often?"
    assert qs[0]["expected"] == ["garden-watering"]


def test_profile_switch_rollback_and_licence_are_audited(client, pg, tmp_path):
    import audit
    from core import eval as ev

    for q, n in [("where is the guest wifi code", "wifi-password"),
                 ("when do the boats come in", "tide-tables"),
                 ("what is the roast of the house blend", "coffee-roaster")]:
        ev.add_question(pg, q, [n])
    st = client.get("/api/magnitude/memory/switch").json()
    assert st["available"] and st["active_generation"] and st["job"] is None
    r = client.post("/api/magnitude/memory/switch", json={"profile": "turbo"})
    assert r.status_code == 409
    job = client.post("/api/magnitude/memory/switch",
                      json={"profile": "gpu", "urls": ["http://gpu-node:8080"]}).json()
    done = _wait(lambda: (lambda j: j if j["status"] not in ("building", "evaluating")
                          else None)(client.get("/api/magnitude/memory/switch").json()["job"]))
    assert done["id"] == job["id"] and done["status"] == "switched"
    st = client.get("/api/magnitude/memory/switch").json()
    assert st["current"]["profile"] == "gpu" and st["rollback"]
    rb = client.post("/api/magnitude/memory/rollback")
    assert rb.status_code == 200 and rb.json()["kind"] == "rollback"
    assert client.post("/api/magnitude/memory/rollback").status_code == 409

    bad = client.post("/api/magnitude/memory/licence", json={"decision": "maybe"})
    assert bad.status_code == 400
    r = client.post("/api/magnitude/memory/licence",
                    json={"decision": "accepted", "download": False})
    assert r.status_code == 200 and r.json()["licence"]["decision"] == "accepted"
    lic = json.loads((tmp_path / "models" / "licence.json").read_text())
    assert lic["gemma"]["by"] == "owner@localhost" and lic["gemma"]["via"] == "ui"
    actions = [e["action"] for e in audit.recent()]
    for a in ("memory.profile.switch", "memory.profile.rollback", "memory.licence.gemma.accepted"):
        assert a in actions
