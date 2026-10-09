"""humanize.py — what a person reads: host names instead of addresses, values with their unit,
« down » instead of « 0 < 1 », raw PromQL said in words.

Shared by the API (preview, rules, alerts), the messages (every channel) and the sentence of a
rule, so the cockpit and a Telegram message say the same thing.
"""
from __future__ import annotations

import re

from .durations import human, seconds

UNITS = ("", "%", "bytes", "s", "/s", "events", "days")

# label values that carry an address (Prometheus `instance`, blackbox `target`, …)
_ADDR = re.compile(r"^\[?[0-9a-fA-F.:]+\]?(:\d+)?$")
_IPV4 = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(:\d+)?$")

# raw PromQL we know → (what it measures, unit). First match wins.
_KNOWN = [
    (re.compile(r"node_memory_MemAvailable_bytes\s*/\s*node_memory_MemTotal_bytes"), "memory used", "%"),
    (re.compile(r"node_cpu_seconds_total\{[^}]*mode=\"idle\""), "CPU used", "%"),
    (re.compile(r"node_filesystem_(avail|free)_bytes.*node_filesystem_size_bytes", re.S), "disk used", "%"),
    (re.compile(r"probe_ssl_earliest_cert_expiry"), "days before the certificate expires", "days"),
    (re.compile(r"node_load(1|5|15)\b"), "load average", ""),
    (re.compile(r"^\s*up\s*(\{|$|[=!<>])"), "target up", ""),
]
_METRIC = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{|\[|\)|$|\s)")
_NOT_METRIC = {"sum", "avg", "max", "min", "count", "rate", "irate", "increase", "by", "without",
               "on", "ignoring", "group_left", "group_right", "time", "vector", "scalar", "abs",
               "histogram_quantile", "quantile", "topk", "bottomk", "and", "or", "unless", "bool",
               "offset", "label_replace", "clamp_min", "clamp_max", "round", "delta", "deriv",
               "avg_over_time", "max_over_time", "min_over_time", "sum_over_time", "absent"}


# ------------------------------------------------------------------------------ hosts
def _resolve(addr: str) -> str | None:
    try:
        import hostnames  # backend/hostnames.py (Infra uses it too)
    except ImportError:   # pragma: no cover — the package is always run from backend/
        return None
    try:
        name = hostnames.resolve(addr)
    except Exception:  # noqa: BLE001 — a name is a nicety, never a failure
        return None
    host = hostnames.split_host(addr)[0]
    return name if name and name != host else None


def display_value(key: str, value: str) -> str:
    """`100.76.30.90:9100` → `rpi1` (the address stays in `group_display`'s tooltip)."""
    v = str(value)
    if v and _ADDR.match(v) and (_IPV4.match(v) or ":" in v):
        name = _resolve(v)
        if name:
            return name
    return v


def group_display(group: dict | None) -> dict:
    """{label: what to show} — only the labels whose value reads better (an address → a name)."""
    out = {}
    for k, v in (group or {}).items():
        d = display_value(k, v)
        if d != str(v):
            out[k] = d
    return out


def group_title(group: dict | None) -> str:
    """One short line for a group: hosts first, then the other values (« rpi1 · node »).
    Empty group → ""."""
    if not group:
        return ""
    disp = group_display(group)
    hosts = [disp[k] for k in group if k in disp]
    rest = [str(v) for k, v in group.items() if k not in disp and str(v)]
    return " · ".join(hosts + rest)


def group_where(group: dict | None) -> str:
    """For a message: « rpi1 (100.76.30.90:9100), job=node »."""
    disp = group_display(group)
    parts = []
    for k, v in (group or {}).items():
        parts.append(f"{disp[k]} ({v})" if k in disp else f"{k}={v}")
    return ", ".join(parts)


# ------------------------------------------------------------------------------ queries in words
def describe_raw(promql: str) -> tuple[str, str]:
    """(what a raw PromQL measures, unit) — never the query itself. Unknown → the main metric
    name in words (« node_network_receive_bytes_total »  → « node network receive bytes »);
    nothing recognisable → ("", "")."""
    q = promql or ""
    for rx, label, unit in _KNOWN:
        if rx.search(q):
            return label, unit
    for m in _METRIC.finditer(q):
        name = m.group(1)
        if name in _NOT_METRIC or name.isdigit() or "_" not in name:
            continue
        unit = _unit_of_metric(name, rate="rate(" in q or "irate(" in q or "increase(" in q)
        if "* 100" in q.replace("*100", "* 100") and "/" in q:
            unit = "%"
        words = re.sub(r"_(total|count|sum|bucket)$", "", name).replace("_", " ").strip()
        return words, unit
    return "", ""


def _unit_of_metric(name: str, rate: bool = False) -> str:
    if name.endswith("_bytes") or name.endswith("_bytes_total"):
        return "/s" if rate and name.endswith("_total") else "bytes"
    if name.endswith("_seconds") or name.endswith("_seconds_total"):
        return "s"
    if rate and name.endswith("_total"):
        return "/s"
    if name.endswith("_ratio") or name.endswith("_percent"):
        return "%"
    return ""


def infer_unit(rule: dict, source_kind: str) -> str:
    """The unit of the value a rule watches (« % », « bytes », « s », « /s », « events »…)."""
    u = rule.get("unit")
    if u in UNITS and u:
        return u
    t = rule.get("type")
    if source_kind != "prometheus":
        return "events"
    if t == "absence":
        return ""
    q = rule.get("query") or {}
    label = str(q.get("label") or "")
    if label.endswith("(%)") or label.endswith("%"):
        return "%"
    if q.get("mode") == "raw":
        return describe_raw(q.get("raw") or "")[1]
    b = q.get("builder") or {}
    if b.get("ratio_of"):
        return "%"
    return _unit_of_metric(str(b.get("metric") or ""), rate=b.get("agg") in ("rate",))


# ------------------------------------------------------------------------------ values
def _num(x: float) -> str:
    if x != x:  # NaN
        return "—"
    if abs(x) >= 100:
        return f"{x:.0f}"
    if abs(x) >= 1:
        return f"{x:.1f}".rstrip("0").rstrip(".")
    if x == 0:
        return "0"
    return f"{x:.2g}"


def fmt(value, unit: str = "") -> str:
    """77.83 % · 1.2 GB · 350 ms · 4.2/s · 12 events."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit == "%":
        return f"{_num(v)} %"
    if unit == "bytes":
        for step, suffix in ((1 << 40, "TB"), (1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
            if abs(v) >= step:
                return f"{_num(v / step)} {suffix}"
        return f"{_num(v)} B"
    if unit == "s":
        if abs(v) < 1:
            return f"{_num(v * 1000)} ms"
        if abs(v) >= 120:
            return human(v)
        return f"{_num(v)} s"
    if unit == "/s":
        return f"{_num(v)}/s"
    if unit == "events":
        n = int(round(v))
        return f"{n} event" + ("" if n == 1 else "s")
    if unit == "days":
        return f"{_num(v)} day" + ("" if abs(v) == 1 else "s")
    return _num(v)


_OPS = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "="}


def value_text(value, threshold: dict | None, unit: str = "", rule_type: str = "",
               window: str = "") -> str:
    """The one line under an alert: « 77.8 % > 50 % », « down », « 12 events in 5 min (≥ 10) »."""
    th = threshold or {}
    t = rule_type or th.get("type") or ""
    u = unit or th.get("unit") or ""
    if t == "absence":
        return "no data — unreachable"
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    # « up < 1 » : a Prometheus target down
    if th.get("op") in ("<", "<=") and th.get("value") in (1, 1.0) and u in ("", None) and v in (0, 0.0):
        return "down"
    if t in ("frequency", "flatline") or (u == "events" and th.get("op")):
        win = window or th.get("window") or ""
        span = f" in {human(seconds(win))}" if win and seconds(win) > 0 else ""
        limit = f"{_OPS.get(th.get('op', ''), '')} {_num(float(th.get('value', 0)))}"
        if t in ("frequency", "flatline"):
            return f"{fmt(v, 'events')}{span} ({limit})"
        return f"{fmt(v, 'events')}{span} {limit}"
    if th.get("op") and isinstance(th.get("value"), (int, float)):
        return f"{fmt(v, u)} {_OPS.get(th['op'], th['op'])} {fmt(th['value'], u)}"
    return fmt(v, u)
