"""core/embed.py — server chain, identity, task prefixes, truncation, reranker.

The llama.cpp servers are emulated by small local HTTP servers (same routes and
payloads: /v1/embeddings, /tokenize, /detokenize, /v1/rerank, /health)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import embed, models

_ENV = ("MEMORY_PROFILE", "EMBED_URLS", "EMBED_URL", "RERANK_URL", "RERANK_POLICY",
        "EMBED_MODEL_ID", "EMBED_MAX_TOKENS", "MODELS_DIR", "DATA_DIR", "ML_SERVICE_URL",
        "LEGACY_MODEL", "EMBED_MODEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for n in _ENV:
        monkeypatch.delenv(f"CORTHEXIS_{n}", raising=False)
        monkeypatch.delenv(f"SOKKAN_{n}", raising=False)
    monkeypatch.delenv("ML_SERVICE_URL", raising=False)
    monkeypatch.setenv("CORTHEXIS_MODELS_DIR", str(tmp_path / "models"))
    embed.reset()
    yield
    embed.reset()


class FakeLlama:
    """A llama.cpp server double. Tokenizer = one token per character."""

    def __init__(self, dim=768, delay=0.0):
        self.dim, self.delay = dim, delay
        self.calls: list[tuple[str, dict]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.calls.append((self.path, {}))
                self._send({"status": "ok"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                fake.calls.append((self.path, req))
                if fake.delay:
                    time.sleep(fake.delay)
                if self.path == "/v1/embeddings":
                    data = [{"index": i, "embedding": [float(len(t) % 7 + 1)] + [0.0] * (fake.dim - 1)}
                            for i, t in enumerate(req["input"])]
                    self._send({"data": list(reversed(data))})  # order restored by index
                elif self.path == "/tokenize":
                    self._send({"tokens": [ord(c) for c in req["content"]]})
                elif self.path == "/detokenize":
                    self._send({"content": "".join(chr(t) for t in req["tokens"])})
                elif self.path == "/v1/rerank":
                    res = [{"index": i, "relevance_score": 1.0 / (i + 1)}
                           for i in range(len(req["documents"]))]
                    self._send({"results": list(reversed(res))})
                else:
                    self._send({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def posts(self, path):
        return [r for p, r in self.calls if p == path]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def servers():
    made = []

    def make(**kw):
        s = FakeLlama(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def _dead_url():
    s = FakeLlama()
    url = s.url
    s.close()  # nothing listens there any more → connection refused
    return url


# --------------------------------------------------------------------------- identity / profile
def test_identity_default_model():
    e = embed.build("leger")
    assert e.identity() == "llamacpp:embeddinggemma-300m-q8@768"
    assert e.urls == ["http://corthexis-embed:8080"]
    assert e.rerank_url is None and e.rerank_policy == "off"


def test_identity_follows_active_model(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    (d / "active.json").write_text(json.dumps({"embed": "multilingual-e5-base-q8"}))
    e = embed.build("standard")
    assert e.identity() == "llamacpp:multilingual-e5-base-q8@768"
    assert e.lexical_weight == 0.1


def test_profiles_defaults(monkeypatch):
    gpu = embed.build("gpu")
    assert gpu.urls == ["http://corthexis-embed-gpu:8080", "http://corthexis-embed:8080"]
    assert gpu.rerank_url == "http://corthexis-rerank:8080"
    assert gpu.rerank_policy == "interactive"
    std = embed.build("standard")
    assert std.rerank_policy == "async"
    monkeypatch.setenv("CORTHEXIS_RERANK_URL", "off")
    assert embed.build("gpu").rerank_url is None


def test_corthexis_env_wins_over_sokkan(monkeypatch):
    monkeypatch.setenv("SOKKAN_MEMORY_PROFILE", "gpu")
    assert embed.current_profile() == "gpu"
    monkeypatch.setenv("CORTHEXIS_MEMORY_PROFILE", "standard")
    assert embed.current_profile() == "standard"
    monkeypatch.setenv("SOKKAN_EMBED_URLS", "http://a:1, http://b:2")
    assert embed.build().urls == ["http://a:1", "http://b:2"]


def test_2x_config_maps_to_remote(monkeypatch):
    monkeypatch.setenv("ML_SERVICE_URL", "http://ml:8001/")
    assert embed.current_profile() == "remote"
    e = embed.build()
    assert isinstance(e, embed.LegacyEmbedder)
    assert e.identity() == "remote:http://ml:8001"
    assert e.identity_2x() == "remote:http://ml:8001"
    assert e.rerank("q", ["d"]) is None


def test_legacy_fastembed_identity(monkeypatch):
    e = embed.legacy()
    assert e.identity() == "fastembed:paraphrase-multilingual-minilm-l12-v2@384"
    assert e.identity_2x() == f"local:{embed.LEGACY_MODEL}"
    monkeypatch.setenv("SOKKAN_EMBED_MODEL", "intfloat/multilingual-e5-small")
    assert embed.legacy().identity() == "fastembed:multilingual-e5-small"  # dim unknown


def test_invalid_profile(monkeypatch):
    monkeypatch.setenv("CORTHEXIS_MEMORY_PROFILE", "turbo")
    with pytest.raises(ValueError):
        embed.current_profile()


# --------------------------------------------------------------------------- prefixes / vectors
def test_task_prefixes_and_normalisation(servers, monkeypatch):
    s = servers()
    monkeypatch.setenv("CORTHEXIS_EMBED_URLS", s.url)
    v = embed.embed_query("où est le tunnel ?")
    assert abs(sum(x * x for x in v) - 1.0) < 1e-9
    assert s.posts("/v1/embeddings")[-1]["input"] == [
        "task: search result | query: où est le tunnel ?"]
    out = embed.embed_docs(["a", "bb", "ccc"])
    assert len(out) == 3 and all(len(x) == 768 for x in out)
    assert s.posts("/v1/embeddings")[-1]["input"] == [
        "title: none | text: a", "title: none | text: bb", "title: none | text: ccc"]


def test_e5_prefixes(servers, monkeypatch):
    s = servers()
    monkeypatch.setenv("CORTHEXIS_EMBED_URLS", s.url)
    monkeypatch.setenv("CORTHEXIS_EMBED_MODEL_ID", "multilingual-e5-base-q8")
    embed.embed_query("où ?")
    assert s.posts("/v1/embeddings")[-1]["input"] == ["query: où ?"]
    embed.embed_docs(["doc"])
    assert s.posts("/v1/embeddings")[-1]["input"] == ["passage: doc"]


def test_harrier_prefixes(servers, monkeypatch, tmp_path):
    s = servers(dim=640)
    monkeypatch.setenv("CORTHEXIS_EMBED_URLS", s.url)
    monkeypatch.setenv("CORTHEXIS_EMBED_MODEL_ID", "harrier-oss-v1-270m-q8")
    embed.embed_query("x")
    assert s.posts("/v1/embeddings")[-1]["input"][0].startswith("Instruct: Given a web search")
    embed.embed_docs(["doc"])
    assert s.posts("/v1/embeddings")[-1]["input"] == ["doc"]  # no document prefix


def test_batches(servers, monkeypatch):
    s = servers()
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [s.url], batch=4)
    assert len(e.embed_docs([f"t{i}" for i in range(10)])) == 10
    assert [len(r["input"]) for r in s.posts("/v1/embeddings")] == [4, 4, 2]


def test_truncation_in_tokens(servers):
    s = servers()
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [s.url], max_doc_tokens=50)
    e.embed_docs(["short"])
    assert not s.posts("/tokenize")  # ≤ 50 bytes: no tokenizer round trip
    e.embed_docs(["x" * 200])
    assert len(s.posts("/tokenize")) == 1 and len(s.posts("/detokenize")) == 1
    sent = s.posts("/v1/embeddings")[-1]["input"][0]
    assert len(sent) == 50 and sent.startswith("title: none | text: ")


def test_dimension_mismatch_is_refused(servers):
    s = servers(dim=384)
    other = servers()
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [s.url, other.url])
    with pytest.raises(embed.DimensionMismatch):
        e.embed_query("q")
    assert not other.posts("/v1/embeddings")  # never silently served by the next one


# --------------------------------------------------------------------------- chain
def test_chain_skips_dead_server_then_cools_down(servers):
    dead, live = _dead_url(), servers()
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [dead, live.url])
    assert len(e.embed_query("q")) == 768
    assert dead in e._down
    assert e._chain() == [live.url]  # the dead one is skipped while it cools down
    e._down[dead] = 0  # cooldown over → tried again first
    assert e._chain() == [dead, live.url]


def test_first_server_gets_short_timeout(servers, monkeypatch):
    monkeypatch.setattr(embed, "FIRST_TIMEOUT", 0.3)
    slow, fast = servers(delay=2.0), servers()
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [slow.url, fast.url])
    t0 = time.monotonic()
    e.embed_query("q", timeout=30)
    assert time.monotonic() - t0 < 1.5
    assert fast.posts("/v1/embeddings")


def test_last_server_keeps_full_timeout(servers, monkeypatch):
    monkeypatch.setattr(embed, "FIRST_TIMEOUT", 0.1)
    slow = servers(delay=0.4)
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [slow.url])
    assert len(e.embed_query("q", timeout=5)) == 768


def test_all_servers_down_raises():
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [_dead_url(), _dead_url()])
    with pytest.raises(RuntimeError, match="no embedding server answers"):
        e.embed_query("q")


# --------------------------------------------------------------------------- reranker
def test_rerank_scores_in_document_order(servers):
    s = servers()
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [s.url], rerank_url=s.url,
                               rerank_policy="interactive")
    scores = e.rerank("q" * 5000, ["d1", "d2" * 2000, "d3"])
    assert scores == [1.0, 0.5, pytest.approx(1 / 3)]
    req = s.posts("/v1/rerank")[-1]
    assert len(req["query"]) == embed.RERANK_QUERY_CHARS
    assert len(req["documents"][1]) == embed.RERANK_MAX_CHARS


def test_rerank_unavailable_returns_none(servers):
    e = embed.LlamaCppEmbedder("embeddinggemma-300m-q8", [servers().url],
                               rerank_url=_dead_url(), rerank_policy="interactive")
    assert e.rerank("q", ["d"]) is None
    assert embed.LlamaCppEmbedder("embeddinggemma-300m-q8", ["http://x"]).rerank("q", ["d"]) is None


def test_module_contract(servers, monkeypatch):
    s = servers()
    monkeypatch.setenv("CORTHEXIS_MEMORY_PROFILE", "gpu")
    monkeypatch.setenv("CORTHEXIS_EMBED_URLS", s.url)
    monkeypatch.setenv("CORTHEXIS_RERANK_URL", s.url)
    assert embed.identity() == "llamacpp:embeddinggemma-300m-q8@768"
    assert len(embed.embed_query("q")) == 768
    assert len(embed.embed_docs(["a", "b"])) == 2
    assert embed.rerank("q", ["a", "b"]) == [1.0, 0.5]
    assert embed.rerank_policy() == "interactive"
    assert embed.lexical_weight() == models.MODELS["embeddinggemma-300m-q8"]["lexical_weight"]
    d = embed.describe()
    assert d["profile"] == "gpu" and d["licence"] == "gemma" and d["dim"] == 768


# --------------------------------------------------------------------------- legacy (2.x)
def test_legacy_remote_api(monkeypatch):
    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append(req)
            out = ({"embeddings": [[3.0, 4.0]] * len(req["texts"])} if "texts" in req
                   else {"embedding": [0.0, 2.0]})
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ML_SERVICE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        e = embed.build()
        assert e.embed_docs(["a", "b"]) == [[0.6, 0.8], [0.6, 0.8]]
        assert e.embed_query("q") == [0.0, 1.0]
        assert got[0] == {"texts": ["a", "b"]} and got[1] == {"text": "q"}  # no prefixes
    finally:
        srv.shutdown()
        srv.server_close()
