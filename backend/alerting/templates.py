"""templates.py — ready-made rules (the « easy to set up » part). Each template knows what it
needs (metrics, a log source) and says whether this instance has it: `available` + `missing`.
The rule it returns is a complete RuleIn with the variables' defaults applied; the UI lets the
person change the variables, then the backtest shows what it would have done."""
from __future__ import annotations

import time

from . import sources

_cache: dict = {}


def _metrics(src: dict) -> set[str]:
    k = ("m", src["id"])
    hit = _cache.get(k)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        names = set(sources.adapter(src["id"]).suggest("metrics"))
        if len(names) >= 200:       # suggest() caps at 200: ask Prometheus for the full list
            ad = sources.adapter(src["id"])
            names = set(ad._req("GET", "/api/v1/label/__name__/values").get("data") or [])
    except sources.SourceError:
        names = set()
    _cache[k] = (time.time(), names)
    return names


def _labels(src: dict, metric: str) -> set[str]:
    k = ("l", src["id"], metric)
    hit = _cache.get(k)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        labs = set(sources.adapter(src["id"]).suggest("labels", metric=metric))
    except sources.SourceError:
        labs = set()
    _cache[k] = (time.time(), labs)
    return labs


def _q(builder: dict | None = None, raw: str = "") -> dict:
    return {"mode": "raw" if raw else "builder", "raw": raw, "builder": builder or {}}


def _base(name: str, src: dict | None, **kw) -> dict:
    r = {"name": name, "description": "", "severity": "warning", "enabled": True,
         "source_id": src["id"] if src else None, "every": "1m", "for": "5m", "group_by": [],
         "realert": "1h", "channels": [], "actions": {"incident": False, "diag_session": False,
                                                      "agent_id": None, "runbook": None},
         "labels": {}, "runbook_url": "", "level": None,
         "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00", "tz": "Europe/Zurich",
                         "days": [0, 1, 2, 3, 4, 5, 6]}}
    r.update(kw)
    return r


def _v(key, label, default, unit=""):
    return {"key": key, "label": label, "default": default, "unit": unit}


HTTP_METRICS = ("http_requests_total", "http_server_requests_seconds_count",
                "nginx_http_requests_total", "traefik_service_requests_total",
                "caddy_http_requests_total", "starlette_requests_total", "fastapi_requests_total")


def build(project: str, values: dict | None = None) -> list[dict]:
    """Every template, with availability against this project's sources."""
    vals = values or {}
    srcs = sources.list_sources(project)
    prom = next((s for s in srcs if s["kind"] == "prometheus"), None)
    logs = next((s for s in srcs if s["kind"] in ("loki", "elasticsearch", "opensearch")), None)
    sok = next((s for s in srcs if s["kind"] == "sokkan"), None)
    metrics = _metrics(prom) if prom else set()
    out: list[dict] = []

    def add(tid, name, cat, icon, desc, kind, src, variables, make, need=None):
        v = {x["key"]: vals.get(tid, {}).get(x["key"], x["default"]) for x in variables}
        missing = None
        if src is None:
            missing = {"prometheus": "No Prometheus source (set SOKKAN_PROM or add one in Sources)",
                       "logs": "No log source (Loki, Elasticsearch or OpenSearch — add one in Sources)",
                       "sokkan": "SOKKAN events unavailable"}[kind]
        elif need:
            missing = need()
        rule = None
        if src is not None and missing is None:
            try:
                rule = make(v, src)
            except Exception as e:  # noqa: BLE001 — a template that cannot be built is unavailable
                missing = str(e)
        out.append({"id": tid, "name": name, "category": cat, "icon": icon, "description": desc,
                    "source_kind": src["kind"] if src else kind, "available": rule is not None,
                    "missing": missing, "source_id": src["id"] if src else None,
                    "variables": variables, "rule": rule})

    # ---------------------------------------------------------------- Web
    http = next((m for m in HTTP_METRICS if m in metrics), None)

    def need_http():
        return None if http else "No HTTP metrics found (needs http_requests_total or similar)"

    def code_label():
        labs = _labels(prom, http)
        return next((x for x in ("status", "code", "status_code", "http_status") if x in labs), "status")

    add("http-5xx-rate", "Too many 5xx errors", "Web", "globe",
        "Share of HTTP responses in 5xx above a limit for a few minutes.", "prometheus", prom,
        [_v("threshold", "Error share", 2, "%"), _v("for", "For at least", "5m", "duration")],
        lambda v, s: _base("Too many 5xx errors", s, severity="critical", **{"for": v["for"]},
                           query=_q({"metric": http, "agg": "rate", "range": "5m",
                                     "filters": [{"label": code_label(), "op": "=~", "value": "5.."}],
                                     "ratio_of": {"metric": http, "agg": "rate", "range": "5m", "filters": []}}),
                           type="threshold", params={"op": ">", "value": v["threshold"], "reduce": "last"}),
        need_http)
    bucket = next((m for m in sorted(metrics) if m.endswith("_duration_seconds_bucket")
                   and ("http" in m or "request" in m)), None)
    add("latency-p95", "Slow responses (p95)", "Web", "gauge",
        "95 % of requests answer within a limit; alert when the slowest 5 % get slower.",
        "prometheus", prom, [_v("threshold", "p95 above", 1, "s"), _v("for", "For at least", "10m", "duration")],
        lambda v, s: _base("Slow responses (p95)", s, **{"for": v["for"]},
                           query=_q({"metric": bucket, "agg": "p95", "range": "5m"}),
                           type="threshold", params={"op": ">", "value": v["threshold"], "reduce": "last"}),
        lambda: None if bucket else "No request duration histogram (needs *_request_duration_seconds_bucket)")

    # ---------------------------------------------------------------- Hosts
    node = "node_cpu_seconds_total" in metrics

    def need_node():
        return None if node else "No host metrics (needs node_exporter)"

    add("host-cpu", "Host CPU busy", "Hosts", "cpu",
        "CPU used above a limit on a host, for a while.", "prometheus", prom,
        [_v("threshold", "CPU above", 90, "%"), _v("for", "For at least", "10m", "duration")],
        lambda v, s: _base("Host CPU busy", s, **{"for": v["for"]}, group_by=["instance"],
                           query=_q(raw='100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100'),
                           type="threshold", params={"op": ">", "value": v["threshold"], "reduce": "last"}),
        need_node)
    add("host-memory", "Host memory almost full", "Hosts", "memory",
        "Memory used above a limit on a host.", "prometheus", prom,
        [_v("threshold", "Memory above", 90, "%"), _v("for", "For at least", "10m", "duration")],
        lambda v, s: _base("Host memory almost full", s, **{"for": v["for"]}, group_by=["instance"],
                           query=_q(raw="(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100"),
                           type="threshold", params={"op": ">", "value": v["threshold"], "reduce": "last"}),
        lambda: None if "node_memory_MemAvailable_bytes" in metrics else "No host metrics (needs node_exporter)")
    add("host-disk", "Disk almost full", "Hosts", "disk",
        "A filesystem used above a limit.", "prometheus", prom,
        [_v("threshold", "Disk above", 90, "%")],
        lambda v, s: _base("Disk almost full", s, severity="critical", **{"for": "15m"},
                           group_by=["instance", "mountpoint"], realert="6h",
                           query=_q(raw='(1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} '
                                        '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}) * 100'),
                           type="threshold", params={"op": ">", "value": v["threshold"], "reduce": "last"}),
        lambda: None if "node_filesystem_avail_bytes" in metrics else "No host metrics (needs node_exporter)")
    add("target-down", "Service down", "Hosts", "plug",
        "A target Prometheus scrapes stops answering.", "prometheus", prom,
        [_v("for", "For at least", "2m", "duration")],
        lambda v, s: _base("Service down", s, severity="critical", **{"for": v["for"]},
                           group_by=["job", "instance"],
                           query=_q({"metric": "up", "agg": "last", "filters": []}),
                           type="threshold", params={"op": "<", "value": 1, "reduce": "last"}),
        lambda: None if "up" in metrics else "Prometheus has no `up` metric yet")
    add("metric-silent", "A metric stopped arriving", "Hosts", "pulse",
        "No data at all for a metric (an exporter died, a job was renamed).", "prometheus", prom,
        [_v("metric", "Metric", "up", "metric"), _v("window", "Silent for", "10m", "duration")],
        lambda v, s: _base("A metric stopped arriving", s, **{"for": "0s"},
                           query=_q({"metric": v["metric"], "agg": "last", "filters": []}),
                           type="absence", params={"window": v["window"]}))

    # ---------------------------------------------------------------- Certificates
    add("tls-expiry", "TLS certificate expires soon", "Certificates", "lock",
        "Days left before a certificate probed by blackbox_exporter expires.", "prometheus", prom,
        [_v("days", "Days left below", 14, "days")],
        lambda v, s: _base("TLS certificate expires soon", s, **{"for": "0s"}, every="1h",
                           group_by=["instance"], realert="1d",
                           query=_q(raw="(probe_ssl_earliest_cert_expiry - time()) / 86400"),
                           type="threshold", params={"op": "<", "value": v["days"], "reduce": "last"}),
        lambda: None if "probe_ssl_earliest_cert_expiry" in metrics else
        "No certificate probe (needs blackbox_exporter with an https module)")

    # ---------------------------------------------------------------- Logs
    def lq(text: str = "", level: str = "") -> dict:
        return _q({"filters": [], "text": text, "level": level})

    add("log-errors", "Errors in the logs", "Logs", "alert",
        "Many log lines with « error » in a short time.", "logs", logs,
        [_v("count", "At least", 20, "lines"), _v("window", "Within", "5m", "duration"),
         _v("text", "Lines containing", "error", "text")],
        lambda v, s: _base("Errors in the logs", s, **{"for": "0s"},
                           query=lq(v["text"]), type="frequency",
                           params={"count": v["count"], "window": v["window"]}))
    add("logs-flatline", "Logs stopped", "Logs", "pulse",
        "Fewer log lines than expected: an app or a shipper went silent.", "logs", logs,
        [_v("count", "Fewer than", 1, "lines"), _v("window", "Within", "10m", "duration")],
        lambda v, s: _base("Logs stopped", s, severity="critical", **{"for": "0s"},
                           query=lq(), type="flatline", params={"count": v["count"], "window": v["window"]}))
    add("log-spike", "Sudden burst of logs", "Logs", "chart",
        "Log volume jumps compared with the hour before (a loop, an attack, a crash).", "logs", logs,
        [_v("ratio", "At least", 3, "× the usual"), _v("window", "Over", "10m", "duration")],
        lambda v, s: _base("Sudden burst of logs", s, **{"for": "0s"}, query=lq(), type="spike",
                           params={"ratio": v["ratio"], "direction": "up", "window": v["window"],
                                   "reference": "1h", "min_count": 50}))
    add("new-error", "A new kind of error", "Logs", "sparkle",
        "An error type never seen in the last week shows up.", "logs", logs,
        [_v("field", "Field holding the error type", "error.type" if logs and logs["kind"] != "loki" else "level",
            "field")],
        lambda v, s: _base("A new kind of error", s, **{"for": "0s"}, every="5m", query=lq(),
                           type="new_term", params={"field": v["field"], "lookback": "7d"}))
    add("login-failures", "Repeated failed logins", "Logs", "key",
        "Many failed logins in a short time (brute force, a broken client).", "logs", logs,
        [_v("count", "At least", 10, "failures"), _v("window", "Within", "5m", "duration"),
         _v("text", "Lines containing", "failed login", "text")],
        lambda v, s: _base("Repeated failed logins", s, severity="critical", **{"for": "0s"},
                           query=lq(v["text"]), type="frequency",
                           params={"count": v["count"], "window": v["window"]}))

    # ---------------------------------------------------------------- SOKKAN
    add("agent-run-failed", "An agent run failed", "Agents", "robot",
        "A Crew agent run ended failed, timed out or over budget.", "sokkan", sok, [],
        lambda v, s: _base("An agent run failed", s, **{"for": "0s"}, realert="0s",
                           query=_q({"metric": "agents.failed", "filters": []}), type="any", params={}))
    add("login-refused", "Refused sign-ins", "SOKKAN", "shield",
        "People refused at sign-in (no role, revoked) — several in a short time.", "sokkan", sok,
        [_v("count", "At least", 5, "refusals"), _v("window", "Within", "15m", "duration")],
        lambda v, s: _base("Refused sign-ins", s, **{"for": "0s"},
                           query=_q({"metric": "audit", "filters": [{"field": "action", "op": "=",
                                                                     "value": "auth.login.refused"}]}),
                           type="frequency", params={"count": v["count"], "window": v["window"]}))
    add("project-budget", "Project budget reached", "SOKKAN", "coins",
        "An agent run was stopped because the project's budget is spent.", "sokkan", sok, [],
        lambda v, s: _base("Project budget reached", s, **{"for": "0s"}, realert="0s",
                           query=_q({"metric": "audit", "filters": [{"field": "action", "op": "=",
                                                                     "value": "agent.run.project_budget"}]}),
                           type="any", params={}))
    return out


def apply_variables(t: dict, values: dict, project: str) -> dict | None:
    """The template's rule rebuilt with the person's variables."""
    for x in build(project, {t["id"]: values}):
        if x["id"] == t["id"]:
            return x["rule"]
    return None
