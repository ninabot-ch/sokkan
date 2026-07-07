#!/usr/bin/env python3
"""llm.py — SOKKAN : configuration LLM par instance (BYOK ou inférence incluse).

Une instance cliente choisit COMMENT ses sessions s'authentifient auprès du
modèle, sans recreate du container : la config vit dans un JSON persistant
(`$SOKKAN_DATA_DIR/llm.json`) et `agentchat` injecte l'env correspondant à
CHAQUE session (ClaudeAgentOptions.env).

Deux modes :
- **byok**     : la clé Anthropic du client. Ses sessions tapent Anthropic en
                 direct — la clé ne quitte jamais SA VM (souverain, zéro coût pour NINABOT).
- **included** : routées vers la passerelle NINABOT (`infer.sokkan.ch`) qui
                 compte les tokens + applique le quota + forward vers Qwen/Anthropic.

Format `llm.json` :
  {"mode": "byok", "anthropic_api_key": "sk-ant-..."}
  {"mode": "included", "base_url": "https://infer.sokkan.ch",
   "auth_token": "sik_...", "model": "qwen3-coder-plus"}
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG = Path(os.environ.get(
    "SOKKAN_LLM_CONFIG",
    os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
                 "llm.json")))


def load() -> dict:
    """Config LLM : llm.json (posé par le cockpit) prioritaire, sinon fallback
    sur l'env `included` seedé au provisioning (SOKKAN_INFER_BASE_URL/TOKEN/MODEL)."""
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
        if c:
            return c
    except (FileNotFoundError, ValueError):
        pass
    base = os.environ.get("SOKKAN_INFER_BASE_URL", "")
    tok = os.environ.get("SOKKAN_INFER_TOKEN", "")
    if base and tok:  # instance provisionnée en « inférence incluse »
        return {"mode": "included", "base_url": base, "auth_token": tok,
                "model": os.environ.get("SOKKAN_INFER_MODEL", "")}
    return {}


def save(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        CONFIG.chmod(0o600)  # contient une clé/token
    except OSError:
        pass


def configured() -> bool:
    c = load()
    if c.get("mode") == "byok":
        return bool(c.get("anthropic_api_key"))
    if c.get("mode") == "included":
        return bool(c.get("base_url") and c.get("auth_token"))
    # pas de config explicite : l'env du container (ANTHROPIC_API_KEY/OAUTH) fait foi
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


def status() -> dict:
    """Résumé non-sensible pour l'UI (jamais la clé)."""
    c = load()
    mode = c.get("mode") or ("env" if configured() else "none")
    return {"mode": mode, "configured": configured(),
            "model": c.get("model") if mode == "included" else None}


def session_env() -> dict:
    """Variables d'env à injecter dans une session (surcharge de os.environ)."""
    c = load()
    if c.get("mode") == "byok" and c.get("anthropic_api_key"):
        # clé client → Anthropic direct ; on neutralise un éventuel base_url hérité
        return {"ANTHROPIC_API_KEY": c["anthropic_api_key"], "ANTHROPIC_BASE_URL": "",
                "ANTHROPIC_AUTH_TOKEN": ""}
    if c.get("mode") == "included" and c.get("base_url") and c.get("auth_token"):
        return {"ANTHROPIC_BASE_URL": c["base_url"].rstrip("/"),
                "ANTHROPIC_AUTH_TOKEN": c["auth_token"],
                "ANTHROPIC_API_KEY": c["auth_token"]}  # gateway accepte x-api-key OU Bearer
    return {}


def session_model() -> str | None:
    """Modèle à forcer (mode included → l'ID upstream, ex. qwen3-coder-plus)."""
    c = load()
    if c.get("mode") == "included":
        return c.get("model") or None
    return None
