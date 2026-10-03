"""Integration tests of memory/core/store.py against a real Postgres + pgvector.

Skipped unless SOKKAN_TEST_PG_DSN points at a server where the test may create and drop
databases, e.g. an ephemeral container::

    docker run -d --name sokkan-v3-store-pg --memory 1g -p 127.0.0.1:55432:5432 \\
        -e POSTGRES_USER=sokkan -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
    SOKKAN_TEST_PG_DSN=postgresql://sokkan:test@127.0.0.1:55432/postgres pytest
"""
import datetime
import math
import os
import uuid

import numpy as np
import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytest.importorskip("psycopg_pool")

from core import search as rk  # noqa: E402
from core.store import (  # noqa: E402
    ChunkRecord, DimensionMismatch, NoteRecord, SearchConfig, Store, StoreError)

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="SOKKAN_TEST_PG_DSN not set (needs Postgres)")


@pytest.fixture()
def store():
    name = "sokkan_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    s = Store(dsn)
    try:
        yield s
    finally:
        s.close()
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


# --------------------------------------------------------------------------- corpus

TOPICS = {
    "deploy": "deploy release rollout pipeline tunnel cloudflare staging production",
    "backup": "backup restore snapshot nightly retention archive disaster recovery",
    "billing": "invoice billing payment stripe subscription refund pricing plans",
    "network": "router subnet dhcp vpn tailscale firewall ports gateway",
    "gpu": "gpu inference vram embedding reranker model quantization throughput",
    "mail": "email smtp dkim dmarc mailbox forwarding bounce deliverability",
    "monitor": "monitoring alert grafana prometheus uptime probe dashboard latency",
    "design": "logo brand colours typography visuals poster banner palette",
}
WORDS = " ".join(TOPICS.values()).split() + [
    "équipe", "déploiements", "paramètres", "notes", "client", "serveur", "semaine"]
DIM = 32


def _corpus(seed=7, n_notes=40):
    """Synthetic notes: topic words + noise, topic-centred unit vectors (fp16-exact)."""
    rng = np.random.default_rng(seed)
    centres = {t: rng.normal(size=DIM) for t in TOPICS}
    notes = []
    for i in range(n_notes):
        topic = list(TOPICS)[i % len(TOPICS)]
        tw = TOPICS[topic].split()
        name = f"{topic}-note-{i:02d}"
        desc = " ".join(rng.choice(tw, size=3, replace=False)) + f" item{i}"
        chunks = []
        for j in range(int(rng.integers(1, 5))):
            words = list(rng.choice(tw, size=6)) + list(rng.choice(WORDS, size=6))
            v = centres[topic] + rng.normal(scale=1.2, size=DIM)
            v = (v / np.linalg.norm(v)).astype(np.float16).astype(np.float32)
            chunks.append(ChunkRecord(j, " ".join(words) + f" para{j}", v.tolist()))
        mod = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) + \
            datetime.timedelta(days=int(rng.integers(0, 250)))
        notes.append((NoteRecord(name, desc, "project", 0, f"/notes/{name}.md", mod,
                                 "frontmatter" if i % 3 else "indexed", "body"), chunks))
    queries = []
    for i in range(16):
        topic = list(TOPICS)[i % len(TOPICS)]
        v = centres[topic] + rng.normal(scale=1.5, size=DIM)
        text = " ".join(rng.choice(TOPICS[topic].split(), size=2)) + \
            (" déploiement paramètre" if i % 4 == 0 else "")
        queries.append((v.tolist(), text))
    return notes, queries


def _fp16_unit(v):
    a = np.asarray(v, dtype=np.float32)
    return (a / np.linalg.norm(a)).astype(np.float16).astype(np.float32)


def reference_search(notes, qvec, query, top_k, lexical_weight=0.30, head_share=0.5):
    """Port of the SQLite search of the internal reference implementation (brute force over
    every chunk, IDF head/body lexical), used as ground truth for the ranking."""
    chunks = [(n.name, n.description, c.body, np.asarray(c.embedding, dtype=np.float32))
              for n, cs in notes for c in cs]
    hay = {}
    for name, desc, body, _ in chunks:
        hay.setdefault(name, [name, desc or ""]).append(body)
    toks = {n: rk.tokens(" ".join(p)) for n, p in hay.items()}
    head = {n: rk.tokens(p[0].replace("-", " ") + " " + p[1]) for n, p in hay.items()}
    df = {}
    for ts in toks.values():
        for t in ts:
            df[t] = df.get(t, 0) + 1
    nn = max(len(toks), 1)
    idf = {t: math.log(1 + nn / c) for t, c in df.items()}
    qtok = rk.tokens(query)
    q = None if qvec is None else _fp16_unit(qvec)
    agg = {}
    for name, desc, body, emb in chunks:
        rel = float(np.dot(q, emb)) if q is not None else len(qtok & rk.tokens(body)) / len(qtok)
        a = agg.get(name)
        if a is None:
            agg[name] = {"rel": rel, "snippet": body}
        elif rel > a["rel"]:
            a["rel"], a["snippet"] = rel, body
    out = []
    for name, a in agg.items():
        lex = 0.0
        if qtok:
            w = {t: idf.get(t, math.log(1 + len(agg))) for t in qtok}
            tot = sum(w.values()) or 1.0
            body_lex = sum(w[t] for t in qtok & toks[name]) / tot
            head_lex = sum(w[t] for t in qtok & head[name]) / tot
            lex = (1 - head_share) * body_lex + head_share * head_lex
        score = 0.7 * lex + 0.3 * a["rel"] if q is None else \
            (1 - lexical_weight) * a["rel"] + lexical_weight * lex
        out.append((name, score, a["snippet"]))
    out.sort(key=lambda r: (-r[1], r[0]))
    return out[:top_k]


def _load(store, notes, dim=DIM, identity="test:model@32"):
    g = store.create_generation(identity, dim)
    store.upsert_notes(notes, g.id)
    return store.activate_generation(g.id)


# --------------------------------------------------------------------------- tests

def test_migrate_is_idempotent(store):
    assert store.schema_version() == 1
    assert store.migrate() == []


def test_ranking_equivalent_to_sqlite_reference(store):
    notes, queries = _corpus()
    _load(store, notes)
    for qv, text in queries:
        ref = reference_search(notes, qv, text, 10)
        got = store.search(qv, text, 10)
        assert [h.note_name for h in got] == [r[0] for r in ref], text
        for h, r in zip(got, ref):
            assert h.score == pytest.approx(r[1], abs=2e-3)
            assert h.snippet == r[2]


def test_ranking_equivalent_other_lexical_weight(store):
    notes, queries = _corpus(seed=11)
    _load(store, notes)
    for qv, text in queries[:8]:
        ref = reference_search(notes, qv, text, 8, lexical_weight=0.5)
        got = store.search(qv, text, 8, lexical_weight=0.5)
        assert [h.note_name for h in got] == [r[0] for r in ref]


def test_lexical_only_mode_equivalent(store):
    notes, queries = _corpus(seed=3)
    _load(store, notes)
    for _qv, text in queries:
        ref = [r for r in reference_search(notes, None, text, 10) if r[1] > 0]
        got = store.search(None, text, 10)
        assert [h.note_name for h in got] == [r[0] for r in ref][: len(got)]
        assert all(h.degraded and h.cosine is None for h in got)
    with pytest.raises(StoreError):
        store.search(None, "de la", 5)


def test_approximate_path_matches_exact_on_small_corpus(store):
    notes, queries = _corpus(seed=5, n_notes=60)
    _load(store, notes)
    approx = SearchConfig(exact_max_chunks=0, dense_candidates=40, lexical_candidates=20)
    agree = 0
    for qv, text in queries:
        exact = [h.note_name for h in store.search(qv, text, 5)]
        appr = [h.note_name for h in store.search(qv, text, 5, config=approx)]
        agree += exact == appr
    assert agree >= len(queries) - 1


def test_hnsw_index_is_used_on_large_generation(store):
    notes, _ = _corpus()
    g = _load(store, notes)
    with store.pool.connection() as con:
        con.execute("SET enable_seqscan = off")
        plan = "\n".join(r["QUERY PLAN"] for r in con.execute(
            f"EXPLAIN SELECT note_id FROM {g.table} ORDER BY (embedding::halfvec({DIM})) "
            f"<#> %s::halfvec({DIM}) LIMIT 10", (str([0.1] * DIM),)).fetchall())
    assert f"{g.table}_hnsw" in plan


def test_rerank_and_rrf(store):
    notes, queries = _corpus()
    _load(store, notes)
    qv, text = queries[0]
    base = store.search(qv, text, 5)
    rev = store.search(qv, text, 5, rerank=lambda q, docs: list(range(len(docs))))
    assert [h.note_name for h in rev[:5]] != [h.note_name for h in base]
    assert rev[0].rerank is not None
    down = store.search(qv, text, 5, rerank=lambda q, docs: None)
    assert [h.note_name for h in down] == [h.note_name for h in base]
    rrf = store.search(qv, text, 5, fusion="rrf")
    assert rrf[0].score < 0.05 and len(rrf) == 5


def test_hits_carry_age_provenance_generation(store):
    notes, queries = _corpus()
    g = _load(store, notes)
    hits = store.search(queries[1][0], queries[1][1], 5)
    for h in hits:
        assert h.generation == g.id
        assert h.age_days is not None and h.age_days > 0
        assert h.date_source in ("frontmatter", "indexed")
        d = h.as_dict()
        assert {"age_days", "date_source", "generation"} <= set(d)


def test_two_generations_of_different_dimensions(store):
    notes, queries = _corpus()
    g1 = _load(store, notes)
    g2 = store.create_generation("test:bigger@48", 48)
    rng = np.random.default_rng(1)
    big = [(n, [ChunkRecord(c.idx, c.body, rng.normal(size=48).tolist()) for c in cs])
           for n, cs in notes]
    store.upsert_notes(big[:10], g2.id)
    # the active generation keeps serving while g2 builds
    assert store.active_generation().id == g1.id
    assert store.search(queries[0][0], queries[0][1], 3)[0].generation == g1.id
    with pytest.raises(DimensionMismatch):
        store.search(queries[0][0], "x", 3, generation=g2.id)
    assert store.search(rng.normal(size=48).tolist(), "deploy", 3, generation=g2.id)
    with pytest.raises(DimensionMismatch):
        store.upsert_note(notes[0][0], notes[0][1], g2.id)
    store.upsert_notes(big[10:], g2.id)
    store.activate_generation(g2.id)
    gens = {g.id: g for g in store.list_generations()}
    assert gens[g2.id].status == "active" and gens[g1.id].status == "retired"
    assert gens[g2.id].chunk_count == sum(len(cs) for _, cs in notes)
    assert store.search(rng.normal(size=48).tolist(), "x", 3)[0].generation == g2.id
    # retired one is still searchable on demand, then purged
    assert store.search(queries[0][0], queries[0][1], 3, generation=g1.id)
    with pytest.raises(StoreError):
        store.drop_generation(g2.id)
    assert store.purge_retired(datetime.timedelta(days=7)) == []
    assert store.purge_retired(datetime.timedelta(0)) == [g1.id]
    assert [g.id for g in store.list_generations()] == [g2.id]
    assert len(store.note_names()) == len(notes)


def test_upsert_replace_delete_and_idf_stats(store):
    g = store.create_generation("t@4", 4)
    store.upsert_note(NoteRecord("alpha", "first note", body="b"),
                      [ChunkRecord(0, "zebra tunnel", [1, 0, 0, 0]),
                       ChunkRecord(1, "giraffe", [0, 1, 0, 0])], g.id)
    store.upsert_note(NoteRecord("beta", "second"), [ChunkRecord(0, "zebra", [0, 0, 1, 0])], g.id)

    def df():
        with store.pool.connection() as con:
            return {r["token"]: r["df"] for r in con.execute("SELECT token, df FROM lex_df")}

    d = df()
    assert d[""] == 2 and d["zebra"] == 2 and d["giraffe"] == 1
    # replace alpha: its old chunks go, giraffe disappears from the stats
    store.upsert_note(NoteRecord("alpha", "first note"), [ChunkRecord(0, "zebra", [1, 0, 0, 0])],
                      g.id)
    d = df()
    assert "giraffe" not in d and d["zebra"] == 2 and d[""] == 2
    assert store.get_generation(g.id).chunk_count == 2
    assert store.get_chunks("alpha", g.id) == ["zebra"]
    store.delete_note("beta", g.id)
    d = df()
    assert d[""] == 1 and d["zebra"] == 1 and "second" not in d
    assert store.get_note("beta") is None and store.get_generation(g.id).chunk_count == 1
    store.delete_note("nope")  # no-op


def test_links_and_note_lookup(store):
    g = store.create_generation("t@4", 4)
    store.upsert_note(NoteRecord("a-note", "A", source_path="/m/a_note.md", links=["b", "zz"]),
                      [ChunkRecord(0, "x", [1, 0, 0, 0])], g.id)
    store.upsert_note(NoteRecord("b", "B", links=["a-note"]), [ChunkRecord(0, "y", [0, 1, 0, 0])],
                      g.id)
    ln = store.links("a-note")
    assert [(x["name"], x["exists"]) for x in ln["links"]] == [("b", True), ("zz", False)]
    assert [x["name"] for x in ln["backlinks"]] == ["b"]
    assert store.get_note("a-note").links == ["b", "zz"]
    # links=None leaves them alone
    store.upsert_note(NoteRecord("a-note", "A2", source_path="/m/a_note.md"), [ChunkRecord(0, "x", [1, 0, 0, 0])], g.id)
    assert store.get_note("a-note").links == ["b", "zz"]
    assert store.find_note_by_path("a-note") == "a-note"


def test_note_versions(store):
    assert not store.has_versions()
    t0 = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
    v = store.record_seen("n", "h1", first_seen=t0, date_source="frontmatter", seeded=True,
                          seen_at=t0)
    assert v.first_seen == t0 and v.seeded
    t1 = t0 + datetime.timedelta(days=3)
    v = store.record_seen("n", "h1", first_seen=t1, seen_at=t1)
    assert v.first_seen == t0 and v.last_seen == t1 and v.date_source == "frontmatter"
    t2 = t1 + datetime.timedelta(days=1)
    store.record_seen("n", "h2", seen_at=t2, date_source="indexed")
    assert store.note_seen("n").fingerprint == "h2"
    # rename: the same content under another name finds the old date
    assert store.versions_by_fingerprint(["h1", "zz"])[0].first_seen == t0
    v = store.record_seen("n", "h1", first_seen=t2, seen_at=t2, reset_first_seen=True)
    assert v.first_seen == t2
    assert store.has_versions() and store.note_seen("missing") is None


def test_recall_log(store):
    notes, queries = _corpus()
    _load(store, notes)
    store.record_seen(notes[0][0].name, "fp-x")
    hits = store.search(queries[0][0], queries[0][1], 3)
    assert store.log_recall("prompt", hits, session_id="s1", query=queries[0][1]) == 3
    with store.pool.connection() as con:
        rows = con.execute("SELECT note_name, rank, generation_id FROM recall_log "
                           "ORDER BY rank").fetchall()
    assert [r["rank"] for r in rows] == [1, 2, 3] and rows[0]["generation_id"] == hits[0].generation


def test_no_active_generation_returns_nothing(store):
    assert store.search([0.0] * 4, "x", 3) == []
    with pytest.raises(StoreError):
        store.search([0.0] * 4, "x", 3, generation=99)
