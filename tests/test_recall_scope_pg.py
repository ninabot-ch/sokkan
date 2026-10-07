"""SOKKAN 3.2 lot 1 — project scope of the memory store against a real Postgres + pgvector.

Two projects share a corpus; every search path (exact, HNSW, lexical-only), the quoted-name
lookup and the note listing must keep to the scope, and a search WITHOUT a scope must give
exactly the 3.1 results. Also: migration 0011 puts the notes of an existing index in the
default project. Skipped unless SOKKAN_TEST_PG_DSN is set (see test_core_store_pg.py).
"""
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytest.importorskip("psycopg_pool")

from core import recall as R  # noqa: E402
from core.store import ChunkRecord, NoteRecord, SearchConfig, Store  # noqa: E402

from test_core_store_pg import DIM, _corpus  # noqa: E402

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")


@pytest.fixture()
def store():
    name = "sokkan_scope_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    s = Store(DSN.rsplit("/", 1)[0] + "/" + name)
    try:
        yield s
    finally:
        s.close()
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _with_project(rec: NoteRecord, project: str) -> NoteRecord:
    return NoteRecord(rec.name, rec.description, rec.type, rec.priority, rec.source_path,
                      rec.modified, rec.modified_source, rec.body, project)


def _load_two_projects(store, n_notes=60, seed=5):
    """Even notes → project 'radio', odd → 'tv': both projects cover every topic, so an
    unscoped search mixes them and a scoped one must not."""
    notes, queries = _corpus(seed=seed, n_notes=n_notes)
    tagged = [(_with_project(n, "radio" if i % 2 == 0 else "tv"), cs)
              for i, (n, cs) in enumerate(notes)]
    g = store.create_generation("test:model@32", DIM)
    store.upsert_notes(tagged, g.id)
    store.activate_generation(g.id)
    owner = {n.name: n.project for n, _ in tagged}
    return tagged, queries, owner


def test_migration_0011_puts_existing_notes_in_the_default_project(store):
    with store.pool.connection() as con:   # an index as 3.1 left it: no project column
        con.execute("ALTER TABLE notes DROP COLUMN project")
        con.execute("DELETE FROM schema_migrations WHERE version = 11")
        con.execute("INSERT INTO notes(name, description, body) VALUES ('old-note', 'd', 'b')")
    assert store.migrate() == [11]
    assert store.get_note("old-note").project == "default"
    assert store.note_names(None, project="default") == {"old-note"}
    assert store.note_names(None, project="radio") == set()


@pytest.mark.parametrize("config", [None, SearchConfig(exact_max_chunks=0, dense_candidates=40,
                                                       lexical_candidates=20)],
                         ids=["exact", "hnsw"])
def test_scoped_search_never_returns_another_project(store, config):
    tagged, queries, owner = _load_two_projects(store)
    kw = {"config": config} if config else {}
    mixed = 0
    for qv, text in queries:
        unscoped = store.search(qv, text, 10, **kw)
        mixed += {owner[h.note_name] for h in unscoped} == {"radio", "tv"}
        for proj in ("radio", "tv"):
            got = store.search(qv, text, 10, projects=[proj], **kw)
            assert got, (proj, text)
            assert {owner[h.note_name] for h in got} == {proj}
            assert all(h.project == proj for h in got)
        assert store.search(qv, text, 10, projects=[], **kw) == []
        both = store.search(qv, text, 10, projects=["radio", "tv"], **kw)
        assert [h.note_name for h in both] == [h.note_name for h in unscoped]
    assert mixed >= len(queries) // 2   # the corpus does mix the projects


def test_scoped_search_keeps_the_ranking_of_the_project(store):
    """Within the scope, the order is the unscoped order with the other project removed
    (exact path: candidates are the whole generation)."""
    _, queries, owner = _load_two_projects(store)
    for qv, text in queries:
        everything = [h.note_name for h in store.search(qv, text, 60)]
        radio = [h.note_name for h in store.search(qv, text, 60, projects=["radio"])]
        assert radio == [n for n in everything if owner[n] == "radio"][: len(radio)]


def test_lexical_only_search_is_scoped(store):
    _, queries, owner = _load_two_projects(store, seed=3)
    for _qv, text in queries:
        got = store.search(None, text, 10, projects=["tv"])
        assert {owner[h.note_name] for h in got} <= {"tv"}


def test_quoted_names_and_lookups_are_scoped(store):
    tagged, _q, owner = _load_two_projects(store)
    radio_name = next(n for n, p in owner.items() if p == "radio")
    tv_name = next(n for n, p in owner.items() if p == "tv")
    assert store.existing_names([radio_name, tv_name], projects=["radio"]) == {radio_name}
    assert store.existing_names([radio_name, tv_name], projects=[]) == set()
    assert store.existing_names([radio_name, tv_name]) == {radio_name, tv_name}
    assert store.get_note(tv_name).project == "tv"
    gen = store.active_generation()
    assert store.note_names(gen.id, project="tv") == {n for n, p in owner.items() if p == "tv"}


class _Emb:
    rerank_policy = "off"

    def __init__(self, vec):
        self.vec = vec

    def embed_query(self, text, timeout=30):
        return self.vec


def test_recaller_on_postgres_stays_in_the_session_project(store):
    tagged, queries, owner = _load_two_projects(store)
    tv_name = next(n for n, p in owner.items() if p == "tv")
    for qv, text in queries[:6]:
        r = R.Recaller(store, _Emb(qv), R.RecallConfig(threshold=0.0, top_k=6, min_words=1),
                       profile="test")
        res = r.recall(f"{text} {tv_name}", session_id=f"s-{uuid.uuid4().hex[:6]}",
                       projects=("radio",))
        assert res.notes and {owner[n] for n in res.notes} == {"radio"}
        assert tv_name not in res.forced
    # the recall log only holds what was injected: nothing of the other project
    with store.pool.connection() as con:
        logged = {r["note_name"] for r in con.execute("SELECT note_name FROM recall_log")}
    assert logged and {owner[n] for n in logged} == {"radio"}


def test_reindex_moves_a_note_between_projects(store):
    g = store.create_generation("t@4", 4)
    rec = NoteRecord("moving", "d", "project", 0, "/m/moving.md", None, "indexed", "b", "radio")
    store.upsert_note(rec, [ChunkRecord(0, "moving body words", [1.0, 0, 0, 0])], g.id)
    store.activate_generation(g.id)
    assert [h.note_name for h in store.search([1.0, 0, 0, 0], "moving", 5,
                                              projects=["radio"])] == ["moving"]
    store.upsert_note(_with_project(rec, "tv"), None, g.id)    # metadata-only upsert
    assert store.search([1.0, 0, 0, 0], "moving", 5, projects=["radio"]) == []
    assert store.get_note("moving").project == "tv"
