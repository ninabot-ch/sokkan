"""Integration — core/embed.py against a REAL llama.cpp server.

Skipped unless a server is given:

  CORTHEXIS_TEST_EMBED_URL=http://127.0.0.1:18180      # llama.cpp --embedding (corthexis-embed)
  CORTHEXIS_TEST_EMBED_MODEL=embeddinggemma-300m-q8    # what it serves (default)
  CORTHEXIS_TEST_RERANK_URL=http://127.0.0.1:18181     # optional, llama.cpp --reranking

e.g. `docker compose -f docker/embed/compose.yml up -d corthexis-embed` with a
published port, then `pytest tests/test_core_embed_integration.py`.
"""
import os
import time

import pytest

from core import embed

URL = os.environ.get("CORTHEXIS_TEST_EMBED_URL")
MODEL = os.environ.get("CORTHEXIS_TEST_EMBED_MODEL", "embeddinggemma-300m-q8")
RERANK = os.environ.get("CORTHEXIS_TEST_RERANK_URL")

pytestmark = pytest.mark.skipif(not URL, reason="CORTHEXIS_TEST_EMBED_URL not set")

DOCS = [
    "Le tunnel Cloudflare publie les sites internes ; sa configuration passe par l'API.",
    "La recette du gâteau au chocolat demande 200 g de beurre et quatre œufs.",
    "Backups run nightly at 04:00 and are copied to an offsite bucket in Geneva.",
]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


@pytest.fixture(scope="module")
def e():
    return embed.LlamaCppEmbedder(MODEL, [URL], rerank_url=RERANK,
                                  rerank_policy="interactive" if RERANK else "off")


def test_identity_matches_the_server(e):
    v = e.embed_query("ping")
    assert len(v) == e.dim
    assert abs(_dot(v, v) - 1.0) < 1e-6


def test_retrieval_is_sensible_across_languages(e):
    docs = e.embed_docs(DOCS)
    for q, want in [("comment exposer un service interne ?", 0),
                    ("chocolate cake ingredients", 1),
                    ("wann laufen die Sicherungen?", 2)]:
        qv = e.embed_query(q)
        scores = [_dot(qv, d) for d in docs]
        assert scores.index(max(scores)) == want, (q, scores)


def test_task_prefixes_matter(e):
    """The query prefix changes the vector: proof that it reaches the model."""
    import httpx

    raw = httpx.post(f"{URL}/v1/embeddings", json={"input": ["où sont les sauvegardes ?"]},
                     timeout=30).json()["data"][0]["embedding"]
    raw = embed._norm(raw)
    assert _dot(raw, e.embed_query("où sont les sauvegardes ?")) < 0.999


def test_long_document_is_truncated_not_rejected(e):
    long_doc = " ".join(f"mot{i} sauvegarde nocturne" for i in range(3000))  # ≫ a slot
    v = e.embed_docs([long_doc])[0]
    assert len(v) == e.dim


def test_fallback_to_the_real_server(e):
    chain = embed.LlamaCppEmbedder(MODEL, ["http://127.0.0.1:9", URL])
    assert _dot(chain.embed_query("tunnel"), e.embed_query("tunnel")) > 0.9999


@pytest.mark.skipif(not RERANK, reason="CORTHEXIS_TEST_RERANK_URL not set")
def test_rerank_orders_relevant_first(e):
    s = e.rerank("à quelle heure tournent les sauvegardes ?", DOCS, timeout=60)
    assert s is not None and s.index(max(s)) == 2


def test_latency_budget(e):
    e.embed_query("warm-up")
    t = []
    for i in range(10):
        t0 = time.perf_counter()
        e.embed_query(f"question {i} sur la mémoire")
        t.append(time.perf_counter() - t0)
    assert sorted(t)[len(t) // 2] < 1.0  # generous: a loaded CPU box still answers well under 1 s
