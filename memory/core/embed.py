"""embed.py — CortHeXis memory embedding engine (profiles leger | standard | gpu).

Self-contained (no application import); configured by `CORTHEXIS_*` variables,
the `SOKKAN_*` names and `ML_SERVICE_URL` of SOKKAN being read as a fallback.

Contract (docs: v3 implementation plan):

    identity() -> str          "llamacpp:embeddinggemma-300m-q8@768", stored per index generation
    embed_docs(texts) -> list[list[float]]
    embed_query(text, timeout=30) -> list[float]
    rerank(query, docs, timeout=3) -> list[float] | None    None = no reranker → keep hybrid order

Backends:

- ``llamacpp`` (profiles leger, standard, gpu): llama.cpp servers started with
  ``--embedding`` (`/v1/embeddings`, `/tokenize`) and optionally ``--reranking``
  (`/v1/rerank`). Several servers of the SAME model are tried in order (GPU then
  CPU, or a Magnitude node then the local container): vectors agree to 0.999, so
  any of them can query an index built by another. The first server gets a short
  timeout so the next one still fits in the caller's budget, and a server that
  just failed is skipped for a while.
- ``legacy`` (SOKKAN 2.x): fastembed MiniLM in process, or the remote API of
  ``CORTHEXIS_ML_SERVICE_URL`` / ``ML_SERVICE_URL`` (profile ``remote``). Kept so
  a 2.x index can serve searches while the corpus is re-encoded (migration).

Models need task prefixes (EmbeddingGemma: query ``task: search result | query: ``,
document ``title: none | text: ``) — forgetting them costs a lot of recall. They
come from the model registry (core/models.py), as does the dimension checked on
every answer: a server that serves another model raises instead of silently
mixing vector spaces into an index.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

try:  # package import (memory/ on sys.path → `core.embed`)
    from . import models as _models
except ImportError:  # loaded as a top-level module
    import models as _models  # type: ignore[no-redef]

PROFILES = ("leger", "standard", "gpu", "legacy", "remote")
LLAMACPP_PROFILES = ("leger", "standard", "gpu")
# rerank policy per profile: off (never), async (background work only: hook
# digests, deep search — 4-5 s on CPU), interactive (top 10 in every search)
RERANK_POLICY = {"leger": "off", "standard": "async", "gpu": "interactive",
                 "legacy": "off", "remote": "off"}
DEFAULT_URLS = {"gpu": "http://corthexis-embed-gpu:8080,http://corthexis-embed:8080"}
DEFAULT_URL = "http://corthexis-embed:8080"
DEFAULT_RERANK_URL = "http://corthexis-rerank:8080"

FIRST_TIMEOUT = 2.0     # s — budget of a server that is not the last one of the chain
COOLDOWN_S = 30.0       # s — a failed server is skipped (unless it is the last) for this long
MAX_QUERY_CHARS = 4000
RERANK_MAX_CHARS = 1800  # ≈ 480 tokens, the bench truncation
RERANK_QUERY_CHARS = 1000
LEGACY_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


_env = _models.env


def _ml_url() -> str | None:
    """2.x remote embedding API (CORTHEXIS_ML_SERVICE_URL, else ML_SERVICE_URL)."""
    return _env("ML_SERVICE_URL", None, "ML_SERVICE_URL")


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def current_profile() -> str:
    """CORTHEXIS_MEMORY_PROFILE (or SOKKAN_…), else `remote` when a 2.x ML_SERVICE_URL
    is set, else leger."""
    p = (_env("MEMORY_PROFILE") or "").lower()
    if p in PROFILES:
        return p
    if p:
        raise ValueError(f"CORTHEXIS_MEMORY_PROFILE={p!r}: expected one of "
                         f"{', '.join(PROFILES)}")
    return "remote" if _ml_url() else "leger"


class LlamaCppEmbedder:
    backend = "llamacpp"

    def __init__(self, model_key: str, urls: list[str], rerank_url: str | None = None,
                 rerank_policy: str = "off", max_doc_tokens: int = 500, batch: int = 16):
        spec = _models.MODELS.get(model_key)
        if not spec or spec["kind"] != "embed":
            raise ValueError(f"unknown embedding model {model_key!r}")
        if not urls:
            raise ValueError("no embedding server configured")
        self.model_key, self.spec = model_key, spec
        self.urls = [u.rstrip("/") for u in urls]
        self.rerank_url = (rerank_url or "").rstrip("/") or None
        self.rerank_policy = rerank_policy if self.rerank_url else "off"
        self.max_doc_tokens = max_doc_tokens
        self.batch = batch
        self._down: dict[str, float] = {}
        self._clients: dict = {}
        self._lock = threading.Lock()

    # -- identity --------------------------------------------------------------
    def identity(self) -> str:
        return f"llamacpp:{self.model_key}@{self.spec['dim']}"

    @property
    def dim(self) -> int:
        return self.spec["dim"]

    @property
    def lexical_weight(self) -> float:
        return self.spec["lexical_weight"]

    # -- server chain ----------------------------------------------------------
    def _chain(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            up = [u for u in self.urls if self._down.get(u, 0) <= now]
        last = self.urls[-1]
        return up if up and up[-1] == last else [*up, last]

    def _mark(self, url: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._down.pop(url, None)
            else:
                self._down[url] = time.monotonic() + COOLDOWN_S

    def _call(self, fn, timeout: float):
        """fn(client, url) on the first server that answers. Short timeout for
        every server but the last."""
        chain = self._chain()
        err: Exception | None = None
        for i, url in enumerate(chain):
            t = timeout if i == len(chain) - 1 else min(timeout, FIRST_TIMEOUT)
            try:
                out = fn(_Bound(self._client(url), t))
                self._mark(url, True)
                return out
            except DimensionMismatch:
                raise
            except Exception as e:  # noqa: BLE001 — next server
                err = e
                self._mark(url, False)
        raise RuntimeError(f"no embedding server answers ({', '.join(chain)}): {err!r}")

    def _client(self, url: str):
        """One pooled client per server: building an httpx.Client (SSL context)
        per call cost ~0.4 s under load, 20× the request itself."""
        import httpx

        with self._lock:
            c = self._clients.get(url)
            if c is None:
                c = self._clients[url] = httpx.Client(base_url=url)
            return c

    def _embed(self, c, inputs: list[str]) -> list[list[float]]:
        r = c.post("/v1/embeddings", json={"input": inputs, "model": self.model_key})
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        if len(data) != len(inputs):
            raise RuntimeError(f"{len(data)} embeddings for {len(inputs)} inputs")
        out = []
        for d in data:
            v = d["embedding"]
            if v and isinstance(v[0], list):  # pooling none → per-token; not ours
                raise DimensionMismatch("server returns per-token vectors (pooling none)")
            if len(v) != self.dim:
                raise DimensionMismatch(
                    f"server answers {len(v)}-dim vectors, {self.identity()} expected — "
                    "another model is loaded; refusing to mix vector spaces")
            out.append(_norm(v))
        return out

    def _truncate(self, c, text: str) -> str:
        """A text longer than a slot's context makes the server answer 400/500:
        cut it in tokens. Every token covers ≥ 1 byte, so short texts skip the call."""
        if len(text.encode()) + 8 <= self.max_doc_tokens:
            return text
        r = c.post("/tokenize", json={"content": text})
        r.raise_for_status()
        toks = r.json()["tokens"]
        if len(toks) <= self.max_doc_tokens:
            return text
        r = c.post("/detokenize", json={"tokens": toks[:self.max_doc_tokens]})
        r.raise_for_status()
        return r.json()["content"]

    def _truncate_all(self, c, texts: list[str]) -> list[str]:
        """Truncate a batch; the /tokenize round trips run in parallel (one HTTP
        call per long document, serially it cost two thirds of GPU indexing time)."""
        if sum(len(t.encode()) + 8 > self.max_doc_tokens for t in texts) < 2:
            return [self._truncate(c, t) for t in texts]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(8, len(texts))) as pool:
            return list(pool.map(lambda t: self._truncate(c, t), texts))

    # -- contract ----------------------------------------------------------------
    def embed_query(self, text: str, timeout: float = 30.0) -> list[float]:
        q = self.spec["query_prefix"] + (text or "")[:MAX_QUERY_CHARS]
        return self._call(lambda c: self._embed(c, [q]), timeout)[0]

    def embed_docs(self, texts: list[str], timeout: float = 600.0) -> list[list[float]]:
        out: list[list[float]] = []
        pre = self.spec["doc_prefix"]
        for i in range(0, len(texts), self.batch):
            part = [pre + t for t in texts[i:i + self.batch]]
            out.extend(self._call(
                lambda c, part=part: self._embed(c, self._truncate_all(c, part)),
                timeout))
        return out

    def rerank(self, query: str, docs: list[str], timeout: float = 3.0) -> list[float] | None:
        if not self.rerank_url or not docs:
            return None
        try:
            r = self._client(self.rerank_url).post("/v1/rerank", timeout=timeout, json={
                "query": (query or "")[:RERANK_QUERY_CHARS],
                "documents": [d[:RERANK_MAX_CHARS] for d in docs], "model": "rerank"})
            r.raise_for_status()
            out: list[float | None] = [None] * len(docs)
            for d in r.json()["results"]:
                out[d["index"]] = float(d["relevance_score"])
            if any(s is None for s in out):
                return None
            return out  # type: ignore[return-value]
        except Exception:  # noqa: BLE001 — the reranker improves, it never breaks search
            return None

    # -- introspection -----------------------------------------------------------
    def health(self, timeout: float = 1.0) -> dict:
        import httpx

        def ping(url: str) -> bool:
            try:
                return httpx.get(f"{url}/health", timeout=timeout).status_code == 200
            except Exception:  # noqa: BLE001
                return False

        return {"embed": [{"url": u, "ok": ping(u)} for u in self.urls],
                "rerank": ({"url": self.rerank_url, "ok": ping(self.rerank_url)}
                           if self.rerank_url else None)}


class _Bound:
    """A pooled client with the per-call timeout of the chain applied."""

    def __init__(self, client, timeout: float):
        self.client, self.timeout = client, timeout

    def post(self, path: str, **kw):
        return self.client.post(path, timeout=self.timeout, **kw)


class DimensionMismatch(RuntimeError):
    """The server serves another model than the identity says: never mix spaces."""


class LegacyEmbedder:
    """SOKKAN 2.x embeddings: remote ML_SERVICE_URL API, else fastembed MiniLM.
    Same vectors as a 2.x `memory.db`; no task prefixes, no reranker."""

    backend = "legacy"
    rerank_policy = "off"
    rerank_url = None
    lexical_weight = 0.5  # bench: MiniLM 0.548 → 0.632 MRR with 0.5 instead of 0.3

    def __init__(self, ml_url: str | None = None, model: str | None = None):
        self.ml_url = (ml_url or "").rstrip("/") or None
        self.model = model or LEGACY_MODEL
        self._local = None
        self._lock = threading.Lock()

    def identity(self) -> str:
        if self.ml_url:
            return f"remote:{self.ml_url}"
        dim = "@384" if self.model == LEGACY_MODEL else ""
        return f"fastembed:{self.model.split('/')[-1].lower()}{dim}"

    def identity_2x(self) -> str:
        """The string SOKKAN 2.x wrote in memory.db `meta.model` for this backend."""
        return f"remote:{self.ml_url}" if self.ml_url else f"local:{self.model}"

    def _fastembed(self):
        with self._lock:
            if self._local is None:
                from fastembed import TextEmbedding  # heavy, lazy

                cache = os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(
                    _env("DATA_DIR") or os.path.expanduser("~/.local/share/corthexis"),
                    "models")
                os.makedirs(cache, exist_ok=True)
                self._local = TextEmbedding(model_name=self.model, cache_dir=cache)
        return self._local

    def embed_docs(self, texts: list[str], timeout: float = 120.0) -> list[list[float]]:
        if not texts:
            return []
        if not self.ml_url:
            return [_norm(list(v)) for v in self._fastembed().embed(texts)]
        import httpx

        out: list[list[float]] = []
        with httpx.Client(timeout=timeout) as c:
            for i in range(0, len(texts), 64):
                part = texts[i:i + 64]
                r = c.post(f"{self.ml_url}/api/v1/embed/text", json={"texts": part})
                r.raise_for_status()
                vecs = r.json().get("embeddings") or []
                if len(vecs) != len(part):
                    raise RuntimeError(f"embed count mismatch: {len(vecs)} for {len(part)}")
                out.extend(_norm(v) for v in vecs)
        return out

    def embed_query(self, text: str, timeout: float = 30.0) -> list[float]:
        if not self.ml_url:
            return self.embed_docs([text])[0]
        import httpx

        r = httpx.post(f"{self.ml_url}/api/v1/embed/text", json={"text": text}, timeout=timeout)
        r.raise_for_status()
        v = r.json().get("embedding") or []
        if not v:
            raise RuntimeError("empty embedding")
        return _norm(v)

    def rerank(self, query: str, docs: list[str], timeout: float = 3.0) -> None:
        return None

    def health(self, timeout: float = 1.0) -> dict:
        return {"embed": [{"url": self.ml_url or "in-process", "ok": None}], "rerank": None}


def _active_model_key() -> str:
    """CORTHEXIS_EMBED_MODEL_ID > active.json written by the first-run setup > default."""
    key = _env("EMBED_MODEL_ID")
    if key:
        return key
    try:
        act = json.loads((_models.models_dir() / "active.json").read_text(encoding="utf-8"))
        if act.get("embed"):
            return act["embed"]
    except (OSError, ValueError):
        pass
    return _models.DEFAULT_EMBED


def _split(v: str) -> list[str]:
    return [u.strip() for u in v.split(",") if u.strip()]


def build(profile: str | None = None):
    """Embedder for `profile` (default: the configured one), from the environment."""
    profile = profile or current_profile()
    if profile not in PROFILES:
        raise ValueError(f"unknown memory profile {profile!r}")
    if profile in ("legacy", "remote"):
        return legacy()
    urls = _split(_env("EMBED_URLS") or _env("EMBED_URL")
                  or DEFAULT_URLS.get(profile, DEFAULT_URL))
    # CORTHEXIS_RERANK_URL: empty = the profile's default, "off" = no reranker
    rr = _env("RERANK_URL") or ""
    if rr.lower() in ("off", "none", "0"):
        rr = None
    elif not rr:
        rr = DEFAULT_RERANK_URL if RERANK_POLICY[profile] != "off" else None
    policy = _env("RERANK_POLICY") or RERANK_POLICY[profile]
    return LlamaCppEmbedder(_active_model_key(), urls, rr, policy,
                            int(_env("EMBED_MAX_TOKENS") or "500"))


def legacy() -> LegacyEmbedder:
    """The 2.x embedder, whatever the profile — serves the old index during migration."""
    return LegacyEmbedder(_ml_url(), _env("LEGACY_MODEL", None, "SOKKAN_EMBED_MODEL"))


_default = None
_default_lock = threading.Lock()


def get():
    """Process-wide embedder for the configured profile (built once)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = build()
        return _default


def reset() -> None:
    """Forget the cached embedder (profile or model changed, tests)."""
    global _default
    with _default_lock:
        _default = None


# --- module-level contract -------------------------------------------------------
def identity() -> str:
    return get().identity()


def embed_docs(texts: list[str]) -> list[list[float]]:
    return get().embed_docs(texts)


def embed_query(text: str, timeout: float = 30) -> list[float]:
    return get().embed_query(text, timeout)


def rerank(query: str, docs: list[str], timeout: float = 3) -> list[float] | None:
    return get().rerank(query, docs, timeout)


def rerank_policy() -> str:
    """off | async | interactive — whether a search may call rerank() inline."""
    return get().rerank_policy


def lexical_weight() -> float:
    """Dense/lexical blend that suits the active model (bench: 0.3 Gemma, 0.2
    harrier, 0.1 e5, 0.5 MiniLM) — re-tune whenever the model changes."""
    return get().lexical_weight


def describe() -> dict:
    """Profile, identity, model, servers — for the API and doctor (no network)."""
    e = get()
    out = {"profile": current_profile(), "identity": e.identity(), "backend": e.backend,
           "rerank_policy": e.rerank_policy, "rerank_url": e.rerank_url,
           "lexical_weight": e.lexical_weight}
    if isinstance(e, LlamaCppEmbedder):
        out.update(model=e.model_key, label=e.spec["label"], licence=e.spec["licence"],
                   dim=e.dim, urls=e.urls)
    else:
        out.update(model=e.model, urls=[e.ml_url] if e.ml_url else [])
    return out


def models_dir() -> Path:
    return _models.models_dir()
