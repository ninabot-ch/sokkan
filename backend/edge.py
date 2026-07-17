#!/usr/bin/env python3
"""edge.py — SOKKAN fleet edge : exposition web des domaines CUSTOM du client.

Deux chemins d'exposition en mode managé (routes gérées au portail, onglet
« Ma flotte ») :
- 'subdomain' (`<name>-<tenant>.sokkan.ch`) : ingress du tunnel cloudflared,
  géré côté control plane — rien à faire ici.
- 'custom' (domaine du client, CNAME → `edge-<tenant>.sokkan.ch`) : terminé
  par le conteneur caddy local (profil compose `edge`), TLS Let's Encrypt
  on-demand. Ce module rend le Caddyfile depuis la vue flotte et répond au
  check `ask` de caddy (un cert n'est émis QUE pour un hostname enregistré).

Les cibles sont rendues PAR IP privée (slot statique .fleet, stable à vie) :
aucune dépendance DNS sur le chemin de requête.
"""
from __future__ import annotations

import os
import tempfile

DIR = os.path.join(os.environ.get("SOKKAN_DATA_DIR", "/data"), "edge")
CADDYFILE = os.path.join(DIR, "Caddyfile")

# hostnames custom autorisés (mémoire process, rafraîchi à chaque rendu — le
# `ask` de caddy doit répondre vite et sans appel réseau)
_known: set[str] = set()


def allowed(domain: str) -> bool:
    return domain.lower().rstrip(".") in _known


def _caddyfile(view: dict) -> str:
    """Rend le Caddyfile depuis la vue flotte du portail. Sans route custom :
    options globales seules (aucun listener) — caddy tourne à vide."""
    addr = {"cockpit": view.get("cockpit_ip") or "127.0.0.1"}
    for r in view.get("resources") or []:
        host = r.get("fleet_host")
        if host and r.get("private_ip"):
            addr[host.removesuffix(".fleet")] = r["private_ip"]
    routes = [r for r in view.get("routes") or []
              if r.get("kind") == "custom" and addr.get(r.get("target"))]
    out = [
        "# généré par backend/edge.py — ne pas éditer (écrasé au sync flotte)",
        "{",
        "\ton_demand_tls {",
        "\t\task http://api:8097/api/edge/ask",
        "\t}",
        "}",
    ]
    if routes:
        out += ["", "https:// {", "\ttls {", "\t\ton_demand", "\t}"]
        for i, r in enumerate(routes):
            out += [f"\t@r{i} host {r['hostname']}",
                    f"\thandle @r{i} {{",
                    f"\t\treverse_proxy {addr[r['target']]}:{r['port']}",
                    "\t}"]
        out += ["\thandle {", '\t\trespond "sokkan edge: unknown host" 404', "\t}", "}"]
    return "\n".join(out) + "\n"


def ensure_baseline() -> None:
    """Au boot : écrit un Caddyfile minimal SEULEMENT s'il n'existe pas (le
    conteneur caddy l'attend pour démarrer). Ne touche jamais un fichier
    existant — si le portail est down au restart, les routes déjà rendues
    doivent continuer à servir."""
    if not os.path.exists(CADDYFILE):
        render({})
    else:
        # ré-amorce l'allowlist `ask` depuis le fichier (les certs déjà émis
        # restent servis par caddy ; l'allowlist complète revient au 1er sync)
        try:
            for line in open(CADDYFILE):
                line = line.strip()
                if line.startswith("@") and " host " in line:
                    _known.add(line.split(" host ", 1)[1].strip())
        except OSError:
            pass


def render(view: dict) -> None:
    """Écrit le Caddyfile (atomique, seulement si changé — caddy tourne en
    --watch, on évite les reloads pour rien) + met à jour l'allowlist `ask`."""
    global _known
    _known = {r["hostname"] for r in view.get("routes") or [] if r.get("kind") == "custom"}
    body = _caddyfile(view)
    try:
        os.makedirs(DIR, exist_ok=True)
        if os.path.exists(CADDYFILE) and open(CADDYFILE).read() == body:
            return
        fd, tmp = tempfile.mkstemp(dir=DIR)
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, CADDYFILE)
    except OSError:
        pass  # volume absent (self-hosted sans profil edge) : rien à servir
