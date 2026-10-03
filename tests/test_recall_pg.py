"""Recall + indexer runner + SOKKAN wiring against a real Postgres (fictional corpus).

The embedder is a deterministic bag-of-words hash (no model server): it tests the wiring
(store writes, generations, recall_log, recall_turns, dedup across processes, hooks), not
the ranking quality — that is ``core.bench_recall`` with the real models.
"""
import hashlib
import math
import os
import time
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")

from core import bench_recall as B  # noqa: E402
from core import recall as R  # noqa: E402
from core.indexer import IndexConfig, IndexRunner, corpus_signature  # noqa: E402
from core.search import tokens  # noqa: E402
from core.store import Store  # noqa: E402

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")


class BowEmbedder:
    """Hashed bag of folded words, 64 dims: shared words → close vectors."""
    rerank_policy = "off"
    lexical_weight = 0.3
    dim = 64

    def identity(self):
        return "test:bow@64"

    def _vec(self, text):
        v = [0.0] * self.dim
        for t in tokens(text):
            h = int(hashlib.md5(t.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 8) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v] if any(v) else [1.0] + [0.0] * (self.dim - 1)

    def embed_docs(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text, timeout=30):
        return self._vec(text)

    def rerank(self, q, docs, timeout=3):
        return None


@pytest.fixture()
def dsn():
    name = "sokkan_recall_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    yield DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.fixture()
def corpus(tmp_path):
    return B.write_corpus(tmp_path / "memory")


@pytest.fixture()
def indexed(dsn, corpus):
    store = Store(dsn)
    runner = IndexRunner(lambda: store, BowEmbedder, IndexConfig(memory_dir=corpus),
                         log=lambda m: None)
    rep = runner.run_once()
    assert rep is not None, runner.last_error
    try:
        yield store, runner, corpus
    finally:
        store.close()


def test_indexer_writes_the_store_with_the_model_blend(indexed):
    store, runner, corpus = indexed
    g = store.active_generation()
    assert g.embed_identity == "test:bow@64" and g.lexical_weight == pytest.approx(0.3)
    names = store.note_names(g.id)
    assert names == set(B.FACTS) | set(B.DISTRACTORS)
    assert (corpus / "MEMORY.md").exists()               # the index file is generated
    st = runner.status()
    assert st["runs"] == 1 and st["last"]["reindexed"] == len(names)
    assert store.stats()["notes"] == len(names) and store.stats()["last_mtime"]
    listed = {n["name"]: n for n in store.list_notes()}
    assert listed["mailbox-access-clara"]["chunks"] == 1 and listed["mailbox-access-clara"]["mtime"]


def test_runner_sees_file_changes(indexed):
    store, runner, corpus = indexed
    assert not runner.changed()
    p = corpus / "packaging_supplier.md"
    p.write_text(p.read_text().replace("Kartonage Weiss", "Kartonage Huber"))
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert runner.changed()
    rep = runner.run_once()
    assert rep.reindexed == 1 and "Huber" in store.get_note("packaging-supplier").body
    (corpus / "shop_opening_hours.md").unlink()
    assert runner.changed()
    assert runner.run_once().pruned == 1
    assert store.get_note("shop-opening-hours") is None


def test_runner_normalizes_at_indexing(indexed):
    store, runner, corpus = indexed
    p = corpus / "Coffee Grinder.md"
    p.write_text("---\nname: Coffee Grinder\ndescription: grinder settings\n---\n\nFine.\n")
    old = time.time() - 3600
    os.utime(p, (old, old))
    rep = runner.run_once()
    assert rep.normalize is not None and rep.normalize.changes
    assert (corpus / "coffee_grinder.md").exists() and store.get_note("coffee-grinder")


def test_recall_logs_and_dedups_across_processes(indexed, dsn):
    store, _r, _c = indexed
    r = R.Recaller(store, BowEmbedder(), R.RecallConfig(threshold=0.0, top_k=2), profile="leger")
    q = "Kartonage Weiss cardboard packaging boxes order"
    first = r.recall(q, session_id="sess-1")
    assert "packaging-supplier" in first.notes
    # another process (a command hook) sees what was injected: no second injection
    other = Store(dsn, migrate=False)
    try:
        r2 = R.Recaller(other, BowEmbedder(), R.RecallConfig(threshold=0.0, top_k=2))
        again = r2.recall(q, session_id="sess-1")
        assert "packaging-supplier" not in again.notes
        assert "packaging-supplier" in again.deduplicated
        assert store.recalled_notes("sess-1") >= set(first.notes)
    finally:
        other.close()
    rows = store.recall_log(session_id="sess-1")
    assert {x["note_name"] for x in rows} >= set(first.notes)
    assert all(x["generation_id"] == store.active_generation().id for x in rows)
    assert rows[0]["content_hash"]                          # version of the injected note
    groups = store.recall_summary()["by_channel"]       # one group per (channel, profile)
    assert {g["channel"] for g in groups} == {"prompt"}
    assert sum(g["turns"] for g in groups) == 2 and sum(g["recalled"] for g in groups) >= 1
    assert all(g["p50_ms"] is not None for g in groups)


def test_subagent_recall_is_logged_apart(indexed):
    store, _r, _c = indexed
    r = R.Recaller(store, BowEmbedder(), R.RecallConfig(threshold=0.0, top_k=2))
    r.recall("Kartonage Weiss cardboard packaging boxes", session_id="s")
    out = R.hook_output({"hook_event_name": "PreToolUse", "tool_name": "Task",
                         "tool_use_id": "toolu_1", "session_id": "s",
                         "tool_input": {"prompt": "Reorder Kartonage Weiss cardboard boxes",
                                        "description": "packaging"}}, r)
    prompt = out["hookSpecificOutput"]["updatedInput"]["prompt"]
    assert "packaging-supplier" in prompt          # the parent already had it: not deduped
    rows = store.recall_log(session_id="s")
    assert {x["channel"] for x in rows} == {"prompt", "subagent"}
    assert any(x["agent_id"] == "toolu_1" for x in rows)


def test_existing_names_and_quoted_note(indexed):
    store, _r, _c = indexed
    assert store.existing_names(["render-node-gpu", "nope-nope"]) == {"render-node-gpu"}
    r = R.Recaller(store, BowEmbedder(), R.RecallConfig(threshold=0.99))
    res = r.recall("please read render-node-gpu before touching anything there", session_id="q")
    assert res.notes[0] == "render-node-gpu"


def test_sokkan_bridge_and_hooks(indexed, monkeypatch, tmp_path):
    """memory_search / memory_get / memory_links / the SDK hook core through the store."""
    import memrecall
    import memory_search_server as m
    import store_backend as sb

    store, _r, _c = indexed
    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", "postgresql://unused")
    monkeypatch.delenv("CORTHEXIS_MEMORY_BACKEND", raising=False)
    monkeypatch.setattr(sb, "_store", store)
    monkeypatch.setattr(sb, "_qemb", (time.monotonic() + 3600, 1, BowEmbedder()))
    assert sb.enabled() and memrecall.active()
    res = m.memory_search("Kartonage Weiss cardboard packaging", top_k=3)
    assert res[0]["note_name"] == "packaging-supplier" and res[0]["generation"]
    body = m.memory_get("packaging-supplier")
    assert body.startswith("[note packaging-supplier — updated 2026-03-") and "Kartonage" in body
    assert "reconstructed" not in body                         # frontmatter date
    assert m.memory_links("packaging-supplier")["note"] == "packaging-supplier"

    memrecall.reset()
    monkeypatch.setenv("CORTHEXIS_RECALL_THRESHOLD", "0")
    out = memrecall.hook_output({"hook_event_name": "UserPromptSubmit", "session_id": "cli",
                                 "prompt": "Kartonage Weiss cardboard packaging boxes"},
                                session_id="sokkan-1")
    assert "packaging-supplier" in out["hookSpecificOutput"]["additionalContext"]
    assert store.recalled_notes("sokkan-1")                   # keyed on the SOKKAN id
    hooks = memrecall.sdk_hooks("sokkan-1")
    assert set(hooks) == {"UserPromptSubmit", "PreToolUse"}
    assert hooks["PreToolUse"][0].matcher == "Task|Agent"

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    path = memrecall.cli_settings_path()
    import json
    cfg = json.loads(open(path).read())
    cmd = cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert cmd.endswith("recall_hook.py") and cfg["hooks"]["PreToolUse"][0]["matcher"] == \
        "Task|Agent"
    memrecall.reset()


def test_spawn_preseed_is_logged(indexed, monkeypatch):
    import store_backend as sb

    store, _r, _c = indexed
    monkeypatch.setattr(sb, "_store", store)
    sb.log_spawn_recall("sid-9", [{"note_name": "packaging-supplier", "score": 0.7}], "boxes")
    assert store.recalled_notes("sid-9") == {"packaging-supplier"}


def test_corpus_signature_ignores_the_index_file(corpus):
    s1 = corpus_signature(corpus)
    (corpus / "MEMORY.md").write_text("x")
    assert corpus_signature(corpus)[1:] == s1[1:]
