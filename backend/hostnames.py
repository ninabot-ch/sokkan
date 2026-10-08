#!/usr/bin/env python3
"""hostnames.py — SOKKAN 3.2.3: a name for every IP the cockpit shows.

Prometheus targets, the Infra topology, Magnitude endpoints and alert payloads carry
bare addresses (`100.124.110.23:9100`). People think in host names. `resolve(ip)`
returns the best name it can find, in this order (first hit wins):

  1. `SOKKAN_HOSTS` — JSON object ip → name (or ip → {"name": …}), set by the operator;
  2. `SOKKAN_INFRA_NODES` — the Infra topology map (ip → {"name", "role"});
  3. `/etc/hosts` (path: `SOKKAN_HOSTS_FILE`) — first non-localhost alias of the address;
  4. Tailscale — `tailscale status --json` when the binary is there (MagicDNS name,
     first label: `rog1.tail1234.ts.net` → `rog1`), cached 60 s;
  5. reverse DNS (`gethostbyaddr`, bounded to `SOKKAN_RDNS_TIMEOUT_S`, default 0.5 s),
     cached 10 min, misses included.

`label(ip)` = "name (ip)" when a name was found, else the ip. `annotate_text()` rewrites
every IPv4 address in a free text the same way (alert summaries). Nothing here raises:
an unresolved address is shown as it is.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

_IPV4 = re.compile(r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?![\d.])")
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
                "broadcasthost"}

_lock = threading.Lock()
_ts_cache: dict = {"at": 0.0, "map": {}}
_rdns_cache: dict[str, tuple[float, str | None]] = {}
_TS_TTL_S = 60.0
_RDNS_TTL_S = 600.0
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="rdns")


def _short(name: str) -> str:
    """`rog1.tail1234.ts.net.` → `rog1`; any other FQDN is kept (minus the final dot)."""
    n = (name or "").strip().rstrip(".")
    if n.endswith(".ts.net"):
        return n.split(".", 1)[0]
    return n


def _json_env(var: str) -> dict:
    raw = (os.environ.get(var) or "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _configured() -> dict[str, str]:
    out: dict[str, str] = {}
    for var in ("SOKKAN_INFRA_NODES", "SOKKAN_HOSTS"):   # SOKKAN_HOSTS wins: read last
        for ip, v in _json_env(var).items():
            name = v.get("name") if isinstance(v, dict) else v
            if isinstance(name, str) and name.strip():
                out[str(ip).strip()] = name.strip()
    return out


def _etc_hosts() -> dict[str, str]:
    path = os.environ.get("SOKKAN_HOSTS_FILE") or "/etc/hosts"
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split("#", 1)[0].split()
                if len(parts) < 2:
                    continue
                ip, names = parts[0], [n for n in parts[1:] if n.lower() not in _LOCAL_NAMES]
                if names and ip not in out and not ip.startswith("127."):
                    out[ip] = names[0]
    except OSError:
        pass
    return out


def _tailscale() -> dict[str, str]:
    now = time.time()
    with _lock:
        if now - _ts_cache["at"] < _TS_TTL_S:
            return _ts_cache["map"]
    m: dict[str, str] = {}
    exe = shutil.which("tailscale")
    if exe:
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True,
                                 timeout=3).stdout
            d = json.loads(out or "{}")
            peers = [d.get("Self") or {}] + list((d.get("Peer") or {}).values())
            for p in peers:
                name = _short(p.get("DNSName") or "") or (p.get("HostName") or "").strip()
                if not name or name.lower() in _LOCAL_NAMES:
                    continue
                for ip in p.get("TailscaleIPs") or []:
                    m.setdefault(ip, name)
        except (OSError, ValueError, subprocess.SubprocessError):
            m = {}
    with _lock:
        _ts_cache.update(at=now, map=m)
    return m


def _rdns(ip: str) -> str | None:
    now = time.time()
    with _lock:
        hit = _rdns_cache.get(ip)
    if hit and now - hit[0] < _RDNS_TTL_S:
        return hit[1]
    try:
        timeout = float(os.environ.get("SOKKAN_RDNS_TIMEOUT_S") or 0.5)
    except ValueError:
        timeout = 0.5
    name = None
    if timeout > 0:
        try:
            name = _pool.submit(socket.gethostbyaddr, ip).result(timeout=timeout)[0]
        except Exception:  # noqa: BLE001 — timeout, NXDOMAIN, no resolver: no name
            name = None
    name = _short(name) if name and name != ip else None
    with _lock:
        _rdns_cache[ip] = (now, name)
    return name


def resolve(ip: str) -> str | None:
    """Best name of `ip` (or of `ip:port`), None when nothing knows it."""
    host = split_host(ip)[0]
    if not host:
        return None
    if host in ("127.0.0.1", "::1", "localhost"):
        return "localhost"
    if not _IPV4.fullmatch(host):
        return None     # already a name (cadvisor:8080, loki:3100…)
    for source in (_configured, _etc_hosts, _tailscale):
        name = source().get(host)
        if name:
            return name
    return _rdns(host)


def split_host(instance: str) -> tuple[str, str]:
    """`100.1.2.3:9100` → ("100.1.2.3", "9100"); `loki:3100` → ("loki", "3100")."""
    s = (instance or "").strip()
    if s.startswith("["):    # [v6]:port
        host, _, port = s[1:].partition("]")
        return host, port.lstrip(":")
    if s.count(":") == 1:
        host, port = s.split(":")
        return host, port
    return s, ""


def label(ip: str) -> str:
    """"name (ip)" when the address resolves, else the address unchanged."""
    host, _ = split_host(ip)
    name = resolve(host)
    if not name or name == host:
        return ip
    return f"{name} ({ip})"


def describe(instance: str) -> dict:
    """{host, port, name, label} for an `ip:port` Prometheus instance."""
    host, port = split_host(instance)
    name = resolve(host)
    return {"host": host, "port": port, "name": name,
            "label": f"{name} ({instance})" if name and name != host else instance}


def annotate_text(text: str) -> str:
    """Every IPv4 address in `text` → "name (ip)" (unknown addresses left as they are)."""
    if not text:
        return text

    def sub(m: re.Match) -> str:
        ip = m.group(1)
        name = resolve(ip)
        if not name:
            return ip
        # already annotated ("rog1 (100.1.2.3)")? keep it
        before = text[max(0, m.start() - len(name) - 2):m.start()]
        return ip if before == f"{name} (" else f"{name} ({ip})"

    return _IPV4.sub(sub, text)


def clear_caches() -> None:
    with _lock:
        _ts_cache.update(at=0.0, map={})
        _rdns_cache.clear()
