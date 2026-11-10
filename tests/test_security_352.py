"""3.5.2 — two security debts of the on-prem audit, and the commit of a compose install.

* backend/previewenv.py passed the api's WHOLE environment to the dev-server it starts;
* `POST /api/llm` wrote the model key in clear in llm.json;
* /api/version answered commit "unknown" on every compose install (D11)."""
import json
import os

import pytest

SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-LEAKME", "SOKKAN_LOCAL_TOKEN": "tok-LEAKME",
    "SOKKAN_DB_PASSWORD": "pg-LEAKME", "SOKKAN_SESSION_SECRET": "sess-LEAKME",
    "SOKKAN_OIDC_CLIENT_SECRET": "oidc-LEAKME", "SOKKAN_INFER_TOKEN": "sik-LEAKME",
    "SOKKAN_SMTP_PASSWORD": "smtp-LEAKME", "CORTHEXIS_DATABASE_URL": "postgresql://u:LEAKME@db/x",
    "SOKKAN_DEEP_TOKEN": "deep-LEAKME", "CLAUDE_CODE_OAUTH_TOKEN": "oat-LEAKME",
}


def test_dev_server_does_not_inherit_the_api_environment(tmp_path, monkeypatch):
    import previewenv
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    out = tmp_path / "env.txt"
    cfg = tmp_path / "preview-envs.json"
    cfg.write_text(json.dumps({"web": {
        "cwd": str(tmp_path), "port": 1, "label": "web",
        "cmd": f"env > {out}", "env": {"API_URL": "http://api.local", "N": 3}}}))
    monkeypatch.setattr(previewenv, "CFG", cfg)
    monkeypatch.setattr(previewenv, "LOG_DIR", tmp_path)
    monkeypatch.setattr(previewenv, "_listening", lambda port: False)
    previewenv.start("web")
    for _ in range(100):
        if out.exists() and "PORT=" in out.read_text():
            break
        import time
        time.sleep(0.05)
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert "LEAKME" not in out.read_text()
    assert not [k for k in got if k.startswith(("SOKKAN_", "CORTHEXIS_", "ANTHROPIC_", "CLAUDE_"))]
    assert got["PORT"] == "1" and got["API_URL"] == "http://api.local" and got["N"] == "3"
    assert got["PATH"] == os.environ["PATH"] and got["LC_ALL"] == "C.UTF-8"


# ---------------------------------------------------------------- llm.json at rest

@pytest.fixture()
def llm_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    import secrets_provider
    secrets_provider.reset() if hasattr(secrets_provider, "reset") else None
    import llm
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    for var in ("SOKKAN_INFER_BASE_URL", "SOKKAN_INFER_TOKEN", "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN", "SOKKAN_SHIP_URL"):
        monkeypatch.delenv(var, raising=False)
    return llm


@pytest.mark.parametrize("cfg,secret", [
    ({"mode": "byok", "anthropic_api_key": "sk-ant-api03-PLAINTEXT"}, "sk-ant-api03-PLAINTEXT"),
    ({"mode": "byok", "claude_oauth_token": "sk-ant-oat01-PLAINTEXT"}, "sk-ant-oat01-PLAINTEXT"),
    ({"mode": "custom", "base_url": "http://ship:8004", "auth_token": "tok-PLAINTEXT", "model": "m"},
     "tok-PLAINTEXT"),
])
def test_llm_json_never_holds_a_key_in_clear(llm_file, cfg, secret):
    llm = llm_file
    llm.save(cfg)
    raw = llm.CONFIG.read_text()
    assert secret not in raw and "PLAINTEXT" not in raw
    assert oct(llm.CONFIG.stat().st_mode & 0o777) == "0o600"
    assert llm.load() == cfg                       # sessions still get the key, from memory
    assert llm.configured()
    env = llm.session_env()
    assert secret in env.values()


def test_a_pre_352_plaintext_file_is_sealed_on_first_read(llm_file):
    llm = llm_file
    llm.CONFIG.write_text(json.dumps({"mode": "byok", "anthropic_api_key": "sk-ant-OLD-PLAINTEXT"}))
    assert llm.load()["anthropic_api_key"] == "sk-ant-OLD-PLAINTEXT"
    assert "PLAINTEXT" not in llm.CONFIG.read_text()
    assert llm.load()["anthropic_api_key"] == "sk-ant-OLD-PLAINTEXT"


def test_post_api_llm_stores_the_key_encrypted(llm_file, monkeypatch):
    from fastapi.testclient import TestClient

    import app as a
    import auth
    monkeypatch.setattr(auth, "MODE", "local")
    monkeypatch.setattr(auth, "LOCAL_TOKEN", "")
    c = TestClient(a.app)
    r = c.post("/api/llm", json={"mode": "byok", "anthropic_api_key": "sk-ant-api03-ROUTE-PLAINTEXT"})
    assert r.status_code == 200, r.text
    assert "PLAINTEXT" not in r.text
    assert "PLAINTEXT" not in llm_file.CONFIG.read_text()
    assert llm_file.load()["anthropic_api_key"] == "sk-ant-api03-ROUTE-PLAINTEXT"


def test_a_key_that_no_longer_decrypts_is_no_key(llm_file):
    llm = llm_file
    llm.save({"mode": "byok", "anthropic_api_key": "sk-ant-api03-PLAINTEXT"})
    raw = json.loads(llm.CONFIG.read_text())
    raw["anthropic_api_key_enc"] = "gAAAAAB-not-a-token"
    llm.CONFIG.write_text(json.dumps(raw))
    assert llm.load().get("anthropic_api_key") == "" and not llm.configured()


def test_rotation_reencrypts_llm_json(llm_file):
    llm = llm_file
    llm.save({"mode": "byok", "anthropic_api_key": "sk-ant-api03-PLAINTEXT"})
    before = llm.CONFIG.read_text()
    assert llm.reencrypt() == 1
    assert llm.CONFIG.read_text() != before and "PLAINTEXT" not in llm.CONFIG.read_text()
    assert llm.load()["anthropic_api_key"] == "sk-ant-api03-PLAINTEXT"


# ---------------------------------------------------------------- /api/version commit (D11)

@pytest.mark.parametrize("env,want", [
    ({"SOKKAN_COMMIT": "abcdef1234"}, "abcdef1"),
    ({"SOKKAN_COMMIT": "unknown", "SOKKAN_VERSION": "3.5.1+6f7fee5"}, "6f7fee5"),
    ({"SOKKAN_COMMIT": "", "SOKKAN_VERSION": "3.5.1+6f7fee5"}, "6f7fee5"),
    ({"SOKKAN_COMMIT": "1111111", "SOKKAN_VERSION": "3.5.1+6f7fee5"}, "1111111"),   # the build wins
])
def test_version_commit_comes_from_the_build_then_from_the_installed_release(monkeypatch, env, want):
    import app as a
    for k in ("SOKKAN_COMMIT", "SOKKAN_VERSION"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    v = a._build_version()
    assert v["commit"] == want and v["dist"].endswith("+" + want)


def test_version_commit_is_never_the_word_unknown_from_the_build_arg(monkeypatch, tmp_path):
    """No build-arg, no pinned release, no git: `unknown` — but never because the literal
    default of the Dockerfile was taken for a commit."""
    import app as a
    monkeypatch.setenv("SOKKAN_COMMIT", "unknown")
    monkeypatch.setenv("SOKKAN_VERSION", "dev")
    monkeypatch.setattr(a.subprocess, "run", lambda *x, **k: (_ for _ in ()).throw(OSError()))
    v = a._build_version()
    assert v["commit"] == "unknown" and "+" not in v["dist"]
