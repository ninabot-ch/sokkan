#!/usr/bin/env python3
"""vault.py — SOKKAN : petit coffre de secrets par instance.

Un vibecoder ne peut pas opérer sa prod sans clés (API tierces, tokens de deploy,
DSN…). Le coffre stocke ces secrets CHIFFRÉS (Fernet, clé propre à l'instance)
et les **injecte comme variables d'environnement** dans les sessions — l'agent
les UTILISE (`$STRIPE_KEY` dans un shell) sans jamais **voir** la valeur (ni
l'UI, ni le LLM ne la lisent : seuls les NOMS sont exposés).

Cohérent avec la philosophie : les secrets ne quittent jamais la VM du client.
Fichiers sous $SOKKAN_DATA_DIR (chmod 0600) : vault.key + vault.json.
"""
from __future__ import annotations

import json
import os
import re
import threading

from cryptography.fernet import Fernet

DATA_DIR = os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
KEY_PATH = os.path.join(DATA_DIR, "vault.key")
STORE = os.path.join(DATA_DIR, "vault.json")
# nom = variable d'environnement valide (injectée telle quelle dans les sessions)
_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_lock = threading.Lock()


def _key() -> bytes:
    """Clé Fernet de l'instance (générée une fois, 0600)."""
    try:
        with open(KEY_PATH, "rb") as f:
            return f.read().strip()
    except OSError:
        os.makedirs(DATA_DIR, exist_ok=True)
        k = Fernet.generate_key()
        with open(KEY_PATH, "wb") as f:
            f.write(k)
        os.chmod(KEY_PATH, 0o600)
        return k


def _load() -> dict:
    try:
        with open(STORE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STORE, "w") as f:
        json.dump(d, f, indent=2)
    os.chmod(STORE, 0o600)


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def names() -> list[str]:
    """Noms des secrets (JAMAIS les valeurs) — pour l'UI et le MCP."""
    return sorted(_load().keys())


def set_secret(name: str, value: str) -> None:
    if not valid_name(name):
        raise ValueError("le nom doit être une variable d'environnement (A-Z, 0-9, _)")
    f = Fernet(_key())
    with _lock:
        d = _load()
        d[name] = f.encrypt(value.encode()).decode()
        _save(d)


def delete_secret(name: str) -> None:
    with _lock:
        d = _load()
        d.pop(name, None)
        _save(d)


def session_env() -> dict[str, str]:
    """{NAME: valeur déchiffrée} à merger dans l'env des sessions. Appelé côté
    serveur uniquement (agentchat), jamais renvoyé à l'UI ni au LLM."""
    f = Fernet(_key())
    out: dict[str, str] = {}
    for k, v in _load().items():
        try:
            out[k] = f.decrypt(v.encode()).decode()
        except Exception:  # noqa: BLE001 — secret corrompu / clé changée : on saute
            continue
    return out
