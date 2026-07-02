#!/usr/bin/env python3
"""embeddings.py — SOKKAN: one embedding entry point, two backends.

- If ML_SERVICE_URL is set → POST {url}/api/v1/embed/text (a remote service,
  e.g. sentence-transformers on a GPU box). Fastest for large corpora.
- Otherwise → local ONNX inference via fastembed (multilingual MiniLM-L12,
  384-dim, same model family → cross-lingual recall out of the box).
  The model (~120 MB) is downloaded on first use and cached in
  $FASTEMBED_CACHE_PATH (defaults under $SOKKAN_DATA_DIR/models).

Both backends return unit-normalized vectors, so retrieval stays a dot product.
"""
from __future__ import annotations

import math
import os

ML_URL = (os.environ.get("ML_SERVICE_URL") or "").rstrip("/")
LOCAL_MODEL = os.environ.get(
    "SOKKAN_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

_local = None


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _get_local():
    global _local
    if _local is None:
        from fastembed import TextEmbedding  # imported lazily: heavy

        cache = os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(
            os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
            "models",
        )
        os.makedirs(cache, exist_ok=True)
        _local = TextEmbedding(model_name=LOCAL_MODEL, cache_dir=cache)
    return _local


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch; unit-normalized vectors, order preserved."""
    if not texts:
        return []
    if ML_URL:
        import httpx

        out: list[list[float]] = []
        with httpx.Client(timeout=120.0) as client:
            for i in range(0, len(texts), 64):
                batch = texts[i : i + 64]
                resp = client.post(f"{ML_URL}/api/v1/embed/text", json={"texts": batch})
                resp.raise_for_status()
                vecs = resp.json().get("embeddings") or []
                if len(vecs) != len(batch):
                    raise RuntimeError(f"embed count mismatch: {len(vecs)} for {len(batch)}")
                out.extend(_normalize(v) for v in vecs)
        return out
    return [_normalize(list(v)) for v in _get_local().embed(texts)]


def embed_query(text: str) -> list[float]:
    if ML_URL:
        import httpx

        resp = httpx.post(f"{ML_URL}/api/v1/embed/text", json={"text": text}, timeout=30.0)
        resp.raise_for_status()
        return _normalize(resp.json().get("embedding") or [])
    return embed_texts([text])[0]


def backend() -> str:
    return f"remote:{ML_URL}" if ML_URL else f"local:{LOCAL_MODEL}"
