"""core/models.py — licence decision, first-run setup, download with SHA-256 check."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import models


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for n in ("ACCEPT_GEMMA_TERMS", "MEMORY_PROFILE", "EMBED_FALLBACK", "RERANK",
              "RERANK_MODEL", "MODELS_DIR", "DATA_DIR", "MODEL_BASE_URL", "ML_SERVICE_URL"):
        monkeypatch.delenv(f"CORTHEXIS_{n}", raising=False)
        monkeypatch.delenv(f"SOKKAN_{n}", raising=False)
    monkeypatch.delenv("ML_SERVICE_URL", raising=False)
    monkeypatch.setenv("CORTHEXIS_OWNER", "owner@example.com")


def fake_fetch(fail=()):
    calls = []

    def fetch(key, root, log=print):
        calls.append(key)
        if key in fail:
            raise OSError("offline")
        p = root / "gguf" / models.MODELS[key]["file"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return p
    fetch.calls = calls
    return fetch


def test_registry_is_consistent():
    for key, m in models.MODELS.items():
        assert len(m["sha256"]) == 64 and m["size"] > 0 and len(m["rev"]) == 40
        if m["kind"] == "embed":
            assert m["dim"] > 0 and m["pooling"] in ("mean", "last")
    assert models.MODELS[models.DEFAULT_EMBED]["licence"] == "gemma"
    assert models.MODELS[models.DEFAULT_FALLBACK]["licence"] == "mit"
    g = models.MODELS["embeddinggemma-300m-q8"]
    assert g["query_prefix"] == "task: search result | query: "
    assert g["doc_prefix"] == "title: none | text: "
    assert models.url_of("embeddinggemma-300m-q8").startswith(
        "https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/0f741b5")


def test_terms_summary_both_languages():
    for lang in ("en", "fr"):
        t = models.TERMS_SUMMARY[lang]
        assert models.GEMMA_TERMS_URL in t and models.GEMMA_POLICY_URL in t
        assert "MIT" in t


def test_models_dir_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    assert models.models_dir() == tmp_path / "memory-models"
    monkeypatch.setenv("CORTHEXIS_MODELS_DIR", str(tmp_path / "m"))
    assert models.models_dir() == tmp_path / "m"


def test_accept_records_version_date_and_who(tmp_path):
    doc = models.setup("leger", forced="accepted", root=tmp_path, log=lambda m: None,
                       fetch=fake_fetch())
    assert doc["embed"] == "embeddinggemma-300m-q8" and doc["reason"] == "gemma-terms-accepted"
    lic = json.loads((tmp_path / "licence.json").read_text())["gemma"]
    assert lic["decision"] == "accepted"
    assert lic["terms_version"] == models.GEMMA_TERMS_VERSION
    assert lic["by"] == "owner@example.com" and lic["via"] == "cli" and lic["at"]
    env = (tmp_path / "embed.env").read_text()
    assert "CORTHEXIS_MODEL_FILE=gguf/embeddinggemma-300M-Q8_0.gguf" in env
    assert "CORTHEXIS_MODEL_POOLING=mean" in env
    assert not (tmp_path / "rerank.env").exists()  # leger: no reranker


def test_decline_uses_mit_fallback_and_keeps_history(tmp_path):
    models.record_decision("accepted", "a", "cli", tmp_path)
    f = fake_fetch()
    doc = models.setup("leger", forced="declined", root=tmp_path, log=lambda m: None, fetch=f)
    assert f.calls == ["harrier-oss-v1-270m-q8"]
    assert doc["embed"] == "harrier-oss-v1-270m-q8" and doc["reason"] == "gemma-terms-declined"
    assert "CORTHEXIS_MODEL_POOLING=last" in (tmp_path / "embed.env").read_text()
    st = models.licence_state(tmp_path)
    assert st["decision"] == "declined"
    assert [h["decision"] for h in st["history"]] == ["accepted", "declined"]


def test_undecided_unattended_runs_fallback(tmp_path):
    doc = models.setup("leger", root=tmp_path, log=lambda m: None, fetch=fake_fetch())
    assert doc["decision"] == "undecided"
    assert doc["embed"] == "harrier-oss-v1-270m-q8" and doc["reason"] == "gemma-terms-undecided"
    assert not (tmp_path / "licence.json").exists()  # nothing recorded on the user's behalf


def test_env_acceptance_is_recorded_with_its_source(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_ACCEPT_GEMMA_TERMS", "1")
    doc = models.setup("leger", root=tmp_path, log=lambda m: None, fetch=fake_fetch())
    assert doc["embed"] == "embeddinggemma-300m-q8"
    assert models.licence_state(tmp_path)["via"] == "env:SOKKAN_ACCEPT_GEMMA_TERMS"


def test_offline_gemma_falls_back(tmp_path):
    f = fake_fetch(fail=("embeddinggemma-300m-q8",))
    doc = models.setup("leger", forced="accepted", root=tmp_path, log=lambda m: None, fetch=f)
    assert f.calls == ["embeddinggemma-300m-q8", "harrier-oss-v1-270m-q8"]
    assert doc["embed"] == "harrier-oss-v1-270m-q8" and doc["reason"] == "gemma-unavailable"


def test_nothing_available(tmp_path):
    f = fake_fetch(fail=tuple(models.MODELS))
    doc = models.setup("gpu", forced="accepted", root=tmp_path, log=lambda m: None, fetch=f)
    assert doc["embed"] is None and doc["rerank"] is None
    assert doc["reason"] == "no-model-available" and doc["errors"]
    assert not (tmp_path / "embed.env").exists()


def test_profile_picks_reranker(tmp_path):
    doc = models.setup("gpu", forced="accepted", root=tmp_path, log=lambda m: None,
                       fetch=fake_fetch())
    assert doc["rerank"] == "qwen3-reranker-0.6b-q8"
    assert "CORTHEXIS_MODEL_POOLING" not in (tmp_path / "rerank.env").read_text()
    doc = models.setup("standard", root=tmp_path, log=lambda m: None, fetch=fake_fetch())
    assert doc["rerank"] == "bge-reranker-v2-m3-q8"


def test_reranker_failure_is_not_fatal(tmp_path):
    doc = models.setup("gpu", forced="accepted", root=tmp_path, log=lambda m: None,
                       fetch=fake_fetch(fail=("qwen3-reranker-0.6b-q8",)))
    assert doc["embed"] == "embeddinggemma-300m-q8" and doc["rerank"] is None


def test_remote_profile_installs_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_SERVICE_URL", "http://ml:8001")
    f = fake_fetch()
    doc = models.setup(root=tmp_path, log=lambda m: None, fetch=f)
    assert doc["reason"] == "profile-remote" and f.calls == []


def test_outdated_terms_are_asked_again(tmp_path, monkeypatch):
    models.record_decision("accepted", "a", "cli", tmp_path)
    monkeypatch.setattr(models, "GEMMA_TERMS_VERSION", "gemma-tou-2099-01-01")
    assert not models.licence_state(tmp_path)["current"]
    monkeypatch.setattr(models, "_ask", lambda log: "declined")
    assert models.decide(interactive=True, root=tmp_path, log=lambda m: None) == "declined"
    # unattended: the old decision is kept (with a warning), not silently dropped
    models.record_decision("accepted", "a", "cli", tmp_path)
    monkeypatch.setattr(models, "GEMMA_TERMS_VERSION", "gemma-tou-2100-01-01")
    logs = []
    assert models.decide(root=tmp_path, log=logs.append) == "accepted"
    assert "changed" in logs[0]


def test_fallback_must_not_be_gemma(monkeypatch):
    monkeypatch.setenv("CORTHEXIS_EMBED_FALLBACK", "embeddinggemma-300m-q8")
    with pytest.raises(ValueError):
        models.fallback_key()
    monkeypatch.setenv("CORTHEXIS_EMBED_FALLBACK", "multilingual-e5-base-q8")
    assert models.fallback_key() == "multilingual-e5-base-q8"


# --------------------------------------------------------------------------- download
@pytest.fixture()
def blob_server():
    payload = b"GGUF" + bytes(range(256)) * 400

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", payload
    srv.shutdown()
    srv.server_close()


def _test_model(monkeypatch, payload, sha=None):
    monkeypatch.setitem(models.MODELS, "test-model", {
        "kind": "embed", "label": "test", "licence": "mit", "repo": "org/repo", "rev": "r" * 40,
        "file": "test.gguf", "sha256": sha or hashlib.sha256(payload).hexdigest(),
        "size": len(payload), "dim": 4, "pooling": "mean"})


def test_download_verifies_sha256(blob_server, monkeypatch, tmp_path):
    url, payload = blob_server
    monkeypatch.setenv("CORTHEXIS_MODEL_BASE_URL", url)
    _test_model(monkeypatch, payload)
    p = models.download("test-model", tmp_path, log=lambda m: None)
    assert p.read_bytes() == payload
    assert models.verified("test-model", tmp_path) == p
    assert models.present("test-model", tmp_path)
    # already there: no second download
    monkeypatch.setenv("CORTHEXIS_MODEL_BASE_URL", "http://127.0.0.1:9")
    assert models.download("test-model", tmp_path, log=lambda m: None) == p


def test_download_rejects_bad_hash(blob_server, monkeypatch, tmp_path):
    url, payload = blob_server
    monkeypatch.setenv("CORTHEXIS_MODEL_BASE_URL", url)
    _test_model(monkeypatch, payload, sha="0" * 64)
    with pytest.raises(RuntimeError, match="SHA-256"):
        models.download("test-model", tmp_path, log=lambda m: None)
    assert not list((tmp_path / "gguf").iterdir())  # .part removed, nothing kept


def test_hand_copied_file_is_accepted_offline(monkeypatch, tmp_path):
    payload = b"offline copy"
    _test_model(monkeypatch, payload)
    (tmp_path / "gguf").mkdir()
    (tmp_path / "gguf" / "test.gguf").write_bytes(payload)
    monkeypatch.setenv("CORTHEXIS_MODEL_BASE_URL", "http://127.0.0.1:9")  # unreachable
    assert models.download("test-model", tmp_path, log=lambda m: None).name == "test.gguf"


def test_cli_status_and_terms(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CORTHEXIS_MODELS_DIR", str(tmp_path))
    assert models.main(["terms", "--lang", "fr"]) == 0
    assert "J'accepte" not in capsys.readouterr().out  # summary only, the prompt is separate
    assert models.main(["status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["terms_version"] == models.GEMMA_TERMS_VERSION
    assert st["licence"]["decision"] is None
