"""memory_search_server → 3.0 store bridge (memory/store_backend.py), with a fake store."""
import pytest

pytest.importorskip("psycopg")

from core.search import Hit  # noqa: E402
from core.store import DimensionMismatch  # noqa: E402


class FakeStore:
    def __init__(self, hits=None, dim_error=False):
        self.hits = hits if hits is not None else [
            Hit("deploy-note", 0.9, 0.95, 0.5, None, "deploy with the tunnel", 12, "indexed",
                description="how we deploy", source_path="/m/deploy-note.md", generation=3)]
        self.dim_error = dim_error
        self.calls = []

    def search(self, qv, text, k, rerank=None):
        self.calls.append((qv, text, k))
        if self.dim_error and qv is not None:
            raise DimensionMismatch("vector of dimension 2, generation expects 768")
        return self.hits

    def get_note(self, name):
        from core.contract import NoteRecord
        if name != "deploy-note":
            return None
        return NoteRecord("deploy-note", "d", "project", 0, "/m/deploy_note.md",
                          "2026-01-01T00:00:00+00:00", "migrated-mtime", "the body")

    def find_note_by_path(self, stem):
        return "deploy-note" if stem == "deploy_note" else None


@pytest.fixture()
def mem(monkeypatch):
    import memory_search_server as m
    import store_backend as sb

    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "postgres")
    fake = FakeStore()
    monkeypatch.setattr(sb, "_store", fake)
    monkeypatch.setattr(m, "_embed_query", lambda q: [1.0, 0.0])
    return m, fake


def test_flag_off_by_default(monkeypatch):
    import store_backend as sb
    monkeypatch.delenv("CORTHEXIS_MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("SOKKAN_MEMORY_BACKEND", raising=False)
    assert not sb.enabled()
    monkeypatch.setenv("SOKKAN_MEMORY_BACKEND", "postgres")
    assert sb.enabled()


def test_search_goes_through_the_store(mem):
    m, fake = mem
    res = m.memory_search("deploy tunnel", top_k=3)
    assert fake.calls == [([1.0, 0.0], "deploy tunnel", 3)]
    r = res[0]
    assert r["note_name"] == "deploy-note" and r["path"] == "deploy-note.md"
    assert r["age_days"] == 12 and r["date_source"] == "indexed" and r["generation"] == 3
    assert "degraded" not in r


def test_embedding_down_degrades_to_lexical(mem, monkeypatch):
    m, fake = mem

    def boom(_q):
        raise RuntimeError("down")

    monkeypatch.setattr(m, "_embed_query", boom)
    res = m.memory_search("deploy tunnel", top_k=2)
    assert fake.calls[-1][0] is None and "lexical-only" in res[0]["degraded"]
    assert "error" in m.memory_search("de la et", top_k=2)[0]


def test_dimension_mismatch_degrades(mem, monkeypatch):
    m, fake = mem
    fake.dim_error = True
    res = m.memory_search("deploy tunnel", top_k=2)
    assert "does not match the index" in res[0]["degraded"]


def test_empty_store_says_so(mem):
    m, fake = mem
    fake.hits = []
    assert m.memory_search("x y z", top_k=2)[0]["empty"] is True


def test_memory_get_via_store(mem):
    m, _ = mem
    out = m.memory_get("deploy_note")
    assert out.startswith("[note deploy-note — updated 2026-01-01") and "migrated-mtime" in out
    assert out.endswith("the body")
    assert m.memory_get("nope") == "note not found: nope"


# ------------------------------------------------------------- auto: 2.x serves until migrated
def _state(tmp_path, status):
    import json
    d = tmp_path / "memory-migration"
    d.mkdir(exist_ok=True)
    (d / "state.json").write_text(json.dumps({"status": status}))
    import os
    t = os.stat(d / "state.json").st_mtime + len(status)   # a new mtime for the cache
    os.utime(d / "state.json", (t, t))


def test_auto_serves_memory_db_until_the_migration_is_done(monkeypatch, tmp_path):
    import store_backend as sb
    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "auto")
    monkeypatch.setenv("CORTHEXIS_MIGRATION_DIR", str(tmp_path / "memory-migration"))
    assert sb.configured() == "auto" and not sb.enabled()       # no state yet
    for status, served in (("running", False), ("waiting", False), ("blocked", False),
                           ("done", True), ("not-needed", True)):
        _state(tmp_path, status)
        assert sb.enabled() is served, status


def test_legacy_query_refuses_another_model_than_memory_db(monkeypatch, tmp_path):
    import sqlite3

    import store_backend as sb
    from core import embed

    monkeypatch.setenv("CORTHEXIS_MEMORY_BACKEND", "auto")
    monkeypatch.setenv("CORTHEXIS_MIGRATION_DIR", str(tmp_path / "none"))
    db = tmp_path / "memory.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('model', 'remote:http://old-ml:8001')")
    con.commit()
    con.close()

    class Legacy:
        def identity_2x(self):
            return "local:" + embed.LEGACY_MODEL

        def embed_query(self, text, timeout=30):
            return [1.0]

    monkeypatch.setattr(sb, "_legacy", Legacy())
    with pytest.raises(RuntimeError, match="memory.db was built with remote:http://old-ml"):
        sb.embed_query("q", db)
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET value = ?", ("local:" + embed.LEGACY_MODEL,))
    con.commit()
    con.close()
    assert sb.embed_query("q", db) == [1.0]
