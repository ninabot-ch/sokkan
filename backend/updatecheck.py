#!/usr/bin/env python3
"""updatecheck.py — SOKKAN : check de mise à jour quotidien (opt-out).

Un GET par jour sur `$SOKKAN_DIST_BASE/dist/VERSION` pour savoir si une
nouvelle version est publiée. La SEULE donnée transmise est la requête
elle-même (IP + User-Agent `sokkan-{selfhost|cloud}/<version locale>`) —
rien d'autre ne quitte l'instance, aucun identifiant, aucune télémétrie.
Désactivation : `SOKKAN_UPDATE_CHECK=0` dans `.env`.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request

BASE = os.environ.get("SOKKAN_DIST_BASE", "https://sokkan.ch").rstrip("/")
LOCAL = os.environ.get("SOKKAN_VERSION", "dev")
ENABLED = os.environ.get("SOKKAN_UPDATE_CHECK", "1").strip().lower() not in (
    "0", "false", "no", "off")
INTERVAL_S = 24 * 3600

_state: dict = {"latest": None, "update_available": False, "checked_at": None}


def state() -> dict:
    """Pour /api/instance : version locale + dernier résultat du check."""
    return {"local_version": LOCAL, "check_enabled": ENABLED, **_state}


def _flavor() -> str:
    # SOKKAN_TIER est seedé au provisioning des instances managées SOKKAN Cloud
    return "cloud" if os.environ.get("SOKKAN_TIER") else "selfhost"


def _check_once() -> None:
    req = urllib.request.Request(
        f"{BASE}/dist/VERSION",
        headers={"User-Agent": f"sokkan-{_flavor()}/{LOCAL}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        latest = r.read().decode("utf-8", "replace").strip()[:40]
    _state.update(
        latest=latest or None, checked_at=int(time.time()),
        update_available=bool(latest) and LOCAL not in ("dev", "?", "")
        and latest != LOCAL)
    if _state["update_available"]:
        print(f"[sokkan] mise à jour disponible : {LOCAL} → {latest} "
              f"(curl -fsSL {BASE}/install.sh | sh)", file=sys.stderr)


def _loop() -> None:
    time.sleep(90)  # laisse l'instance finir de démarrer
    while True:
        try:
            _check_once()
        except Exception:  # noqa: BLE001 — réseau coupé/air-gap : silencieux
            pass
        time.sleep(INTERVAL_S)


def start() -> None:
    if ENABLED:
        threading.Thread(target=_loop, daemon=True,
                         name="sokkan-updatecheck").start()
