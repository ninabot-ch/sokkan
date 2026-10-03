"""CortHeXis tab API (backend/corthexis.py): graph, note, review, repairs with approval,
curation session, alerts. Real app, fictional corpus, 2.x SQLite index (default backend)."""
import json
import os
import sqlite3
import tempfile

import pytest

import review_fixture as rf

_TMP = tempfile.mkdtemp()
os.environ.setdefault("SOKKAN_DATA_DIR", _TMP)
os.environ.setdefault("CLAUDE_CONFIG_DIR", f"{_TMP}/claude")
os.environ.setdefault("SOKKAN_MEMORY_DIR", f"{_TMP}/memory")
os.environ.setdefault("SOKKAN_AGENT_CWD", _TMP)
os.environ.setdefault("SOKKAN_OWNER_EMAIL", "owner@localhost")
os.environ.setdefault("SOKKAN_UPDATE_CHECK", "0")
os.environ.setdefault("SOKKAN_LOCAL_TOKEN", "")


def _sqlite_index(db, mem):
    from core.review import load_corpus
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE notes(name TEXT PRIMARY KEY, description TEXT, type TEXT, "
                      "mtime REAL, source_path TEXT, priority INTEGER DEFAULT 0);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, note_name TEXT, chunk_idx "
                      "INTEGER, body TEXT, embedding TEXT);"
                      "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                      "CREATE TABLE links(src TEXT, dst TEXT, PRIMARY KEY(src, dst));")
    emb = rf.BowEmbedder()
    for n in load_corpus(mem):
        con.execute("INSERT INTO notes VALUES(?,?,?,?,?,0)",
                    (n.name, n.description, n.type, n.mtime, str(n.path)))
        con.execute("INSERT INTO chunks(note_name, chunk_idx, body, embedding) VALUES(?,0,?,?)",
                    (n.name, n.body, json.dumps(emb.embed_docs([n.name + " " + n.body])[0])))
    con.commit()
    con.close()


@pytest.fixture()
def cx(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import audit
    import board
    import corthexis
    import iam
    import memorykb
    import notify

    mem, repo = rf.build(tmp_path)
    db = tmp_path / "memory.db"
    _sqlite_index(db, mem)
    monkeypatch.setenv("SOKKAN_MEMORY_DIR", str(mem))
    monkeypatch.setenv("CORTHEXIS_DEAD_PATH_ROOTS", str(repo))
    monkeypatch.setenv("CORTHEXIS_DEAD_PATH_PREFIXES", "scripts,docs,infra")
    monkeypatch.setenv("CORTHEXIS_REVIEW_GIANT_WORDS", "600")
    monkeypatch.setenv("CORTHEXIS_REVIEW_CHAIN", "0")
    monkeypatch.delenv("CORTHEXIS_MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("SOKKAN_MEMORY_BACKEND", raising=False)
    monkeypatch.setattr(memorykb, "MEM_DB", db)
    monkeypatch.setattr(corthexis, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(corthexis, "PROPOSALS", tmp_path / "data" / "proposals.json")
    monkeypatch.setattr(corthexis, "BACKUPS", tmp_path / "data" / "backups")
    monkeypatch.setattr(corthexis, "_history", {"obj": None, "pg": None})
    monkeypatch.setattr(corthexis, "_state", {"report": None, "graph": None, "graph_key": None,
                                              "running": False})
    monkeypatch.setattr(corthexis, "reindex_hook", None)
    monkeypatch.setattr(notify, "hitl_enabled", lambda: False)
    monkeypatch.setattr(notify, "enabled", lambda: False)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    saved = {"iam": iam.DB, "board": board.DB}
    iam.DB, board.DB = tmp_path / "iam.db", tmp_path / "board.db"
    iam.init(force=True)
    board.init(force=True)
    try:
        yield TestClient(a.app), mem, corthexis
    finally:
        iam.DB, board.DB = saved["iam"], saved["board"]


def test_review_graph_and_note(cx):
    c, _mem, _cx = cx
    r = c.post("/api/corthexis/review/run").json()
    rep = r["report"]
    ids = {f["id"] for f in rep["findings"]}
    assert {"secrets", "stale_open", "broken_links", "near_duplicates", "naming"} <= ids
    assert "naming" in ids and r["history"][-1]["score"] == rep["score"]
    g = c.get("/api/corthexis/graph").json()
    by = {n["id"]: n for n in g["nodes"]}
    assert by["print-partner"]["level"] == "warn"
    assert by["api-credentials"]["level"] == "crit"
    assert by["studio-overview"]["in"] >= 10 and by["studio-overview"]["sx"] is not None
    assert any(gh["label"] == "onboarding-checklist" for gh in g["ghosts"])
    assert c.get(f"/api/corthexis/graph?since={g['version']}").json()["unchanged"]
    n = c.get("/api/corthexis/note/print-partner").json()
    assert [f["action"] for f in n["flags"]] == ["close"] and n["age"] is not None
    assert "studio-overview" in [o["resolved"] for o in n["out"]]
    assert c.get("/api/corthexis/note/nope").status_code == 404


def test_repair_needs_approval_and_is_audited(cx):
    c, mem, _cx = cx
    before = (mem / "pricing_2026.md").read_text()
    p = c.post("/api/corthexis/proposals", json={
        "kind": "relink", "target": "onboarding-checklist",
        "new_target": "client-onboarding-checklist"}).json()
    assert p["status"] == "pending" and "+Every new client" in p["changes"][0]["diff"]
    assert (mem / "pricing_2026.md").read_text() == before          # nothing written yet
    assert c.get("/api/corthexis/review").json()["pending"] == 1
    r = c.post(f"/api/corthexis/proposals/{p['id']}/approve").json()
    assert r["status"] == "applied" and r["written"] == ["pricing_2026.md"]
    assert "[[client-onboarding-checklist]]" in (mem / "pricing_2026.md").read_text()
    assert c.post(f"/api/corthexis/proposals/{p['id']}/approve").status_code == 409
    actions = [e["action"] for e in c.get("/api/audit?limit=20").json()]
    assert "memory.repair.propose" in actions and "memory.repair.apply" in actions


def test_refuse_and_conflict(cx):
    c, mem, _cx = cx
    p = c.post("/api/corthexis/proposals", json={"kind": "close", "note": "print-partner"}).json()
    assert c.post(f"/api/corthexis/proposals/{p['id']}/refuse").json()["status"] == "refused"
    assert "Closed on" not in (mem / "print_partner.md").read_text()
    p = c.post("/api/corthexis/proposals", json={"kind": "merge", "keep": "printer-setup",
                                                 "drop": "printer-configuration"}).json()
    (mem / "printer_setup.md").write_text((mem / "printer_setup.md").read_text() + "\nedit\n")
    r = c.post(f"/api/corthexis/proposals/{p['id']}/approve")
    assert r.status_code == 409 and "edited meanwhile" in r.json()["detail"]
    assert (mem / "printer_configuration.md").exists()
    statuses = {x["id"]: x["status"] for x in c.get("/api/corthexis/proposals").json()}
    assert statuses[p["id"]] == "conflict"


def test_bad_proposals(cx):
    c, _mem, _cx = cx
    assert c.post("/api/corthexis/proposals", json={"kind": "nope"}).status_code == 400
    assert c.post("/api/corthexis/proposals", json={"kind": "relink",
                                                    "target": "x"}).status_code == 400
    assert c.post("/api/corthexis/proposals", json={"kind": "rename",
                                                    "note": "missing"}).status_code == 400


def test_curation_session_is_preloaded(cx, monkeypatch):
    c, _mem, corthexis = cx
    seen = {}

    def spawn(tag, prompt, title="", user=""):
        seen.update(tag=tag, prompt=prompt, title=title)
        return {"session_id": "s1", "title": title}
    monkeypatch.setattr(corthexis, "spawn_hook", spawn)
    c.post("/api/corthexis/review/run")
    r = c.post("/api/corthexis/curation", json={"finding_ids": ["description_drift", "secrets"]})
    assert r.json()["session_id"] == "s1" and seen["title"] == "Memory curation"
    assert "Probable secret" in seen["prompt"] and "api-credentials" in seen["prompt"]
    assert rf.fake_key() not in seen["prompt"]
    assert "never copy a secret value" in seen["prompt"]
    r = c.post("/api/corthexis/curation", json={"finding_ids": ["nope"]})
    assert r.status_code == 400


def test_alerts_digest_then_quiet(cx, monkeypatch):
    _c, _mem, corthexis = cx
    import notify
    sent = []
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send", lambda t, b="", link="", kind="": sent.append(
        (t, b, link, kind)) or {"telegram": "ok"})
    monkeypatch.setenv("CORTHEXIS_REVIEW_DIGEST_AT", "00:00")
    corthexis.run(chain=False)
    assert len(sent) == 1 and "health" in sent[0][0] and sent[0][2].endswith("?tab=corthexis")
    assert rf.fake_key() not in sent[0][1]
    corthexis.run(chain=False)
    assert len(sent) == 1                          # same findings: no second message


def test_chain_check_from_the_session_context(cx, monkeypatch):
    """The handshake starts the real memory server exactly as agentchat does."""
    _c, _mem, corthexis = cx
    cfg = corthexis.chain_config()
    assert cfg.launch["args"][0].endswith("memory/memory_search_server.py")
    from core.review import chain_checks
    assert not [f for f in chain_checks(cfg) if f.id == "mcp_unreachable"]
