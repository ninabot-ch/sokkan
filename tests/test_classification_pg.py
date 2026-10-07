"""SOKKAN 3.4 « classification » against a real Postgres + pgvector: every search path
(exact, HNSW, lexical-only), the lookups (resolve / quoted names / listing / recall log)
keep to the (project, clearance) scope; an upsert never lowers a level; Store.set_level
does, with a floor; the access log is the audited recall. Skipped unless SOKKAN_TEST_PG_DSN
is set (see test_core_store_pg.py)."""
import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytest.importorskip("psycopg_pool")

import uuid  # noqa: E402

from core.store import NoteRecord, SearchConfig, Store  # noqa: E402

from test_core_store_pg import DIM, _corpus  # noqa: E402

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")


@pytest.fixture()
def store():
    name = "sokkan_cls_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    s = Store(DSN.rsplit("/", 1)[0] + "/" + name)
    try:
        yield s
    finally:
        s.close()
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _load(store, seed=5, n_notes=60):
    """Every third note of `radio` is confidential (3), every fifth restricted (4)."""
    notes, queries = _corpus(seed=seed, n_notes=n_notes)
    tagged, level = [], {}
    for i, (n, cs) in enumerate(notes):
        lv = 4 if i % 5 == 0 else 3 if i % 3 == 0 else 2
        tagged.append((NoteRecord(n.name, n.description, n.type, n.priority, n.source_path,
                                  n.modified, n.modified_source, n.body, "radio", lv), cs))
        level[n.name] = lv
    g = store.create_generation("test:model@32", DIM)
    store.upsert_notes(tagged, g.id)
    store.activate_generation(g.id)
    return tagged, queries, level


@pytest.mark.parametrize("config", [None, SearchConfig(exact_max_chunks=0, dense_candidates=40,
                                                       lexical_candidates=20)],
                         ids=["exact", "hnsw"])
def test_search_keeps_to_the_clearance(store, config, monkeypatch):
    from core import scope as S
    # the SQL alone must keep to the clearance (the Python post-filter is switched off here)
    monkeypatch.setattr(S, "filter_hits", lambda hits, scope: hits)
    _, queries, level = _load(store)
    kw = {"config": config} if config else {}
    above = 0
    for qv, text in queries:
        everything = store.search(qv, text, 10, **kw)
        above += any(level[h.note_name] > 2 for h in everything)
        for cap in (2, 3):
            got = store.search(qv, text, 10, projects=[f"radio@{cap}"], **kw)
            assert got and all(level[h.note_name] <= cap for h in got), (cap, text)
            assert all(h.level == level[h.note_name] for h in got)
        bare = store.search(qv, text, 10, projects=["radio"], **kw)
        assert all(level[h.note_name] <= 2 for h in bare)     # bare entry = default level
        full = store.search(qv, text, 10, projects=["radio@4"], **kw)
        assert [h.note_name for h in full] == [h.note_name for h in everything]
    assert above >= len(queries) // 2


def test_lexical_only_and_lookups_keep_to_the_clearance(store):
    tagged, queries, level = _load(store, seed=3)
    for _qv, text in queries:
        assert all(level[h.note_name] <= 3
                   for h in store.search(None, text, 10, projects=["radio@3"]))
    secret = next(n for n, lv in level.items() if lv == 4)
    plain = next(n for n, lv in level.items() if lv == 2)
    assert store.existing_names([secret, plain], projects=["radio@3"]) == {plain}
    assert store.resolve_note(secret, ["radio@3"]) is None
    assert store.resolve_note(secret, ["radio@4"]).level == 4
    listed = {n["name"]: n["level"] for n in store.list_notes(["radio@2"])}
    assert secret not in listed and listed[plain] == "project"
    hits = store.search(None, tagged[0][0].name, 3, projects=["radio@4"])
    store.log_recall("prompt", hits, session_id="s1", query="q")
    assert all(r["level"] <= 2 for r in store.recall_log(projects=["radio"]))
    assert {r["note_name"] for r in store.recall_log(projects=["radio@4"])} >= {
        h.note_name for h in hits}


def test_upsert_never_lowers_set_level_does_and_holds_a_floor(store):
    tagged, _q, level = _load(store)
    n, cs = next((n, cs) for n, cs in tagged if level[n.name] == 3)
    g = store.active_generation()
    lowered = NoteRecord(n.name, n.description, n.type, n.priority, n.source_path,
                         n.modified, n.modified_source, n.body + " edited", "radio", 2)
    store.upsert_notes([(lowered, cs)], g.id)
    assert store.note_level(n.name, "radio") == 3           # an edit of the file: no effect
    store.set_level(n.name, 2, project="radio", by="max@x", reason="published")
    assert store.note_level(n.name, "radio") == 2
    store.set_level("future-note", 4, project="radio", by="session", reason="inherited")
    fut = NoteRecord("future-note", "d", "project", 0, "/f.md", None, "indexed", "b", "radio", 2)
    store.upsert_notes([(fut, cs[:1])], g.id)
    assert store.note_level("future-note", "radio") == 4     # the floor written beforehand


def test_access_log_and_session_level(store):
    _, _q, level = _load(store)
    conf = next(n for n, lv in level.items() if lv == 3)
    assert store.session_level("s9") is None
    store.log_access("mcp", [{"note_name": conf, "project": "radio", "level": 3}],
                     actor="alice@x", session_id="s9", query="keys")
    hits = store.search(None, conf, 1, projects=["radio@4"])
    store.log_recall("spawn", hits, session_id="s8")
    assert store.session_level("s9") == 3
    rows = store.access_log(projects=["radio"])
    assert rows[-1]["via"] == "mcp" and rows[-1]["actor"] == "alice@x"
    assert {r["via"] for r in rows} == {"mcp", "spawn"}
    assert store.access_log(projects=["tv"]) == []


def test_migration_0013_puts_existing_notes_at_the_default_level(store):
    with store.pool.connection() as con:
        con.execute("ALTER TABLE notes DROP COLUMN level")
        con.execute("DELETE FROM schema_migrations WHERE version = 13")
        con.execute("INSERT INTO notes(name, description, body) VALUES ('old-note', 'd', 'b')")
    assert store.migrate() == [13]
    assert store.get_note("old-note").level == 2
