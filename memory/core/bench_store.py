#!/usr/bin/env python3
"""Load test of the 3.0 memory store: insertion, RAM, search latency, HNSW recall.

Fills a throw-away database with synthetic notes (random unit vectors + Zipf-distributed
words, 10 chunks per note by default) in steps (25 000 / 100 000 / 250 000 chunks), and at
each step measures:

* bulk insertion rate (COPY path of ``Store.upsert_notes``, HNSW dropped) and HNSW build time;
* incremental ``upsert_note`` latency with the index in place (what the indexer does daily);
* RAM of the Postgres container (``docker stats``) — pass ``--container``;
* ``Store.search`` latency p50/p95 without reranker (dense HNSW + lexical + exact re-score);
* recall of the approximate index: chunk-level recall@10 of the HNSW scan against an exact
  scan, for several ``hnsw.ef_search``, and note-level agreement of the final top 8 with
  the exact search (``exact_max_chunks`` set above the corpus size).

Random 768-d vectors are close to the worst case for HNSW (no cluster structure); queries
are stored vectors plus noise (cosine ~0.7 to their source), so that a true neighbour
exists, as with a real question.

    python -m core.bench_store --dsn postgresql://u:p@127.0.0.1:55432/postgres \\
        --container sokkan-v3-store-pg --sizes 25000,100000,250000
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path

import numpy as np
import psycopg

from .store import ChunkRecord, NoteRecord, SearchConfig, Store


def container_mem(name: str | None) -> str | None:
    if not name:
        return None
    try:
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


class Corpus:
    def __init__(self, dim: int, vocab: int = 20_000, seed: int = 42, words_per_chunk: int = 90):
        self.dim = dim
        self.rng = np.random.default_rng(seed)
        self.vocab = np.array([f"w{i:05d}" for i in range(vocab)])
        p = 1.0 / np.arange(1, vocab + 1) ** 1.05  # Zipf-like word frequencies
        self.p = p / p.sum()
        self.wpc = words_per_chunk

    def words(self, n: int) -> list[str]:
        return list(self.rng.choice(self.vocab, size=n, p=self.p))

    def vectors(self, n: int) -> np.ndarray:
        v = self.rng.normal(size=(n, self.dim)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def notes(self, start: int, count: int, chunks_per_note: int):
        vecs = self.vectors(count * chunks_per_note)
        out = []
        for i in range(count):
            nid = start + i
            chunks = [ChunkRecord(j, " ".join(self.words(self.wpc)),
                                  vecs[i * chunks_per_note + j])
                      for j in range(chunks_per_note)]
            out.append((NoteRecord(f"note-{nid:06d}", " ".join(self.words(8)), "project",
                                   1 if nid % 50 == 0 else 0, f"/bench/note_{nid:06d}.md",
                                   "2026-06-01", "frontmatter", ""), chunks))
        return out


def pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def sample_queries(store: Store, gen, n: int, rng, noise: float) -> list[tuple[list[float], str]]:
    with store.pool.connection() as con:
        rows = con.execute(
            f"SELECT embedding::text AS e, body FROM {gen.table} TABLESAMPLE SYSTEM (1) "
            "LIMIT %s", (n,)).fetchall()
    out = []
    for r in rows:
        v = np.array(json.loads(r["e"]), dtype=np.float32)
        v = v + rng.normal(scale=noise / np.sqrt(len(v)), size=len(v)).astype(np.float32)
        words = r["body"].split()
        out.append(((v / np.linalg.norm(v)).tolist(),
                    " ".join(rng.choice(words, size=3, replace=False))))
    return out


def chunk_recall(store: Store, gen, queries, ef_values, k: int = 10) -> dict:
    res = {}
    with store.pool.connection() as con:
        exact = []
        con.execute("BEGIN")
        con.execute("SET LOCAL enable_indexscan = off")
        for qv, _ in queries:
            exact.append({r["ctid"] for r in con.execute(
                f"SELECT ctid::text FROM {gen.table} ORDER BY embedding <#> %s::halfvec "
                "LIMIT %s", (str(qv), k)).fetchall()})
        con.execute("COMMIT")
        for ef in ef_values:
            con.execute("BEGIN")
            con.execute(f"SET LOCAL hnsw.ef_search = {int(ef)}")
            hit, lat = 0, []
            for (qv, _), ex in zip(queries, exact):
                t = time.perf_counter()
                got = {r["ctid"] for r in con.execute(
                    f"SELECT ctid::text FROM {gen.table} ORDER BY "
                    f"(embedding::halfvec({gen.dim})) <#> %s::halfvec({gen.dim}) LIMIT %s",
                    (str(qv), k)).fetchall()}
                lat.append((time.perf_counter() - t) * 1000)
                hit += len(got & ex)
            con.execute("COMMIT")
            res[ef] = {"recall@10": round(hit / (k * len(queries)), 4),
                       "hnsw_p50_ms": round(pct(lat, 0.5), 1),
                       "hnsw_p95_ms": round(pct(lat, 0.95), 1)}
    return res


def search_latency(store: Store, queries, cfg: SearchConfig, k: int = 8):
    lat, results = [], []
    for qv, text in queries:
        t = time.perf_counter()
        hits = store.search(qv, text, k, config=cfg)
        lat.append((time.perf_counter() - t) * 1000)
        results.append([h.note_name for h in hits])
    return lat, results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dsn", required=True, help="server DSN (a database 'sokkan_bench' is "
                    "created and dropped)")
    ap.add_argument("--container", help="Postgres container name, for docker stats")
    ap.add_argument("--sizes", default="25000,100000,250000")
    ap.add_argument("--dim", type=int, default=768)
    ap.add_argument("--chunks-per-note", type=int, default=10)
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--recall-queries", type=int, default=50)
    ap.add_argument("--m", type=int, default=16)
    ap.add_argument("--ef-construction", type=int, default=64)
    ap.add_argument("--ef-search", default="40,100,200")
    ap.add_argument("--maintenance-work-mem", default="512MB")
    ap.add_argument("--noise", type=float, default=0.9,
                    help="query noise (norm ratio); 0.9 ≈ cosine 0.74 to the source chunk")
    ap.add_argument("--batch-notes", type=int, default=200)
    ap.add_argument("--keep", action="store_true", help="keep the bench database")
    ap.add_argument("--json", help="write the results to this file")
    a = ap.parse_args()

    sizes = [int(s) for s in a.sizes.split(",")]
    efs = [int(e) for e in a.ef_search.split(",")]
    with psycopg.connect(a.dsn, autocommit=True) as con:
        con.execute("DROP DATABASE IF EXISTS sokkan_bench WITH (FORCE)")
        con.execute("CREATE DATABASE sokkan_bench")
    dsn = a.dsn.rsplit("/", 1)[0] + "/sokkan_bench"
    store = Store(dsn, max_size=2)
    corpus = Corpus(a.dim)
    rng = np.random.default_rng(7)
    gen = store.create_generation(f"bench:random@{a.dim}", a.dim, hnsw_m=a.m,
                                  hnsw_ef_construction=a.ef_construction)
    report = {"dim": a.dim, "m": a.m, "ef_construction": a.ef_construction,
              "chunks_per_note": a.chunks_per_note, "steps": []}
    print(f"idle RAM: {container_mem(a.container)}", flush=True)
    loaded = 0
    try:
        for size in sizes:
            step = {"chunks": size}
            # bulk load without the HNSW index (as the initial import does)
            with store.pool.connection() as con:
                con.execute(f"DROP INDEX IF EXISTS {gen.table}_hnsw")
                con.execute("UPDATE index_generations SET index_built = false WHERE id = %s",
                            (gen.id,))
                con.commit()
            t0, n0 = time.perf_counter(), loaded
            gen_s = 0.0
            while loaded < size:
                cnt = min(a.batch_notes, (size - loaded) // a.chunks_per_note)
                tg = time.perf_counter()
                items = corpus.notes(loaded // a.chunks_per_note, cnt, a.chunks_per_note)
                gen_s += time.perf_counter() - tg
                loaded += store.upsert_notes(items, gen.id)
            ins = time.perf_counter() - t0 - gen_s
            step["insert_s"] = round(ins, 1)
            step["insert_rows_per_s"] = round((loaded - n0) / ins)
            t0 = time.perf_counter()
            store.build_index(gen.id, maintenance_work_mem=a.maintenance_work_mem)
            step["hnsw_build_s"] = round(time.perf_counter() - t0, 1)
            if gen.status != "active":
                gen = store.activate_generation(gen.id)
            with store.pool.connection() as con:
                step["db_size"] = con.execute(
                    "SELECT pg_size_pretty(pg_database_size(current_database())) AS s"
                ).fetchone()["s"]
                step["hnsw_size"] = con.execute(
                    f"SELECT pg_size_pretty(pg_relation_size('{gen.table}_hnsw')) AS s"
                ).fetchone()["s"]
            step["ram_after_load"] = container_mem(a.container)
            print(json.dumps(step), flush=True)

            queries = sample_queries(store, gen, a.queries, rng, a.noise)
            step["chunk_recall"] = chunk_recall(store, gen, queries[: a.recall_queries], efs)
            print(json.dumps(step["chunk_recall"]), flush=True)

            cfg = SearchConfig(exact_max_chunks=0)
            search_latency(store, queries[:20], cfg)  # warm-up
            lat, approx = search_latency(store, queries, cfg)
            step["search_ms"] = {"p50": round(pct(lat, 0.5), 1), "p95": round(pct(lat, 0.95), 1),
                                 "max": round(max(lat), 1), "n": len(lat)}
            step["ram_during_search"] = container_mem(a.container)
            ex_cfg = SearchConfig(exact_max_chunks=10**9)
            nq = a.recall_queries
            lat_ex, exact = search_latency(store, queries[:nq], ex_cfg)
            agree_top1 = sum(x[:1] == y[:1] for x, y in zip(approx[:nq], exact)) / nq
            overlap = statistics.mean(len(set(x) & set(y)) / max(1, len(y))
                                      for x, y in zip(approx[:nq], exact))
            step["note_level"] = {"top1_agreement": round(agree_top1, 3),
                                  "top8_overlap": round(overlap, 3),
                                  "exact_search_p50_ms": round(pct(lat_ex, 0.5), 1)}
            # incremental upsert with the HNSW index present
            lat_up = []
            for it in corpus.notes(10**6 + loaded, 30, a.chunks_per_note):
                t = time.perf_counter()
                store.upsert_note(it[0], it[1], gen.id)
                lat_up.append((time.perf_counter() - t) * 1000)
            for it in corpus.notes(10**6 + loaded, 30, a.chunks_per_note):
                store.delete_note(it[0].name, gen.id)
            step["upsert_note_10_chunks_ms"] = {"p50": round(pct(lat_up, 0.5), 1),
                                                "p95": round(pct(lat_up, 0.95), 1)}
            print(json.dumps(step), flush=True)
            report["steps"].append(step)
    finally:
        store.close()
        if not a.keep:
            with psycopg.connect(a.dsn, autocommit=True) as con:
                con.execute("DROP DATABASE IF EXISTS sokkan_bench WITH (FORCE)")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
