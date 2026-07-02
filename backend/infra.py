#!/usr/bin/env python3
"""infra.py — SOKKAN P5 : topologie & métriques infra (onglet Infra).

Interroge Prometheus (gmk1:9090) pour les métriques par nœud (CPU/RAM/disque/
charge/uptime) et la santé des targets. Socle de l'IAM à venir et du spawn Exoscale.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

PROM = (os.environ.get("SOKKAN_PROM") or "").rstrip("/")
ENABLED = bool(PROM)

# mapping ip → {name, role}, fourni par l'env (JSON) — ex.
# SOKKAN_INFRA_NODES='{"10.0.0.1": {"name": "prod-1", "role": "app"}}'
NODES = json.loads(os.environ.get("SOKKAN_INFRA_NODES", "{}"))


def _q(expr: str) -> list:
    url = f"{PROM}/api/v1/query?query=" + urllib.parse.quote(expr)
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.load(r)["data"]["result"]


def _by_ip(expr: str) -> dict:
    out: dict[str, float] = {}
    try:
        for r in _q(expr):
            ip = r["metric"].get("instance", "").split(":")[0]
            out[ip] = float(r["value"][1])
    except Exception:  # noqa: BLE001 — Prometheus indispo → métrique absente
        pass
    return out


def nodes() -> list[dict]:
    if not ENABLED:
        return []
    up = _by_ip('up{job="node"}')
    cpu = _by_ip('100 - (avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))*100)')
    memt = _by_ip("node_memory_MemTotal_bytes")
    mema = _by_ip("node_memory_MemAvailable_bytes")
    dsz = _by_ip('node_filesystem_size_bytes{mountpoint="/"}')
    dav = _by_ip('node_filesystem_avail_bytes{mountpoint="/"}')
    load = _by_ip("node_load1")
    cores = _by_ip('count by(instance)(node_cpu_seconds_total{mode="idle"})')
    uptime = _by_ip("node_time_seconds - node_boot_time_seconds")

    ips = list(NODES) + [ip for ip in up if ip not in NODES]
    out = []
    for ip in ips:
        meta = NODES.get(ip, {"name": ip, "role": "?"})
        monitored = ip in up or ip in cpu
        out.append({
            "ip": ip, "name": meta["name"], "role": meta["role"],
            "monitored": monitored,
            "up": (up.get(ip) == 1) if ip in up else None,
            "cpu_pct": round(cpu[ip], 1) if ip in cpu else None,
            "cores": int(cores[ip]) if ip in cores else None,
            "mem_total": memt.get(ip), "mem_avail": mema.get(ip),
            "disk_total": dsz.get(ip), "disk_avail": dav.get(ip),
            "load1": load.get(ip), "uptime_s": uptime.get(ip),
        })
    return out


def targets() -> list[dict]:
    if not ENABLED:
        return []
    out = []
    try:
        for r in _q("up"):
            m = r["metric"]
            out.append({"job": m.get("job"), "instance": m.get("instance"),
                        "up": r["value"][1] == "1"})
    except Exception:  # noqa: BLE001
        pass
    out.sort(key=lambda t: (not t["up"], t["job"] or ""))
    return out
