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


def credit_checkout(pack: int) -> dict:
    """Demande une URL Stripe Checkout pour un pack de crédits d'inférence.
    Le wallet est crédité par le portail APRÈS paiement (webhook)."""
    r = httpx.post(f"{URL}/fleet/credit", headers=_h(), timeout=30, json={"pack": pack})
    r.raise_for_status()
    return r.json()


def remove_resource(rid: int) -> dict:
    """Résilie une ressource : crédit du prorata restant + destroy (données perdues)."""
    r = httpx.delete(f"{URL}/fleet/resource/{rid}", headers=_h(), timeout=60)
    r.raise_for_status()
    return r.json()


def add_route(kind: str, name: str = "", hostname: str = "",
              target: str = "cockpit", port: int = 80) -> dict:
    """Route d'exposition web (gratuite) : 'subdomain' (<name>-<tenant>.sokkan.ch,
    via tunnel) ou 'custom' (domaine du client, via le caddy edge local)."""
    r = httpx.post(f"{URL}/fleet/routes", headers=_h(), timeout=60,
                   json={"kind": kind, "name": name, "hostname": hostname,
                         "target": target, "port": port})
    r.raise_for_status()
    return r.json()


def remove_route(rid: int) -> dict:
    r = httpx.delete(f"{URL}/fleet/route/{rid}", headers=_h(), timeout=60)
    r.raise_for_status()
    return r.json()


def refresh_edge() -> None:
    """Re-rend le Caddyfile edge depuis la vue portail (appelé après chaque
    mutation de route pour ne pas attendre le tick de 120 s)."""
    import edge
    try:
        edge.render(view())
    except Exception:  # noqa: BLE001 — le sync périodique rattrapera
        pass


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
    import edge
    while True:
        try:
            v = view()
            sync_hosts(v)
            edge.render(v)  # Caddyfile des domaines custom (fleet edge)
        except Exception:  # noqa: BLE001 — portail injoignable : on réessaie
            pass
        time.sleep(120)


def start_sync() -> None:
    """Lancé au démarrage de l'app (mode managé uniquement)."""
    if ENABLED:
        # baseline immédiate : le conteneur caddy attend le Caddyfile pour
        # démarrer — ne pas le laisser bloqué si le portail est injoignable
        import edge
        edge.ensure_baseline()
        threading.Thread(target=_sync_loop, daemon=True).start()
        threading.Thread(target=_register_key, daemon=True).start()


# --- clé de MAINTENANCE de la flotte -----------------------------------------
# Générée une fois dans /data, enregistrée auprès du portail qui l'installe
# (root) sur les instances de la flotte. Sert UNIQUEMENT le terminal de
# maintenance (fleetterm.py) — admin de l'instance, ou user explicitement
# autorisé par lui.
SSH_DIR = os.path.join(os.environ.get("SOKKAN_DATA_DIR", "/data"), "fleet_ssh")
KEY_PATH = os.path.join(SSH_DIR, "id_ed25519")


def ensure_keypair() -> str:
    """Crée la paire si absente ; retourne la clé publique."""
    if not os.path.exists(KEY_PATH):
        os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
        import subprocess
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                        "-C", "sokkan-fleet-maintenance", "-f", KEY_PATH], check=True)
    return open(KEY_PATH + ".pub").read().strip()


def _register_key() -> None:
    """Enregistre la clé publique auprès du portail (retries : le portail ou le
    provisioner peuvent être indisponibles au boot)."""
    for _ in range(10):
        try:
            pub = ensure_keypair()
            httpx.post(f"{URL}/fleet/sshkey", headers=_h(), timeout=30,
                       json={"pubkey": pub}).raise_for_status()
            return
        except Exception:  # noqa: BLE001
            time.sleep(60)
