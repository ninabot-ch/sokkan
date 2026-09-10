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


# ---- S2 : dossier client + bascule primaire/repli -------------------------
def test_fallback_config_and_configured(monkeypatch):
    assert assistant._fallback_config() is None
    monkeypatch.setenv("SOKKAN_ASSISTANT_LLM_FALLBACK_URL", "https://infer.sokkan.ch")
    monkeypatch.setenv("SOKKAN_ASSISTANT_LLM_FALLBACK_TOKEN", "sik_x")
    fb = assistant._fallback_config()
    assert fb["api"] == "anthropic" and fb["model"] == "sokkan-ship"
    # un repli seul suffit à activer la feature (primaire pas encore debout)
    monkeypatch.setattr(assistant, "_kb", lambda: "kb")
    monkeypatch.setattr(assistant, "_llm_config", lambda: None)
    assert assistant.configured() is True


def test_fallback_kicks_in_then_sticks(monkeypatch):
    monkeypatch.setattr(assistant, "_primary_down_until", 0.0)
    calls = []

    def fake_ask(cfg, system, msgs, user_email):
        calls.append(cfg["url"])
        if cfg["url"] == "xpu":
            raise assistant.httpx.ConnectError("XPU éteint")
        return "réponse de repli"

    monkeypatch.setattr(assistant, "_ask", fake_ask)
    primary, fb = {"url": "xpu"}, {"url": "gateway"}
    out, via = assistant._ask_with_fallback(primary, fb, "S", [], "a@b.ch")
    assert (out, via) == ("réponse de repli", "fallback")
    assert calls == ["xpu", "gateway"]
    # le primaire est au coin : on ne repaie pas son timeout au message suivant
    out, via = assistant._ask_with_fallback(primary, fb, "S", [], "a@b.ch")
    assert via == "fallback" and calls == ["xpu", "gateway", "gateway"]


def test_primary_failure_propagates_without_fallback(monkeypatch):
    monkeypatch.setattr(assistant, "_primary_down_until", 0.0)
    monkeypatch.setattr(assistant, "_ask", lambda *a: (_ for _ in ()).throw(
        assistant.httpx.ConnectError("down")))
    with pytest.raises(assistant.httpx.HTTPError):
        assistant._ask_with_fallback({"url": "xpu"}, None, "S", [], "a@b.ch")


def test_dossier_never_leaks_a_db_uri(monkeypatch):
    """Garde-fou structurel : le portail envoie l'URI de connexion, le dossier
    recopie une allowlist — elle ne peut pas atterrir dans le prompt."""
    import types
    fake = types.SimpleNamespace(ENABLED=True, view=lambda: {
        "plan": "starter",
        "resources": [
            {"sku": "db-small", "name": "db", "status": "live", "fleet_host": "db.fleet",
             "uri": "postgres://avnadmin:SUPERSECRET@host:21699/defaultdb"},
            {"sku": "compute-medium", "name": "worker", "status": "live",
             "fleet_host": "worker.fleet", "private_ip": "10.0.0.21"},
            {"sku": "compute-medium", "name": "vieux", "status": "destroyed"},
        ],
        "catalog": [{"sku": "compute-medium", "label": "Instance", "price_chf": 59}],
        "routes": [{"hostname": "app-acme.sokkan.ch"}],
    })
    monkeypatch.setitem(sys.modules, "fleet", fake)
    text = "\n".join(assistant._fleet_lines())
    assert "SUPERSECRET" not in text and "postgres://" not in text
    assert "db.fleet" in text and "worker.fleet" in text and "10.0.0.21" in text
    assert "59 CHF" in text
    assert "vieux" not in text          # une ressource détruite n'encombre pas
    assert "app-acme.sokkan.ch" in text


def test_dossier_survives_a_dead_source(monkeypatch):
    """Une brique indisponible retire une ligne, elle ne casse pas le chat."""
    monkeypatch.setattr(assistant, "_dossier_cache", (0.0, ""))
    monkeypatch.setattr(assistant, "_fleet_lines", lambda: (_ for _ in ()).throw(RuntimeError("portail down")))
    monkeypatch.setattr(assistant, "_inference_lines", lambda: ["inférence : mode included"])
    monkeypatch.setattr(assistant, "_sessions_lines", lambda: [])
    text = assistant._dossier()
    assert "inférence : mode included" in text
    assert "version de l'instance" in text


def test_memory_context_maps_the_real_search_shape(monkeypatch):
    """memory_search rend note_name/snippet — pas name/chunk. Les entrées
    sentinelles ({info}/{error}) ne doivent pas produire de [[None]]."""
    import types
    fake = types.SimpleNamespace(memory_search=lambda q, k: [
        {"note_name": "flotte-exoscale", "description": "archi flotte",
         "snippet": "privnet dédié\npar client"},
        {"info": "No project memory yet."},
    ])
    monkeypatch.setitem(sys.modules, "memory_search_server", fake)
    out = assistant._memory_context("flotte")
    assert "[[flotte-exoscale]] — archi flotte" in out
    assert "privnet dédié par client" in out   # les retours ligne sont aplatis
    assert "None" not in out
