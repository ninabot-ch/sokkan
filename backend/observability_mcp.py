#!/usr/bin/env python3
"""observability_mcp.py — SOKKAN : serveur MCP stdio pour que les sessions
opèrent la prod. Une session qui a la mémoire du projet peut lire les métriques
et les logs, et composer un dashboard Grafana à la demande.

Enregistré dans agentchat.MCP_SERVERS (serveur `sokkan-observability`).
Tools de LECTURE (query_metrics, query_logs, list_dashboards) auto-approuvés
via SAFE_TOOLS ; create_dashboard reste soumis au gate de permission (écriture).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observability as obs  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("sokkan-observability")


@mcp.tool()
def query_metrics(promql: str) -> str:
    """Exécute une requête PromQL instantanée sur le Prometheus de la flotte et
    renvoie le résultat (séries + valeurs). Ex: 'histogram_quantile(0.95,
    sum(rate(http_request_duration_seconds_bucket[5m])) by (le))'."""
    try:
        return json.dumps(obs.query_metrics(promql))[:6000]
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


@mcp.tool()
def query_logs(logql: str, limit: int = 100, since_minutes: int = 60) -> str:
    """Interroge les logs via LogQL (Loki) sur les `since_minutes` dernières
    minutes. Ex: '{container="api"} |= "error"'. Renvoie les lignes les plus
    récentes d'abord."""
    try:
        rows = obs.query_logs(logql, limit=limit, since_s=since_minutes * 60)
        return json.dumps(rows)[:6000]
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


@mcp.tool()
def list_dashboards() -> str:
    """Liste les dashboards Grafana existants (titre + uid + url)."""
    try:
        return json.dumps(obs.list_dashboards())
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


@mcp.tool()
def create_dashboard(title: str, panels: list) -> str:
    """Crée (ou écrase) un dashboard Grafana. `panels` = liste d'objets
    {title, expr, unit?} où expr est une requête PromQL. Ex pour surveiller une
    API : [{"title":"p95 latency","expr":"histogram_quantile(0.95, ...)",
    "unit":"s"}, {"title":"5xx rate","expr":"sum(rate(...{status=~\\"5..\\"}[5m]))"}].
    Renvoie l'URL du dashboard créé."""
    try:
        norm = [p if isinstance(p, dict) else {"expr": str(p)} for p in panels]
        return json.dumps(obs.create_dashboard(title, norm))
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


if __name__ == "__main__":
    mcp.run()
