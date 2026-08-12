"""llm.py — env/model injected into sessions per mode (byok / custom / included)."""
import json

import pytest

import llm


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "llm.json"
    monkeypatch.setattr(llm, "CONFIG", path)
    for var in ("SOKKAN_INFER_BASE_URL", "SOKKAN_INFER_TOKEN",
                "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def write(c: dict) -> None:
        path.write_text(json.dumps(c), encoding="utf-8")
    return write


def test_byok_api_key(cfg):
    cfg({"mode": "byok", "anthropic_api_key": "sk-ant-x"})
    env = llm.session_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x"
    assert env["ANTHROPIC_BASE_URL"] == ""
    assert llm.session_model() is None
    assert llm.configured()


def test_custom_endpoint_env(cfg):
    cfg({"mode": "custom", "base_url": "https://api.moonshot.ai/anthropic/",
         "auth_token": "sk-moon", "model": "kimi-k2-0905-preview"})
    env = llm.session_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-moon"
    assert env["ANTHROPIC_API_KEY"] == "sk-moon"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    # sans small_model explicite, le modèle principal sert aussi de small/fast —
    # sinon le CLI demanderait un modèle Anthropic inconnu de l'endpoint
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "kimi-k2-0905-preview"
    assert llm.session_model() == "kimi-k2-0905-preview"
    assert llm.configured()


def test_custom_small_model_override(cfg):
    cfg({"mode": "custom", "base_url": "http://litellm:4000",
         "auth_token": "k", "model": "big", "small_model": "small"})
    assert llm.session_env()["ANTHROPIC_SMALL_FAST_MODEL"] == "small"


def test_custom_requires_model(cfg):
    cfg({"mode": "custom", "base_url": "http://litellm:4000", "auth_token": "k"})
    assert not llm.configured()


def test_custom_status_never_leaks_token(cfg):
    cfg({"mode": "custom", "base_url": "https://api.deepseek.com/anthropic",
         "auth_token": "sk-secret", "model": "deepseek-chat"})
    st = llm.status()
    assert st["mode"] == "custom" and st["model"] == "deepseek-chat"
    assert "sk-secret" not in json.dumps(st)
    assert not st["operator_managed"]


def test_included_metering_header(cfg):
    cfg({"mode": "included", "base_url": "https://infer.sokkan.ch",
         "auth_token": "sik_1", "model": "sokkan-ship"})
    env = llm.session_env("nick@example.ch")
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "x-sokkan-user: nick@example.ch"
    # tier white-label : le modèle est forcé (sessions ciblent la passerelle coding)
    assert env["ANTHROPIC_MODEL"] == "sokkan-ship"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "sokkan-ship"
    assert llm.session_model() == "sokkan-ship"


def test_included_defaults_to_ship_when_model_empty(cfg, monkeypatch):
    # SOKKAN_INFER_MODEL seedé à chaîne VIDE dans les VMs → doit retomber sur sokkan-ship
    monkeypatch.setattr(llm, "DEFAULT_INCLUDED_MODEL", "sokkan-ship")
    cfg({"mode": "included", "base_url": "https://infer.sokkan.ch", "auth_token": "sik_1", "model": ""})
    assert llm.status()["model"] == "sokkan-ship"
    assert llm.session_env()["ANTHROPIC_MODEL"] == "sokkan-ship"


def test_set_tier_validates_and_switches(cfg, monkeypatch):
    cfg({"mode": "included", "base_url": "https://infer.sokkan.ch", "auth_token": "sik_1", "model": "sokkan-ship"})
    monkeypatch.setattr(llm, "tier_catalog", lambda: [{"id": "sokkan-ship"}, {"id": "sokkan-deep"}])
    llm.set_tier("sokkan-deep")
    assert llm.status()["model"] == "sokkan-deep"
    with pytest.raises(ValueError):
        llm.set_tier("bogus")


def test_set_tier_rejected_when_not_included(cfg):
    cfg({"mode": "custom", "base_url": "x", "auth_token": "k", "model": "m"})
    with pytest.raises(ValueError):
        llm.set_tier("sokkan-ship")
