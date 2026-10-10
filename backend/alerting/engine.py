"""engine.py — rule types, evaluated the same way for the backtest the UI draws and for the
live loop (scheduler): one evaluation = for each group, a value and a condition at each step.

The live loop reads the LAST step of each group (and remembers terms / last values for
new_term and change); the backtest folds the conditions with the rule's `for` into the
intervals where the rule would have fired. Same code both ways, so « it would have rung 3
times yesterday » is what will happen.
"""
from __future__ import annotations

import json
import math
import statistics

from . import humanize, sources
from .durations import BadDuration, human, seconds, text as dtext

TYPES = ("threshold", "any", "frequency", "spike", "flatline", "change", "new_term",
         "cardinality", "absence", "anomaly")
METRIC_TYPES = ("threshold", "spike", "absence", "anomaly")       # on a metric source
EVENT_TYPES = ("threshold", "any", "frequency", "spike", "flatline", "change", "new_term",
               "cardinality")                                     # on an event source
SEVERITIES = ("info", "warning", "critical")
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
       "<=": lambda a, b: a <= b, "==": lambda a, b: a == b}
MAX_GROUPS = 20
OP_WORDS = {">": "above", ">=": "at or above", "<": "below", "<=": "at or below", "==": "equal to"}


class RuleError(ValueError):
    pass


# =============================================================================================
# validation / normalisation of a RuleIn
# =============================================================================================
def _dur(v, default, what) -> str:
    try:
        return dtext(seconds(v, default))
    except BadDuration as e:
        raise RuleError(f"{what}: {e}") from None


def normalize(body: dict, source: dict | None) -> dict:
    """Validated, defaulted copy of a RuleIn (raises RuleError with a readable message)."""
    b = dict(body or {})
    name = (b.get("name") or "").strip()
    if not name:
        raise RuleError("give the rule a name")
    if source is None:
        raise RuleError("choose a source")
    typ = b.get("type") or "threshold"
    if typ not in TYPES:
        raise RuleError(f"unknown rule type {typ!r}")
    is_metric = source["kind"] == "prometheus"
    if is_metric and typ not in METRIC_TYPES:
        raise RuleError(f"« {typ} » works on events (logs); with metrics use threshold, spike, "
                        "absence or anomaly")
    if not is_metric and typ not in EVENT_TYPES:
        raise RuleError(f"« {typ} » works on metrics; with events use threshold, any, frequency, "
                        "spike, flatline, change, new_term or cardinality")
    sev = b.get("severity") or "warning"
    if sev not in SEVERITIES:
        raise RuleError("severity must be info, warning or critical")
    p = dict(b.get("params") or {})
    if typ == "threshold":
        if p.get("op", ">") not in OPS:
            raise RuleError("threshold operator must be one of > >= < <= ==")
        try:
            p = {"op": p.get("op", ">"), "value": float(p.get("value", 0)),
                 "reduce": p.get("reduce") or "last",
                 "window": _dur(p.get("window"), 0 if is_metric else 300, "window")}
        except (TypeError, ValueError):
            raise RuleError("the threshold must be a number") from None
        if p["reduce"] not in ("last", "avg", "max", "min", "sum"):
            raise RuleError("reduce must be last, avg, max, min or sum")
    elif typ in ("frequency", "flatline"):
        try:
            p = {"count": int(p.get("count", 1 if typ == "flatline" else 10)),
                 "window": _dur(p.get("window"), 600, "window")}
        except (TypeError, ValueError):
            raise RuleError("count must be a whole number") from None
        if p["count"] < 1:
            raise RuleError("count must be at least 1")
    elif typ == "spike":
        try:
            p = {"ratio": float(p.get("ratio", 3)), "direction": p.get("direction") or "up",
                 "window": _dur(p.get("window"), 600, "window"),
                 "reference": _dur(p.get("reference"), 3600, "reference"),
                 "min_count": float(p.get("min_count", 0))}
        except (TypeError, ValueError):
            raise RuleError("ratio and min_count must be numbers") from None
        if p["ratio"] <= 1:
            raise RuleError("the spike ratio must be above 1 (e.g. 3 = three times)")
        if p["direction"] not in ("up", "down", "both"):
            raise RuleError("direction must be up, down or both")
    elif typ == "change":
        if not (p.get("field") or "").strip():
            raise RuleError("which field should be watched for a change?")
        p = {"field": p["field"].strip(), "key_field": (p.get("key_field") or "").strip()}
    elif typ == "new_term":
        if not (p.get("field") or "").strip():
            raise RuleError("which field should bring a new value?")
        p = {"field": p["field"].strip(), "lookback": _dur(p.get("lookback"), 7 * 86400, "lookback")}
    elif typ == "cardinality":
        if not (p.get("field") or "").strip():
            raise RuleError("which field should be counted?")
        if p.get("op", ">") not in (">", "<"):
            raise RuleError("cardinality compares with > or <")
        try:
            p = {"field": p["field"].strip(), "op": p.get("op", ">"), "value": int(p.get("value", 10)),
                 "window": _dur(p.get("window"), 3600, "window")}
        except (TypeError, ValueError):
            raise RuleError("the limit must be a whole number") from None
    elif typ == "absence":
        p = {"window": _dur(p.get("window"), 300, "window")}
    elif typ == "anomaly":
        try:
            p = {"z": float(p.get("z", 3)), "lookback": _dur(p.get("lookback"), 86400, "lookback")}
        except (TypeError, ValueError):
            raise RuleError("z must be a number") from None
    else:
        p = {}
    qh = dict(b.get("quiet_hours") or {})
    out = {
        "name": name[:120], "description": (b.get("description") or "")[:2000], "severity": sev,
        "enabled": bool(b.get("enabled", True)), "source_id": int(source["id"]),
        "query": _norm_query(b.get("query") or {}), "type": typ, "params": p,
        "every": _dur(b.get("every"), 60, "every"), "for": _dur(b.get("for"), 0, "for"),
        "group_by": [str(x).strip() for x in (b.get("group_by") or []) if str(x).strip()][:5],
        "realert": _dur(b.get("realert"), 1800, "realert"),
        "quiet_hours": {"enabled": bool(qh.get("enabled")), "start": qh.get("start") or "22:00",
                        "end": qh.get("end") or "07:00", "tz": qh.get("tz") or "Europe/Zurich",
                        "days": [int(d) for d in (qh.get("days") if qh.get("days") is not None
                                                  else range(7)) if 0 <= int(d) <= 6]},
        "channels": [int(c) for c in (b.get("channels") or [])],
        # 3.5.1: a rule that tells nobody is a choice, ticked on purpose (« no notification »)
        "no_notification": bool(b.get("no_notification")),
        "actions": {"incident": bool((b.get("actions") or {}).get("incident")),
                    "diag_session": bool((b.get("actions") or {}).get("diag_session")),
                    "agent_id": (b.get("actions") or {}).get("agent_id") or None,
                    "runbook": (b.get("actions") or {}).get("runbook") or None},
        "labels": {str(k)[:40]: str(v)[:120] for k, v in (b.get("labels") or {}).items()},
        "runbook_url": (b.get("runbook_url") or "")[:500],
        "level": b.get("level") if b.get("level") in (None, 0, 1, 2, 3, 4) else None,
    }
    if seconds(out["every"]) < 15:
        raise RuleError("evaluate at most every 15 s")
    try:
        out["query_compiled"] = sources.compile_query(source["kind"], out["query"])
    except ValueError as e:
        raise RuleError(str(e)) from None
    # what the value is measured in (« % », « bytes »…): the person's choice if given, else read
    # from the template label / builder / query — the cockpit and the messages print values with it
    u = b.get("unit")
    out["unit"] = u if (u in humanize.UNITS and u) else humanize.infer_unit(out, source["kind"])
    out["sentence"] = sentence(out, source)
    return out


def _norm_query(q: dict) -> dict:
    b = dict(q.get("builder") or {})
    out = {"mode": "raw" if q.get("mode") == "raw" else "builder", "raw": (q.get("raw") or "")[:5000],
           "builder": b}
    label = str(q.get("label") or "").strip()[:120]
    if label:
        out["label"] = label
    return out


# =============================================================================================
# the sentence — « Alert when … » (what the rule means, in words)
# =============================================================================================
def _what(r: dict, src: dict) -> str:
    q = r["query"]
    if q["mode"] == "raw":
        label = q.get("label") or (humanize.describe_raw(q.get("raw") or "")[0]
                                   if src["kind"] == "prometheus" else "")
        if label and r.get("unit") == "%":
            label = label.removesuffix("(%)").strip()     # « above 85 % » says it already
        return label or f"the query ({src['name']})"
    b = q["builder"]
    if src["kind"] == "prometheus":
        agg = {"rate": "the rate of", "increase": "the increase of", "avg": "the average of",
               "max": "the maximum of", "min": "the minimum of", "sum": "the sum of",
               "count": "the number of series of", "p95": "the p95 of", "last": ""}.get(b.get("agg") or "last", "")
        s = f"{agg} {b.get('metric', '')}".strip()
        if b.get("ratio_of"):
            s = f"{s} as a share of {b['ratio_of'].get('metric', '')} (%)"
    else:
        s = "matching events"
        if b.get("text"):
            s = f"events containing « {b['text']} »"
    return s


def _filters(r: dict, src: dict) -> str:
    """The label filters of the rule in words (« except host raspberrypi, on gmk1 ») — 3.5.1: the
    sentence said « a target is down » for `up{host!="raspberrypi"}` and printed builder filters as
    « (instance=100.76.30.90:9100) »."""
    q = r.get("query") or {}
    if q.get("mode") == "raw":
        return humanize.filters_phrase(humanize.selector_filters(q.get("raw") or "")) \
            if src.get("kind") == "prometheus" else ""
    return humanize.filters_phrase((q.get("builder") or {}).get("filters") or [])


def sentence(r: dict, src: dict) -> str:
    p, t, w = r["params"], r["type"], _what(r, src)
    if t == "threshold" and _is_target_down(r, p):
        s = "Alert when a target is down"
    elif t == "threshold":
        if src["kind"] == "prometheus":
            red = "" if p["reduce"] == "last" else f"{p['reduce']} over {human(seconds(p['window']))} of "
            s = f"Alert when {red}{w} is {OP_WORDS[p['op']]} {_val(p['value'], r.get('unit', ''))}"
        else:
            s = (f"Alert when the number of {w} in {human(seconds(p['window']))} is "
                 f"{OP_WORDS[p['op']]} {_n(p['value'])}")
    elif t == "any":
        s = f"Alert on every one of the {w}"
    elif t == "frequency":
        s = f"Alert when there are {p['count']} or more {w} within {human(seconds(p['window']))}"
    elif t == "flatline":
        s = f"Alert when there are fewer than {p['count']} {w} within {human(seconds(p['window']))}"
    elif t == "spike":
        d = {"up": "rises to", "down": "drops to 1/", "both": "moves by"}[p["direction"]]
        k = _n(p["ratio"])
        s = (f"Alert when {w} over {human(seconds(p['window']))} {d}{'' if p['direction'] == 'down' else ' '}"
             f"{k}{'×' if p['direction'] != 'down' else ''} the level of the previous "
             f"{human(seconds(p['reference']))}")
    elif t == "change":
        key = f" for the same {p['key_field']}" if p["key_field"] else ""
        s = f"Alert when the value of {p['field']} changes{key} in {w}"
    elif t == "new_term":
        s = (f"Alert when {p['field']} takes a value never seen in the last "
             f"{human(seconds(p['lookback']))} in {w}")
    elif t == "cardinality":
        s = (f"Alert when the number of distinct {p['field']} in {human(seconds(p['window']))} is "
             f"{OP_WORDS[p['op']]} {p['value']} in {w}")
    elif t == "absence":
        s = f"Alert when {w} sends no data for {human(seconds(p['window']))}"
    elif t == "anomaly":
        s = (f"Alert when {w} moves more than {_n(p['z'])} standard deviations away from its "
             f"usual level (last {human(seconds(p['lookback']))})")
    else:
        s = "Alert"
    flt = _filters(r, src)
    if flt:
        s += f", {flt}"
    if r.get("group_by"):
        s += f", by {', '.join(r['group_by'])}"
    if seconds(r["for"]) > 0:
        s += f", for {human(seconds(r['for']))}"
    return s


def _is_target_down(r: dict, p: dict) -> bool:
    """`up < 1` (or `up == 0`) on Prometheus: « a target is down », not « up is below 1 »."""
    q = r.get("query") or {}
    metric = (q.get("builder") or {}).get("metric") if q.get("mode") == "builder" else None
    is_up = metric == "up" or (q.get("mode") == "raw" and
                                humanize.describe_raw(q.get("raw") or "")[0] == "target up")
    return bool(is_up and ((p.get("op") in ("<", "<=") and float(p.get("value", 0)) == 1)
                           or (p.get("op") == "==" and float(p.get("value", 1)) == 0)))


def _val(v: float, unit: str) -> str:
    """A threshold in a sentence: « 85 % », « 2 GB », « 300 ms » — a bare number without unit."""
    return humanize.fmt(v, unit) if unit and unit != "events" else _n(v)


def _n(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


# =============================================================================================
# evaluation
# =============================================================================================
def gkey(group: dict, by: list[str]) -> str:
    keys = by or sorted(group)
    return ",".join(f"{k}={group.get(k, '')}" for k in keys)


def lookback(r: dict) -> float:
    """How much history one live evaluation needs."""
    p, t = r["params"], r["type"]
    w = seconds(p.get("window") or 0)
    if t == "spike":
        w = seconds(p["window"]) + seconds(p["reference"])
    elif t == "anomaly":
        w = seconds(p["lookback"])
    return max(w, seconds(r["every"]) * 5, seconds(r["for"]) + seconds(r["every"]), 300)


def evaluate(r: dict, src: dict, ad: "sources.Adapter", start: float, end: float, step: float,
             known_terms: set | None = None, last_values: dict | None = None) -> dict:
    """→ {"kind", "series": [{group, group_key, points, cond}], "threshold", "reference",
    "sample_events", "warnings", "new_terms": {...}, "changes": {...}}.
    `cond` = list of booleans aligned with points. Source errors propagate (SourceError)."""
    t, p, q = r["type"], r["params"], r["query_compiled"]
    by = r.get("group_by") or []
    out = {"kind": "metric", "series": [], "threshold": None, "reference": None,
           "sample_events": [], "warnings": []}
    step = max(float(step), 1.0)
    if src["kind"] == "prometheus":
        pad = seconds(p.get("reference") or 0) + seconds(p.get("lookback") or 0) if t in ("spike", "anomaly") else 0
        raw = ad.series(q, start - pad, end, step)
        if not by and len(raw) > MAX_GROUPS:
            out["warnings"].append(f"The query returns {len(raw)} series: only the {MAX_GROUPS} "
                                   "largest are shown — group them with « by » or add a filter")
        raw = _largest(raw, MAX_GROUPS)
        if t == "threshold":
            out["threshold"] = {"op": p["op"], "value": p["value"]}
            win = seconds(p["window"])
            for s in raw:
                vals = _rolling(s["points"], win, p["reduce"]) if win > 0 else s["points"]
                vals = [x for x in vals if x[0] >= start]
                out["series"].append(_grp(s["group"], by, vals,
                                          [OPS[p["op"]](v, p["value"]) for _, v in vals]))
        elif t == "spike":
            win, ref = seconds(p["window"]), seconds(p["reference"])
            for s in raw:
                cur = _rolling(s["points"], win, "avg")
                base = _shifted_avg(s["points"], win, ref)
                vals, cond, refpts = [], [], []
                for (ts, v), b in zip(cur, base):
                    if ts < start:
                        continue
                    vals.append([ts, v])
                    refpts.append([ts, b])
                    cond.append(_spike(v, b, p))
                out["series"].append(_grp(s["group"], by, vals, cond))
                if out["reference"] is None:
                    out["reference"] = {"group_key": gkey(s["group"], by), "points": refpts}
        elif t == "absence":
            win = seconds(p["window"])
            n = int(math.ceil((end - start) / step))
            grid = [start + (i + 1) * step for i in range(n)]
            alls = sorted(ts for s in raw for ts, _ in s["points"])
            pres = [[ts, float(_any_in(alls, ts - win, ts))] for ts in grid]
            out["series"].append({"group": {}, "group_key": "", "points": pres,
                                  "cond": [v == 0 for _, v in pres]})
            out["kind"] = "count"
            for s in raw:     # a group that WAS there and went silent
                last = s["points"][-1][0] if s["points"] else None
                if last is not None and end - last > win and s["group"]:
                    pts = [[ts, float(_any_in([x for x, _ in s["points"]], ts - win, ts))] for ts in grid]
                    out["series"].append(_grp(s["group"], by, pts, [v == 0 for _, v in pts]))
        elif t == "anomaly":
            lb = seconds(p["lookback"])
            for s in raw:
                vals, cond, lo, hi = [], [], [], []
                pts = s["points"]
                for i, (ts, v) in enumerate(pts):
                    if ts < start:
                        continue
                    hist = [x for tt, x in pts[:i] if tt >= ts - lb]
                    if len(hist) < 10:
                        vals.append([ts, v])
                        cond.append(False)
                        continue
                    mu = statistics.fmean(hist)
                    sd = statistics.pstdev(hist) or 1e-9
                    vals.append([ts, v])
                    lo.append([ts, mu - p["z"] * sd])
                    hi.append([ts, mu + p["z"] * sd])
                    cond.append(abs(v - mu) / sd >= p["z"])
                out["series"].append(_grp(s["group"], by, vals, cond))
                if out["reference"] is None:
                    out["reference"] = {"lower": lo, "upper": hi}
        return out

    # ------------------------------------------------------------------ event sources
    out["kind"] = "count"
    win = seconds(p.get("window") or 0) or step
    if t in ("threshold", "frequency", "flatline", "spike"):
        pad = seconds(p.get("reference") or 0) if t == "spike" else 0
        bstep = min(step, win)
        raw = ad.counts(q, start - win - pad, end, bstep, by or None)
        raw = _largest(raw, MAX_GROUPS)
        if not raw:
            raw = [{"group": {}, "points": [[start + (i + 1) * bstep, 0.0]
                                            for i in range(int(math.ceil((end - start) / bstep)))]}]
        for s in raw:
            summed = _rolling(s["points"], win, "sum")
            vals = [x for x in summed if x[0] >= start]
            if t == "threshold":
                out["threshold"] = {"op": p["op"], "value": p["value"]}
                cond = [OPS[p["op"]](v, p["value"]) for _, v in vals]
            elif t == "frequency":
                out["threshold"] = {"op": ">=", "value": p["count"]}
                cond = [v >= p["count"] for _, v in vals]
            elif t == "flatline":
                out["threshold"] = {"op": "<", "value": p["count"]}
                cond = [v < p["count"] for _, v in vals]
            else:
                base = _shifted_sum(s["points"], win, seconds(p["reference"]))
                base = [x for x in base if x[0] >= start]
                cond = [_spike(v, b, p) for (_, v), (_, b) in zip(vals, base)]
                if out["reference"] is None:
                    out["reference"] = {"group_key": gkey(s["group"], by), "points": base}
            out["series"].append(_grp(s["group"], by, vals, cond))
        try:
            out["sample_events"] = [_ev(e) for e in ad.events(q, max(start, end - 86400), end, 5)]
        except sources.SourceError:
            pass
        return out

    # any / change / new_term / cardinality: they need the events themselves
    out["kind"] = "events"
    extra = seconds(p["lookback"]) if t == "new_term" and known_terms is None else 0
    evts = ad.events(q, start - extra - (win if t == "cardinality" else 0), end, limit=10000)
    if len(evts) >= 10000:
        out["warnings"].append("More than 10 000 events in the range: the oldest are not counted")
    evts.sort(key=lambda e: e["ts"])
    n = int(math.ceil((end - start) / step)) or 1
    grid = [start + (i + 1) * step for i in range(n)]
    groups: dict[str, dict] = {}

    def slot(e) -> dict:
        g = {b: str(e["fields"].get(b, "")) for b in by}
        k = gkey(g, by)
        if k not in groups:
            groups[k] = {"group": g, "hits": [0] * n, "vals": [0.0] * n, "samples": []}
        return groups[k]

    def idx(ts) -> int:
        return min(max(int(math.ceil((ts - start) / step)) - 1, 0), n - 1)

    if t == "any":
        for e in evts:
            if e["ts"] < start:
                continue
            s = slot(e)
            s["hits"][idx(e["ts"])] += 1
            s["samples"].append(e)
        for s in groups.values():
            s["vals"] = [float(h) for h in s["hits"]]
    elif t == "new_term":
        f = p["field"]
        seen = set(known_terms) if known_terms is not None else set()
        new_terms: dict[str, float] = {}
        for e in evts:
            v = e["fields"].get(f)
            if v is None or v == "":
                continue
            v = str(v)
            if e["ts"] < start:
                seen.add(v)
                continue
            if v not in seen:
                seen.add(v)
                new_terms[v] = e["ts"]
                s = slot(e)
                s["hits"][idx(e["ts"])] += 1
                s["samples"].append({**e, "text": f"new {f}: {v} — {e['text']}"})
        for s in groups.values():
            s["vals"] = [float(h) for h in s["hits"]]
        out["new_terms"] = new_terms
    elif t == "change":
        f, kf = p["field"], p["key_field"]
        last = dict(last_values or {})
        changes: dict[str, str] = {}
        for e in evts:
            v = e["fields"].get(f)
            if v is None:
                continue
            key = str(e["fields"].get(kf, "")) if kf else "*"
            prev = last.get(key)
            last[key] = str(v)
            changes[key] = str(v)
            if e["ts"] >= start and prev is not None and prev != str(v):
                s = slot(e)
                s["hits"][idx(e["ts"])] += 1
                s["samples"].append({**e, "text": f"{f}: {prev} → {v}" + (f" ({kf}={key})" if kf else "")})
        for s in groups.values():
            s["vals"] = [float(h) for h in s["hits"]]
        out["changes"] = changes
    elif t == "cardinality":
        f = p["field"]
        out["threshold"] = {"op": p["op"], "value": p["value"]}
        per: dict[str, list] = {}
        for e in evts:
            g = {b: str(e["fields"].get(b, "")) for b in by}
            per.setdefault(gkey(g, by), [g, []])[1].append(e)
        if not per:
            per[gkey({}, by)] = [{b: "" for b in by}, []]
        for k, (g, es) in per.items():
            vals = []
            for ts in grid:
                vals.append(float(len({str(e["fields"].get(f)) for e in es
                                       if ts - win < e["ts"] <= ts and e["fields"].get(f) is not None})))
            groups[k] = {"group": g, "vals": vals, "hits": [0] * n,
                         "samples": [e for e in es if e["ts"] >= start][-5:]}
    for k, s in groups.items():
        pts = [[grid[i], s["vals"][i]] for i in range(n)]
        if t == "cardinality":
            cond = [OPS[p["op"]](v, p["value"]) for _, v in pts]
        else:
            cond = [h > 0 for h in s["hits"]]
        out["series"].append({"group": s["group"], "group_key": k, "points": pts, "cond": cond})
        out["sample_events"] += [_ev(e) for e in s["samples"][-5:]]
    if not out["series"]:
        out["series"].append({"group": {}, "group_key": "", "points": [[ts, 0.0] for ts in grid],
                              "cond": [False] * n})
    out["series"] = out["series"][:MAX_GROUPS]
    out["sample_events"] = sorted(out["sample_events"], key=lambda e: -e["ts"])[:5]
    return out


def _ev(e: dict) -> dict:
    return {"ts": e["ts"], "text": str(e.get("text", ""))[:300],
            "fields": {k: v for k, v in list((e.get("fields") or {}).items())[:20]}}


def _grp(group: dict, by: list[str], points: list, cond: list) -> dict:
    g = {k: group.get(k, "") for k in by} if by else group
    return {"group": g, "group_key": gkey(group, by), "points": points, "cond": cond}


def _largest(raw: list[dict], n: int) -> list[dict]:
    if len(raw) <= n:
        return raw
    return sorted(raw, key=lambda s: -max((abs(v) for _, v in s["points"]), default=0))[:n]


def _rolling(points: list, win: float, how: str) -> list:
    """Value at each point = reduce over (t - win, t]."""
    if win <= 0 or how == "last":
        return [list(x) for x in points]
    out, j, acc = [], 0, []
    for i, (ts, _v) in enumerate(points):
        while j < len(points) and points[j][0] <= ts:
            j += 1
        acc = [v for tt, v in points[:j] if tt > ts - win]
        if not acc:
            out.append([ts, 0.0])
            continue
        r = {"avg": statistics.fmean, "max": max, "min": min, "sum": math.fsum}[how](acc)
        out.append([ts, float(r)])
    return out


def _shifted_sum(points: list, win: float, ref: float) -> list:
    """Reference for spike on counts: events in (t - win - ref, t - win], scaled to `win`."""
    out = []
    for ts, _ in points:
        s = math.fsum(v for tt, v in points if ts - win - ref < tt <= ts - win)
        out.append([ts, s * win / ref if ref else 0.0])
    return out


def _shifted_avg(points: list, win: float, ref: float) -> list:
    out = []
    for ts, _ in points:
        xs = [v for tt, v in points if ts - win - ref < tt <= ts - win]
        out.append(statistics.fmean(xs) if xs else 0.0)
    return out


def _spike(cur: float, ref: float, p: dict) -> bool:
    k, mn = p["ratio"], p.get("min_count", 0)
    up = cur >= mn and (cur >= k * ref if ref > 0 else cur > 0 and mn > 0)
    down = ref >= mn and ref > 0 and cur <= ref / k
    return {"up": up, "down": down, "both": up or down}[p["direction"]]


def _any_in(sorted_ts: list, a: float, b: float) -> bool:
    import bisect
    i = bisect.bisect_right(sorted_ts, a)
    return i < len(sorted_ts) and sorted_ts[i] <= b


# =============================================================================================
# backtest — the conditions folded with `for` into firing intervals
# =============================================================================================
def fired_intervals(ev: dict, for_s: float) -> list[dict]:
    out = []
    for s in ev["series"]:
        pts, cond = s["points"], s.get("cond") or []
        run_start, peak, firing_at = None, None, None
        for (ts, v), c in zip(pts, cond):
            if c:
                if run_start is None:
                    run_start, peak = ts, v
                peak = max(peak, v) if peak is not None else v
                if firing_at is None and ts - run_start >= for_s:
                    firing_at = ts
            else:
                if firing_at is not None:
                    out.append({"group_key": s["group_key"], "start": firing_at, "end": ts, "peak": peak})
                run_start, peak, firing_at = None, None, None
        if firing_at is not None:
            out.append({"group_key": s["group_key"], "start": firing_at, "end": None, "peak": peak})
    return sorted(out, key=lambda x: x["start"])


def preview(r: dict, src: dict, ad, rng: str, now: float) -> dict:
    span = seconds(rng or "24h")
    if span > 30 * 86400:
        raise RuleError("the backtest covers at most 30 days")
    step = max(15.0, math.ceil(span / 240 / 15) * 15)   # ~240 points, multiple of 15 s
    if r["type"] not in ("any", "new_term", "change") and src["kind"] != "prometheus":
        step = max(step, min(seconds(r["params"].get("window") or step), span / 60))
    base = {"kind": "metric", "step_s": step, "query_compiled": r["query_compiled"],
            "sentence": r["sentence"], "unit": r.get("unit", ""), "series": [], "threshold": None, "reference": None,
            "fired_intervals": [], "fires": 0, "sample_events": [], "error": None, "warnings": []}
    try:
        ev = evaluate(r, src, ad, now - span, now, step)
    except sources.SourceError as e:
        return {**base, "error": str(e)}
    fi = fired_intervals(ev, seconds(r["for"]))
    return {**base, "kind": ev["kind"], "threshold": ev["threshold"], "reference": ev["reference"],
            "unit": r.get("unit", ""),
            "series": [{"group": s["group"], "group_key": s["group_key"],
                        "group_title": humanize.group_title(s["group"]),
                        "group_display": humanize.group_display(s["group"]),
                        "points": [[round(t, 3), round(v, 6)] for t, v in s["points"]]}
                       for s in ev["series"]],
            "fired_intervals": fi, "fires": len(fi), "sample_events": ev["sample_events"],
            "warnings": ev["warnings"]}


def dumps(v) -> str:
    return json.dumps(v, separators=(",", ":"))
