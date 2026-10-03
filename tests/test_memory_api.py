"""/api/memory/* of the 3.0 store: hook endpoint (token), recall log, status, notes."""
import os
import tempfile
import time

import pytest

pytest.importorskip("psycopg")

_TMP = tempfile.mkdtemp()
os.environ.update(
    SOKKAN_DATA_DIR=_TMP, CLAUDE_CONFIG_DIR=f"{_TMP}/claude",
    SOKKAN_MEMORY_DIR=f"{_TMP}/memory", SOKKAN_AGENT_CWD=_TMP,
    SOKKAN_LOCAL_TOKEN="", SOKKAN_OWNER_EMAIL="owner@localhost",
    SOKKAN_UPDATE_CHECK="0",
)

from test_recall_pg import DSN, BowEmbedder, corpus, dsn, indexed  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")


@pytest.fixture()
def client(indexed, monkeypatch, tmp_path):  # noqa: F811
    from fastapi.testclient import TestClient

    import app as a
    import board
    import iam
    import memrecall
    import store_backend as sb

    store, runner, _c = indexed
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("CORTHEXIS_RECALL_THRESHOLD", "0")
    monkeypatch.setattr(sb, "_store", store)
    monkeypatch.setattr(sb, "_qemb", (time.monotonic() + 3600, 1, BowEmbedder()))
    monkeypatch.setattr(a, "_index_runner", runner)
    memrecall.reset()
    saved = {"iam": iam.DB, "board": board.DB}
    iam.DB, board.DB = tmp_path / "iam.db", tmp_path / "board.db"
    iam.init(force=True)
    board.init(force=True)
    try:
        yield TestClient(a.app), store
    finally:
        iam.DB, board.DB = saved["iam"], saved["board"]
        memrecall.reset()


def test_hook_endpoint_needs_the_token(client):
    import memrecall

    c, store = client
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "tmux-1",
               "prompt": "Kartonage Weiss cardboard packaging boxes"}
    assert c.post("/api/memory/hook", json=payload).status_code == 401
    assert c.post("/api/memory/hook", json=payload,
                  headers={"x-sokkan-hook-token": "nope"}).status_code == 401
    r = c.post("/api/memory/hook", json=payload,
               headers={"x-sokkan-hook-token": memrecall.hook_token()})
    assert r.status_code == 200
    assert "packaging-supplier" in r.json()["hookSpecificOutput"]["additionalContext"]
    assert store.recalled_notes("tmux-1")
    assert oct(os.stat(memrecall.token_path()).st_mode)[-3:] == "600"


def test_recall_log_status_and_notes(client):
    import memrecall

    c, store = client
    c.post("/api/memory/hook", headers={"x-sokkan-hook-token": memrecall.hook_token()},
           json={"hook_event_name": "UserPromptSubmit", "session_id": "s2",
                 "prompt": "Kartonage Weiss cardboard packaging boxes"})
    log = c.get("/api/memory/recall-log", params={"session": "s2"}).json()
    assert log["entries"] and all(e["session_id"] == "s2" for e in log["entries"])
    assert log["summary"]["by_channel"][0]["channel"] == "prompt"
    st = c.get("/api/memory/status").json()
    assert st["backend"] == "postgres" and st["index"]["notes"] > 20
    assert st["indexer"]["runs"] == 1
    notes = c.get("/api/memory/notes").json()
    assert any(n["name"] == "packaging-supplier" for n in notes)
    assert c.get("/api/memory/stats").json()["generation"]
    res = c.get("/api/memory/search", params={"q": "Kartonage Weiss cardboard"}).json()
    assert res[0]["note_name"] == "packaging-supplier"
    assert c.post("/api/memory/index").status_code == 200
