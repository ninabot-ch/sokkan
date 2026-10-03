"""memory/memory_migration.py — SOKKAN glue of the 2.x -> 3.0 migration (paths, config
translation, API view, approvals)."""
import json

import pytest


@pytest.fixture()
def mm(monkeypatch, tmp_path):
    import memory_migration as m
    for v in ("CORTHEXIS_MEMORY_PROFILE", "SOKKAN_MEMORY_PROFILE", "ML_SERVICE_URL",
              "CORTHEXIS_ML_SERVICE_URL", "SOKKAN_EMBED_MODEL", "CORTHEXIS_LEGACY_MODEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "auto")
    monkeypatch.setenv("CORTHEXIS_MIGRATION_DIR", str(tmp_path / "mig"))
    monkeypatch.setattr(m, "LEGACY_DB", tmp_path / "memory.db")
    monkeypatch.setenv("SOKKAN_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setattr(m, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(m, "start", lambda: None)
    return m


def test_config_translation(mm, monkeypatch):
    assert mm.config_translation()["profile"] == "leger"
    monkeypatch.setenv("ML_SERVICE_URL", "http://ml:8001")
    t = mm.config_translation()
    assert t["profile"] == "remote" and "ML_SERVICE_URL" in t["reason"]
    monkeypatch.setenv("SOKKAN_MEMORY_PROFILE", "standard")
    assert mm.config_translation()["profile"] == "standard"


def test_new_install_goes_straight_to_the_store(mm):
    assert mm.status()["serving"] == "store"      # no 2.x memory.db: nothing to migrate


def test_status_before_and_after(mm, tmp_path):
    (tmp_path / "memory.db").write_bytes(b"")     # a 2.x install
    st = mm.status()
    assert st["status"] == "pending" and st["serving"] == "memory.db (2.x)"
    assert st["steps_order"][0] == "archive" and st["plan"] is None
    d = tmp_path / "mig"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"status": "done", "steps": {}}))
    (d / "normalize-plan.txt").write_text("normalize plan (dry run)\n")
    st = mm.status()
    assert st["status"] == "done" and st["serving"] == "store"
    assert st["plan"].startswith("normalize plan")


def test_status_off_in_2x_mode(mm, monkeypatch):
    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "sqlite")
    assert mm.status()["status"] == "off" and not mm.active()


def test_approve_writes_its_own_file(mm, tmp_path):
    doc = mm.approve("normalize", "owner@example.org")
    assert doc["normalize"]["by"] == "owner@example.org"
    assert json.loads((tmp_path / "mig" / "approved.json").read_text())["normalize"]
    assert "override" in mm.approve("switch", "owner@example.org")
    with pytest.raises(ValueError):
        mm.approve("everything", "x")


def test_recall_reads_memory_db_during_the_migration(mm, monkeypatch, tmp_path):
    """Per-turn recall while the 2.x index serves: LegacyIndex + the 2.x embedder."""
    import json as _json
    import sqlite3

    import memory_search_server as mss
    import store_backend as sb
    from core import embed
    from core.recall import Recaller, RecallConfig

    db = tmp_path / "memory.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE notes (name TEXT PRIMARY KEY, description TEXT, type TEXT, mtime REAL,"
        " source_path TEXT, priority INTEGER DEFAULT 0);"
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, note_name TEXT, chunk_idx INTEGER,"
        " body TEXT, embedding TEXT);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
    for i, (n, body) in enumerate((("flour-supplier", "The mill delivers flour on Tuesday."),
                                   ("opening-hours", "Closed on Monday."))):
        con.execute("INSERT INTO notes VALUES (?,?,?,?,?,0)", (n, n, "project", 1.0, f"/m/{n}.md"))
        vec = [1.0, 0.0] if i == 0 else [0.0, 1.0]
        con.execute("INSERT INTO chunks(note_name, chunk_idx, body, embedding) VALUES (?,0,?,?)",
                    (n, body, _json.dumps(vec)))
    con.execute("INSERT INTO meta VALUES ('model', ?)", ("local:" + embed.LEGACY_MODEL,))
    con.commit()
    con.close()
    monkeypatch.setattr(mss, "DB_PATH", db)

    class Legacy:
        def identity(self):
            return "fastembed:paraphrase-multilingual-minilm-l12-v2@384"

        def identity_2x(self):
            return "local:" + embed.LEGACY_MODEL

        def embed_query(self, text, timeout=30):
            return [1.0, 0.0]

    monkeypatch.setattr(sb, "_legacy", Legacy())
    assert sb.migrating() and not sb.enabled()
    r = Recaller(sb.LegacyIndex(db), sb.LegacyQueryEmbedder(db), RecallConfig(threshold=0.5))
    res = r.recall("who delivers the flour and when", session_id="s1")
    assert [h.note_name for h in res.hits] == ["flour-supplier"]
    assert res.generation == 0 and "flour" in res.context
