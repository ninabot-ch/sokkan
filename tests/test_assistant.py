"""Nina — sélection du dialecte LLM et extraction de la réponse."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import assistant  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("SOKKAN_ASSISTANT_LLM_URL", "SOKKAN_ASSISTANT_LLM_TOKEN",
              "SOKKAN_ASSISTANT_LLM_MODEL", "SOKKAN_ASSISTANT_LLM_API",
              "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def _dedicated(monkeypatch, **extra):
    monkeypatch.setenv("SOKKAN_ASSISTANT_LLM_URL", "http://rog1:11434/v1/")
    monkeypatch.setenv("SOKKAN_ASSISTANT_LLM_TOKEN", "tok")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_dedicated_endpoint_defaults_to_anthropic(monkeypatch):
    _dedicated(monkeypatch)
    cfg = assistant._llm_config()
    assert cfg["api"] == "anthropic"
    assert cfg["url"] == "http://rog1:11434/v1"  # slash final normalisé


def test_openai_dialect_is_opt_in(monkeypatch):
    _dedicated(monkeypatch, SOKKAN_ASSISTANT_LLM_API="OpenAI",
               SOKKAN_ASSISTANT_LLM_MODEL="phi4:14b-q4_K_M")
    cfg = assistant._llm_config()
    assert cfg["api"] == "openai"          # insensible à la casse
    assert cfg["model"] == "phi4:14b-q4_K_M"


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _capture(monkeypatch, payload):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _Resp(payload)

    monkeypatch.setattr(assistant.httpx, "post", fake_post)
    return seen


def test_openai_call_shape_and_reply(monkeypatch):
    seen = _capture(monkeypatch, {"choices": [{"message": {"content": " salut "}}]})
    cfg = {"url": "http://rog1:11434/v1", "token": "tok", "api": "openai", "model": "m"}
    out = assistant._ask(cfg, "SYS", [{"role": "user", "content": "hey"}], "nick@x.ch")
    assert out == "salut"
    assert seen["url"].endswith("/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    # le système passe en premier message, pas dans un champ dédié
    assert seen["body"]["messages"][0] == {"role": "system", "content": "SYS"}


def test_openai_falls_back_to_reasoning_content(monkeypatch):
    _capture(monkeypatch, {"choices": [{"message": {"content": None,
                                                    "reasoning_content": "déduit"}}]})
    cfg = {"url": "http://x/v1", "token": "t", "api": "openai", "model": "m"}
    assert assistant._ask(cfg, "S", [], "a@b.ch") == "déduit"


def test_anthropic_call_shape_and_reply(monkeypatch):
    seen = _capture(monkeypatch, {"content": [{"type": "thinking", "text": "ignoré"},
                                              {"type": "text", "text": "ok"}]})
    cfg = {"url": "https://infer.sokkan.ch", "token": "sik_x", "api": "anthropic", "model": "m"}
    out = assistant._ask(cfg, "SYS", [{"role": "user", "content": "hey"}], "nick@x.ch")
    assert out == "ok"
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "sik_x"
    assert seen["body"]["system"] == "SYS"
