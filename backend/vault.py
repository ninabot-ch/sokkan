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
_lock = threading.RLock()


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


FORMAT = 2   # {"format": 2, "projects": {slug: {NAME: token}}, "instance": {NAME: token}}


def _read() -> dict:
    try:
        with open(STORE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _migrate(d: dict) -> tuple[dict, bool]:
    """3.2 lot 4: the flat 3.1 vault ({NAME: token}) becomes namespaced per project; every
    existing secret goes to `default`. Idempotent; returns (vault, changed)."""
    if d.get("format") == FORMAT and isinstance(d.get("projects"), dict):
        d.setdefault("instance", {})
        return d, False
    flat = {k: v for k, v in d.items() if isinstance(v, str) and valid_name(k)}
    return {"format": FORMAT, "projects": {"default": flat} if flat else {},
            "instance": {}}, True


def _load() -> dict:
    """The namespaced vault. A 3.1 file is migrated in place on first read (the original
    is kept once as vault.json.v1.bak, 0600 — what a rollback to 3.1 restores)."""
    d, changed = _migrate(_read())
    if changed and os.path.exists(STORE):
        with _lock:
            raw = _read()
            d, changed = _migrate(raw)
            if changed:
                bak = STORE + ".v1.bak"
                if not os.path.exists(bak):
                    with open(bak, "w") as f:
                        json.dump(raw, f, indent=2)
                    os.chmod(bak, 0o600)
                _save(d)
    return d


def _save(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def namespace(project: str | None) -> str | None:
    """Vault namespace a project's sessions, agents and admins use — None = no vault.

    `default` always has its namespace (= the 3.1 vault). Another project has its own only
    with the feature `project_vault_budgets` on (otherwise none: fail-closed, a secret of
    one team never reaches another team's session). `shared` (read-only knowledge for
    everyone) never holds secrets: a secret seen by every project would defeat the scope."""
    p = (project or "").strip()
    if p == "default":
        return p
    if not p or p == "shared":
        return None
    import features
    import projects
    if not features.enabled("project_vault_budgets") or not projects.valid_slug(p):
        return None
    return p


class VaultOff(ValueError):
    """The project has no vault (feature off, `shared`, unknown project)."""


def _ns_or_raise(project: str | None) -> str:
    ns = namespace(project)
    if ns is None:
        raise VaultOff(f"project {project!r} has no vault (per-project vault: feature "
                       "project_vault_budgets; `shared` never holds secrets)")
    return ns


def session_mode() -> str:
    """SOKKAN_SESSION_SECRETS : `named` (DÉFAUT depuis 3.2 — une session humaine ne
    reçoit que les secrets choisis à son ouverture ou par son playbook, rien sinon) ou
    `all` (comportement 3.1, à poser explicitement : toute session reçoit tout le coffre).
    Une valeur inconnue = `named` (le choix le plus restrictif, jamais « tout »)."""
    import features  # registry: `named_secrets` (forced on managed instances, B1)
    return "named" if features.enabled("named_secrets") else "all"


def managed() -> bool:
    """A SOKKAN Cloud managed instance (SOKKAN_TIER is seeded by the provisioning)."""
    return bool((os.environ.get("SOKKAN_TIER") or "").strip())


def mode_warning() -> str | None:
    """Shown in the cockpit when sessions receive the whole vault (B1)."""
    if session_mode() == "all":
        return ("Every session receives the whole vault (SOKKAN_SESSION_SECRETS=all). "
                "Set it to `named` so a session only gets the secrets picked for it.")
    return None


def upgrade_notice() -> str | None:
    """Message de démarrage pour une installation 3.1 qui passe en 3.2 sans avoir choisi :
    le coffre n'est pas vide et SOKKAN_SESSION_SECRETS n'est pas posé."""
    if os.environ.get("SOKKAN_SESSION_SECRETS") or not names():
        return None
    return ("3.2: sessions now receive only the vault secrets picked when they are opened "
            "(SOKKAN_SESSION_SECRETS=named, the new default). Sessions opened before the "
            "upgrade get none after a restart. Set SOKKAN_SESSION_SECRETS=all to keep the "
            "3.1 behaviour.")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def names(project: str = "default") -> list[str]:
    """Noms des secrets d'UN projet (JAMAIS les valeurs) — pour l'UI et le MCP."""
    ns = namespace(project)
    return sorted((_load()["projects"].get(ns) or {}).keys()) if ns else []


def set_secret(name: str, value: str, project: str = "default") -> None:
    if not valid_name(name):
        raise ValueError("le nom doit être une variable d'environnement (A-Z, 0-9, _)")
    ns = _ns_or_raise(project)
    f = Fernet(_key())
    with _lock:
        d = _load()
        d["projects"].setdefault(ns, {})[name] = f.encrypt(value.encode()).decode()
        _save(d)


def delete_secret(name: str, project: str = "default") -> None:
    ns = _ns_or_raise(project)
    with _lock:
        d = _load()
        (d["projects"].get(ns) or {}).pop(name, None)
        _save(d)


def session_env(only: list[str] | None = None, project: str = "default") -> dict[str, str]:
    """{NAME: valeur déchiffrée} à merger dans l'env des sessions. Appelé côté
    serveur uniquement (agentchat), jamais renvoyé à l'UI ni au LLM.
    `project` (3.2 lot 4) : le coffre de CE projet seulement (rien si le projet n'en a
    pas). `only` = les seuls noms voulus (runs d'agent, mode `named`) ; None = tout le
    coffre du projet (mode `all`)."""
    ns = namespace(project)
    if ns is None:
        return {}
    f = Fernet(_key())
    out: dict[str, str] = {}
    for k, v in (_load()["projects"].get(ns) or {}).items():
        if only is not None and k not in only:
            continue
        try:
            out[k] = f.decrypt(v.encode()).decode()
        except Exception:  # noqa: BLE001 — secret corrompu / clé changée : on saute
            continue
    return out
