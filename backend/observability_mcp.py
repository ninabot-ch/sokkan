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


# ---- 3.5 Operate › Alerts ------------------------------------------------------------------------
# Reads (rules, alerts, backtest) are auto-approved; a session never turns a rule on: it
# PROPOSES one (saved disabled, marked « proposed »), a person reviews the backtest and
# enables it in Operate › Alerts — like Crew agents.
def _project() -> str:
    return (os.environ.get("SOKKAN_SESSION_PROJECT") or "default").strip() or "default"


def _who() -> str:
    u = (os.environ.get("SOKKAN_SESSION_USER") or "").strip()
    sid = (os.environ.get("SOKKAN_SESSION_ID") or "").strip()
    return u or f"session:{sid[:8]}"


@mcp.tool()
def alerting_list_rules() -> str:
    """Alert rules of this project with their state (ok, pending, firing, silenced, error,
    disabled), their meaning in words and the last value seen."""
    try:
        from alerting import rules
        out = [{"id": r["id"], "name": r["name"], "state": r["state"].get("state"),
                "sentence": r.get("sentence"), "severity": r["severity"],
                "last_value": r["state"].get("last_value"), "error": r["state"].get("error")}
               for r in rules.list_rules(_project())]
        return json.dumps(out)[:6000]
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


@mcp.tool()
def alerting_list_alerts(state: str = "active") -> str:
    """Alerts of this project: active (firing + pending, default), firing, pending or resolved."""
    try:
        from alerting import alerts
        out, counts = alerts.list_alerts(_project(), state, 50)
        return json.dumps({"counts": counts, "alerts": [
            {k: a[k] for k in ("id", "rule_name", "state", "severity", "summary", "group",
                               "fired_at", "acked_by", "incident_id")} for a in out]})[:6000]
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def _source_for(rule: dict) -> dict | None:
    from alerting import sources
    sid = rule.get("source_id")
    if sid:
        return sources.get(int(sid), _project())
    kind = rule.pop("source_kind", None) or "prometheus"
    return next((s for s in sources.list_sources(_project()) if s["kind"] == kind), None)


@mcp.tool()
def alerting_preview(rule: dict, range: str = "24h") -> str:  # noqa: A002
    """Backtest an alert rule WITHOUT saving it: how many times it would have fired over the
    range (1h, 6h, 24h, 7d), when, and the values. `rule` = {name, source_kind or source_id,
    query: {mode: "raw", raw: "<PromQL / LogQL / Lucene>"} or {mode: "builder", builder: {...}},
    type: threshold|any|frequency|spike|flatline|change|new_term|cardinality|absence|anomaly,
    params: {...}, for: "5m", group_by: [...]}. Example: {"name": "API 5xx", "source_kind":
    "prometheus", "query": {"mode": "raw", "raw": "sum(rate(http_requests_total{status=~\"5..\"}[5m]))"},
    "type": "threshold", "params": {"op": ">", "value": 1}, "for": "5m"}."""
    try:
        import time as _t
        from alerting import engine, sources
        rule = dict(rule)
        src = _source_for(rule)
        r = engine.normalize({**rule, "name": rule.get("name") or "preview"}, src)
        d = sources._raw(src["id"])
        d["_project"] = _project()
        p = engine.preview(r, src, sources.adapter_for(d), range, _t.time())
        p["series"] = [{"group_key": s["group_key"], "last": s["points"][-1] if s["points"] else None,
                        "max": max((v for _, v in s["points"]), default=None)} for s in p["series"]]
        return json.dumps(p)[:6000]
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


@mcp.tool()
def alerting_propose_rule(rule: dict, why: str = "") -> str:
    """Propose an alert rule for this project (same shape as alerting_preview). It is saved
    DISABLED and marked as proposed: a person reviews its backtest in Operate › Alerts and
    turns it on. Run alerting_preview first so the proposal does not ring all day."""
    try:
        from alerting import rules
        import audit
        rule = dict(rule)
        src = _source_for(rule)
        if src is None:
            return "error: no such source in this project"
        body = {**rule, "source_id": src["id"], "enabled": False,
                "labels": {**(rule.get("labels") or {}), "proposed_by": _who()[:120]},
                "description": (rule.get("description") or why or "")[:2000]}
        r = rules.create(_project(), body, _who())
        audit.log(_who(), "alerting.rule.propose", f"#{r['id']} {r['name']}", why[:300],
                  project=_project())
        return json.dumps({"ok": True, "id": r["id"], "sentence": r["sentence"],
                           "status": "proposed — a person enables it in Operate › Alerts",
                           "link": f"/?plane=operate&tab=alerts&rule={r['id']}"})
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


if __name__ == "__main__":
    mcp.run()
