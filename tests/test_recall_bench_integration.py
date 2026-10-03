"""Integration — the "ignored facts" bench (core/bench_recall.py) with REAL models.

Skipped unless a Postgres and an embedding server are given:

  SOKKAN_TEST_PG_DSN=postgresql://…/postgres
  CORTHEXIS_TEST_EMBED_URL=http://127.0.0.1:18180      # llama.cpp --embedding (EmbeddingGemma)
  CORTHEXIS_TEST_RERANK_URL=http://127.0.0.1:18181     # optional: GPU profile (reranker inline)

Checks what the recall injects (no model answers): every scenario at turn 1, turn 5 and in
a sub-agent, the generic coding messages, the dedup, and the latency budget.
"""
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from core import bench_recall as B  # noqa: E402
from core import embed  # noqa: E402
from core.recall import RecallConfig, Recaller  # noqa: E402
from core.store import Store  # noqa: E402

DSN = os.environ.get("SOKKAN_TEST_PG_DSN", "")
URL = os.environ.get("CORTHEXIS_TEST_EMBED_URL")
RERANK = os.environ.get("CORTHEXIS_TEST_RERANK_URL")
MODEL = os.environ.get("CORTHEXIS_TEST_EMBED_MODEL", "embeddinggemma-300m-q8")
pytestmark = pytest.mark.skipif(not (DSN and URL),
                                reason="SOKKAN_TEST_PG_DSN and CORTHEXIS_TEST_EMBED_URL needed")


@pytest.fixture(scope="module")
def store():
    name = "sokkan_rbench_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name}")
    s = Store(DSN.rsplit("/", 1)[0] + "/" + name)
    e = embed.LlamaCppEmbedder(MODEL, [URL])
    assert B.load(s, e) == len(B.FACTS) + len(B.DISTRACTORS)
    try:
        yield s
    finally:
        s.close()
        with psycopg.connect(DSN, autocommit=True) as con:
            con.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.mark.parametrize("profile", ["leger", "gpu"])
def test_ignored_facts_bench(store, profile):
    if profile == "gpu" and not RERANK:
        pytest.skip("CORTHEXIS_TEST_RERANK_URL not set")
    e = embed.LlamaCppEmbedder(MODEL, [URL], rerank_url=RERANK if profile == "gpu" else None,
                               rerank_policy="interactive" if profile == "gpu" else "off")
    res = B.run(Recaller(store, e, RecallConfig(), profile=profile))
    for mode in ("turn-1", "turn-5", "subagent"):
        assert res["by_mode"][mode]["hit_rate"] >= 0.7, res["misses"]
        assert res["by_mode"][mode]["notes_per_turn"] <= 2.5
    assert res["filler"]["false_positive_rate"] <= 0.1, res["filler_recalls"]
    assert res["dedup"]["ok"]
    assert res["latency_ms"]["p95"] < 1500
