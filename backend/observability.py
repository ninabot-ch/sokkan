#!/usr/bin/env python3
"""observability.py — SOKKAN : opérer la prod depuis le cockpit.

Parle à la stack d'observabilité de la flotte (Prometheus + Grafana + Loki,
add-on `observability` en mode managé, ou tes propres URLs en self-host) :
- lecture métriques (PromQL) et logs (LogQL) — utilisée par le MCP pour que les
  sessions diagnostiquent avec la mémoire du projet ;
- écriture Grafana (créer un dashboard depuis une session : « surveille la p95
  et les 5xx de mon API ») ;
- store d'incidents : une alerte prod → un incident → une session de diagnostic
  pré-seedée (voir app.py /api/observability/alert). Le post-mortem retourne
  dans la mémoire → la prochaine fois l'agent sait.

Config par env (seedée au provisioning de l'add-on obs, ou posée en self-host) :
  SOKKAN_PROM              URL Prometheus (partagée avec infra.py)
  SOKKAN_LOKI              URL Loki
  SOKKAN_GRAFANA_URL       URL API Grafana (interne)
  SOKKAN_GRAFANA_PUBLIC_URL URL publique Grafana (iframe embed)
  SOKKAN_GRAFANA_USER/PASSWORD  basic auth API Grafana
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

import httpx

DATA_DIR = os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
DB = os.path.join(DATA_DIR, "incidents.db")

PROM = (os.environ.get("SOKKAN_PROM") or "").rstrip("/")
LOKI = (os.environ.get("SOKKAN_LOKI") or "").rstrip("/")
GRAFANA = (os.environ.get("SOKKAN_GRAFANA_URL") or "").rstrip("/")
GRAFANA_PUBLIC = (os.environ.get("SOKKAN_GRAFANA_PUBLIC_URL") or "").rstrip("/")
GRAFANA_USER = os.environ.get("SOKKAN_GRAFANA_USER", "admin")
GRAFANA_PASSWORD = os.environ.get("SOKKAN_GRAFANA_PASSWORD", "")

ENABLED = bool(PROM or GRAFANA)
_lock = threading.Lock()


def status() -> dict:
    return {
        "enabled": ENABLED,
        "prometheus": bool(PROM),
        "loki": bool(LOKI),
        "grafana": bool(GRAFANA),
        "grafana_public_url": GRAFANA_PUBLIC or None,
    }


# ---- lecture métriques / logs ----------------------------------------------
def query_metrics(promql: str) -> dict:
    """PromQL instantané → résultat brut Prometheus (data.result)."""
    if not PROM:
        raise RuntimeError("Prometheus non configuré sur cette instance")
    r = httpx.get(f"{PROM}/api/v1/query", params={"query": promql}, timeout=15)
    r.raise_for_status()
    return r.json().get("data", {})


def query_logs(logql: str, limit: int = 100, since_s: int = 3600) -> list[dict]:
    """LogQL sur une fenêtre récente → lignes de log (les plus récentes d'abord)."""
    if not LOKI:
        raise RuntimeError("Loki non configuré sur cette instance")
    now = int(time.time() * 1e9)
    r = httpx.get(f"{LOKI}/loki/api/v1/query_range",
                  params={"query": logql, "limit": limit,
                          "start": now - since_s * 1_000_000_000, "end": now,
                          "direction": "backward"}, timeout=15)
    r.raise_for_status()
    out = []
    for stream in r.json().get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            out.append({"ts": ts, "line": line, "labels": labels})
    return out[:limit]


# ---- écriture Grafana -------------------------------------------------------
def _gf() -> httpx.Client:
    return httpx.Client(base_url=GRAFANA, auth=(GRAFANA_USER, GRAFANA_PASSWORD), timeout=20)


def list_dashboards() -> list[dict]:
    if not GRAFANA:
        return []
    with _gf() as c:
        r = c.get("/api/search", params={"type": "dash-db"})
        r.raise_for_status()
        return [{"title": d["title"], "uid": d["uid"], "url": d.get("url")} for d in r.json()]


def create_dashboard(title: str, panels: list[dict]) -> dict:
    """Crée/écrase un dashboard Grafana. `panels` = [{title, expr, unit?}] (PromQL).
    Rendu en grille de timeseries — volontairement simple : l'agent compose la
    liste, Grafana rend."""
    if not GRAFANA:
        raise RuntimeError("Grafana non configuré sur cette instance")
    gpanels = []
    for i, p in enumerate(panels):
        gpanels.append({
            "id": i + 1, "title": p.get("title", p.get("expr", "")[:40]),
            "type": "timeseries",
            "gridPos": {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8},
            "fieldConfig": {"defaults": {"unit": p.get("unit", "short")}, "overrides": []},
            "targets": [{"expr": p["expr"], "refId": "A"}],
            "datasource": {"type": "prometheus", "uid": "prometheus"},
        })
    dash = {"title": title, "panels": gpanels, "schemaVersion": 39,
            "time": {"from": "now-6h", "to": "now"}, "refresh": "30s", "tags": ["sokkan"]}
    with _gf() as c:
        r = c.post("/api/dashboards/db", json={"dashboard": dash, "overwrite": True})
        r.raise_for_status()
        j = r.json()
    return {"uid": j.get("uid"), "url": (GRAFANA_PUBLIC or GRAFANA) + (j.get("url") or "")}


# ---- store d'incidents (alerte → session → post-mortem) --------------------
def _con() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS incidents ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
        " title TEXT, summary TEXT, severity TEXT DEFAULT 'warning',"
        " status TEXT DEFAULT 'open', session_id TEXT DEFAULT '')")
    return con


def record_incident(title: str, summary: str, severity: str = "warning") -> int:
    with _lock:
        con = _con()
        cur = con.execute(
            "INSERT INTO incidents(ts, title, summary, severity) VALUES(?,?,?,?)",
            (time.time(), title[:200], summary[:2000], severity))
        con.commit()
        rid = cur.lastrowid
        con.close()
    return rid


def link_incident_session(rid: int, session_id: str) -> None:
    con = _con()
    con.execute("UPDATE incidents SET session_id=? WHERE id=?", (session_id, rid))
    con.commit()
    con.close()


def set_incident_status(rid: int, status: str) -> None:
    con = _con()
    con.execute("UPDATE incidents SET status=? WHERE id=?", (status, rid))
    con.commit()
    con.close()


def incidents(limit: int = 50) -> list[dict]:
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM incidents ORDER BY ts DESC LIMIT ?", (limit,))]
    con.close()
    return rows
