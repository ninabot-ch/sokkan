#!/usr/bin/env python3
"""instance.py — SOKKAN : métadonnées de l'instance/organisation.

Dans SOKKAN, une organisation = une instance (1 client = 1 déploiement). Ce
module porte les infos éditables (nom d'organisation) et en lecture (plan/tier,
URL publique) présentées dans la page Profil & organisation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

STORE = Path(os.environ.get(
    "SOKKAN_INSTANCE_CONFIG",
    os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
                 "instance.json")))


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def info() -> dict:
    c = _load()
    return {
        "org_name": c.get("org_name") or os.environ.get("SOKKAN_ORG_NAME")
        or os.environ.get("SOKKAN_OWNER_NAME", "Mon organisation"),
        "tier": os.environ.get("SOKKAN_TIER", ""),           # seedé au provisioning (optionnel)
        "public_url": os.environ.get("SOKKAN_PUBLIC_URL", ""),
        "owner_email": os.environ.get("SOKKAN_OWNER_EMAIL", ""),
        **budgets(),
    }


def budgets() -> dict:
    """Budgets de coût estimé (USD, 0 = désactivé) : par session (hard stop
    HITL — la session refuse de nouveaux tours au-delà, on relève le budget
    pour continuer) et par jour (avertissement au spawn)."""
    c = _load()
    def _f(key: str, env: str) -> float:
        try:
            return max(0.0, float(c.get(key) if c.get(key) is not None
                                  else os.environ.get(env, 0) or 0))
        except (TypeError, ValueError):
            return 0.0
    return {"budget_session_usd": _f("budget_session_usd", "SOKKAN_BUDGET_SESSION_USD"),
            "budget_day_usd": _f("budget_day_usd", "SOKKAN_BUDGET_DAY_USD")}


def set_budgets(session_usd: float | None = None, day_usd: float | None = None) -> dict:
    c = _load()
    if session_usd is not None:
        c["budget_session_usd"] = max(0.0, float(session_usd))
    if day_usd is not None:
        c["budget_day_usd"] = max(0.0, float(day_usd))
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(c, indent=2), encoding="utf-8")
    return info()


def set_org_name(name: str) -> dict:
    c = _load()
    c["org_name"] = name.strip()[:80]
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(c, indent=2), encoding="utf-8")
    return info()
