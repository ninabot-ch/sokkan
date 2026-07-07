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

import httpx

URL = (os.environ.get("SOKKAN_FLEET_URL") or "").rstrip("/")
TOKEN = os.environ.get("SOKKAN_FLEET_TOKEN", "")
ENABLED = bool(URL and TOKEN)


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
