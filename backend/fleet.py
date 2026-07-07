#!/usr/bin/env python3
"""fleet.py — SOKKAN : gestion de la flotte du client (mode managé).

Le cockpit du client parle au **portail NINABOT** (app.sokkan.ch) avec un
**fleet token** propre au tenant (seedé au provisioning). Le client voit sa
flotte et demande des ressources (compute/DB) — facturées (proration) et
provisionnées après paiement, côté NINABOT. Le cockpit ne détient AUCUN
credential cloud : il fait des requêtes au control plane (frontière open-core).

Activé seulement si SOKKAN_FLEET_URL + SOKKAN_FLEET_TOKEN sont présents
(instances managées NINABOT ; absent en self-hosted pur → pas d'onglet flotte).
"""
from __future__ import annotations

import os
import re
import threading
import time

import httpx

URL = (os.environ.get("SOKKAN_FLEET_URL") or "").rstrip("/")
TOKEN = os.environ.get("SOKKAN_FLEET_TOKEN", "")
ENABLED = bool(URL and TOKEN)

HOSTS = "/etc/hosts"  # celui du conteneur — les sessions y résolvent `<name>.fleet`
_MARK_A, _MARK_B = "# --- sokkan fleet ---", "# --- end sokkan fleet ---"


def _h() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def view() -> dict:
    """Catalogue + ressources de la flotte + état infra."""
    r = httpx.get(f"{URL}/fleet", headers=_h(), timeout=20)
    r.raise_for_status()
    return r.json()


def request_resource(sku: str, name: str = "") -> dict:
    """Demande une ressource → proration Stripe → pending → provisionnée au paiement."""
    r = httpx.post(f"{URL}/fleet/request", headers=_h(), timeout=30,
                   json={"sku": sku, "name": name})
    r.raise_for_status()
    return r.json()


def sync_hosts(view_data: dict) -> None:
    """Nomenclature réseau des sessions : écrit le bloc `<name>.fleet` dans le
    /etc/hosts du conteneur depuis la vue portail. Les sessions font ensuite
    `ssh worker-ci.fleet`, `psql -h pg-app.fleet`, sans chercher d'IP DHCP."""
    lines = []
    if view_data.get("cockpit_ip"):
        lines.append(f"{view_data['cockpit_ip']} cockpit.fleet cockpit")
    for r in view_data.get("resources") or []:
        host = r.get("fleet_host")
        if host and r.get("private_ip"):
            lines.append(f"{r['private_ip']} {host} {host.removesuffix('.fleet')}")
    try:
        body = open(HOSTS).read()
        body = re.sub(rf"\n?{re.escape(_MARK_A)}.*?{re.escape(_MARK_B)}\n?", "", body, flags=re.S)
        if lines:
            body = body.rstrip("\n") + f"\n{_MARK_A}\n" + "\n".join(lines) + f"\n{_MARK_B}\n"
        open(HOSTS, "w").write(body)
    except OSError:
        pass  # fs read-only / droits : les IPs restent visibles dans l'onglet


def _sync_loop() -> None:
    while True:
        try:
            sync_hosts(view())
        except Exception:  # noqa: BLE001 — portail injoignable : on réessaie
            pass
        time.sleep(120)


def start_sync() -> None:
    """Lancé au démarrage de l'app (mode managé uniquement)."""
    if ENABLED:
        threading.Thread(target=_sync_loop, daemon=True).start()
