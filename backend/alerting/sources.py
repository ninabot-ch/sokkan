"""sources.py — where the data comes from: Prometheus, Loki, Elasticsearch / OpenSearch, and
SOKKAN's own events. One adapter per kind with the same small interface, plus the query
builder that turns the UI's form into PromQL / LogQL / Lucene (so nobody has to write one).

Adapter interface (all times epoch seconds):
  test() -> {"ok", "latency_ms", "detail", "error"}
  series(q, start, end, step) -> [{"group": {..}, "points": [[t, v], ...]}]    metrics
  counts(q, start, end, step, by) -> same shape, number of matching events per step
  events(q, start, end, limit) -> [{"ts", "text", "fields": {..}}]  newest first
  suggest(kind, metric, label, field, q) -> [str]
A source that fails raises SourceError with a readable message; the engine turns it into the
rule's `error` state, never a crash.
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import time

import httpx

from . import store
from .durations import seconds, text as dtext

KINDS = ("prometheus", "loki", "elasticsearch", "opensearch", "sokkan", "external")
EVENT_KINDS = ("loki", "elasticsearch", "opensearch", "sokkan", "external")
TIMEOUT = float(os.environ.get("SOKKAN_ALERTING_SOURCE_TIMEOUT_S") or 10)
MAX_POINTS = 11000          # Prometheus refuses more than 11 000 points per series


class SourceError(Exception):
    pass


# =============================================================================================
# sources as rows
# =============================================================================================
def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "kind": r["kind"], "url": r["url"],
            "scope": "instance" if r["project"] == "*" else "project", "project": r["project"],
            "builtin": bool(r["builtin"]), "auth": _auth_public(r),
            "options": store.j(r["options"], {}), "status": store.j(r["status"], {})}


def _auth_public(r) -> dict:
    k = r["auth_kind"] or "none"
    out = {"kind": k}
    if k == "basic":
        out["user"] = r["auth_user"]
    if k != "none":
        out["secret_set"] = bool(r["secret_ct"])
    return out


def ensure_builtins() -> None:
    """Sources from the instance env (SOKKAN_PROM, SOKKAN_LOKI) and the always-there ones."""
    want = [("env:prometheus", "Prometheus", "prometheus", (os.environ.get("SOKKAN_PROM") or "").rstrip("/")),
            ("env:loki", "Loki", "loki", (os.environ.get("SOKKAN_LOKI") or "").rstrip("/")),
            ("sokkan", "SOKKAN events", "sokkan", ""),
            ("external", "External alerts (Grafana, webhooks)", "external", "")]
    t = store.now()
    with store._lock:
        c = store.con()
        for key, name, kind, url in want:
            r = c.execute("SELECT id, url FROM sources WHERE builtin=?", (key,)).fetchone()
            if kind in ("prometheus", "loki") and not url:
                if r:   # the env var went away: the builtin source goes too (rules → error)
                    c.execute("DELETE FROM sources WHERE id=?", (r["id"],))
                continue
            if r is None:
                c.execute("INSERT INTO sources(project, name, kind, url, builtin, created_by, "
                          "created_at, updated_at) VALUES('*',?,?,?,?,?,?,?)",
                          (name, kind, url, key, "instance", t, t))
            elif r["url"] != url:
                c.execute("UPDATE sources SET url=?, updated_at=? WHERE id=?", (url, t, r["id"]))
        c.commit()
        c.close()


def list_sources(project: str) -> list[dict]:
    ensure_builtins()
    c = store.con()
    rows = c.execute("SELECT * FROM sources WHERE project IN ('*', ?) ORDER BY builtin = '', id",
                     (project,)).fetchall()
    c.close()
    return [_row(r) for r in rows]


def get(sid: int, project: str | None = None) -> dict | None:
    c = store.con()
    r = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    c.close()
    if r is None or (project is not None and r["project"] not in ("*", project)):
        return None
    return _row(r)


def _raw(sid: int) -> dict | None:
    c = store.con()
    r = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    c.close()
    if r is None:
        return None
    d = dict(r)
    d["options"] = store.j(r["options"], {})
    d["secret"] = store.unseal(r["secret_ct"]).get("secret", "")
    return d


def validate(body: dict) -> dict:
    kind = (body.get("kind") or "").strip()
    if kind not in ("prometheus", "loki", "elasticsearch", "opensearch"):
        raise ValueError("kind must be prometheus, loki, elasticsearch or opensearch")
    url = (body.get("url") or "").strip().rstrip("/")
    if not re.match(r"^https?://", url):
        raise ValueError("url must start with http:// or https://")
    name = (body.get("name") or "").strip() or kind.capitalize()
    auth = body.get("auth") or {}
    ak = (auth.get("kind") or "none").strip()
    if ak not in ("none", "basic", "apikey", "bearer"):
        raise ValueError("auth.kind must be none, basic, apikey or bearer")
    opts = body.get("options") or {}
    if kind in ("elasticsearch", "opensearch"):
        opts = {"index": (opts.get("index") or "*").strip(),
                "time_field": (opts.get("time_field") or "@timestamp").strip(),
                "message_field": (opts.get("message_field") or "message").strip()}
    else:
        opts = {}
    return {"kind": kind, "url": url, "name": name[:80], "auth_kind": ak,
            "auth_user": (auth.get("user") or "").strip() if ak == "basic" else "",
            "secret": auth.get("secret"), "options": opts}


def create(project: str, body: dict, by: str) -> dict:
    v = validate(body)
    t = store.now()
    with store._lock:
        c = store.con()
        cur = c.execute(
            "INSERT INTO sources(project, name, kind, url, auth_kind, auth_user, secret_ct, options,"
            " created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (project, v["name"], v["kind"], v["url"], v["auth_kind"], v["auth_user"],
             store.seal({"secret": v["secret"] or ""}) if v["auth_kind"] != "none" else "",
             json.dumps(v["options"]), by, t, t))
        c.commit()
        sid = cur.lastrowid
        c.close()
    return get(sid)


def update(sid: int, body: dict) -> dict:
    v = validate(body)
    old = _raw(sid)
    secret = v["secret"] if v["secret"] not in (None, "") else (old or {}).get("secret", "")
    with store._lock:
        c = store.con()
        c.execute("UPDATE sources SET name=?, kind=?, url=?, auth_kind=?, auth_user=?, secret_ct=?,"
                  " options=?, updated_at=? WHERE id=?",
                  (v["name"], v["kind"], v["url"], v["auth_kind"], v["auth_user"],
                   store.seal({"secret": secret}) if v["auth_kind"] != "none" else "",
                   json.dumps(v["options"]), store.now(), sid))
        c.commit()
        c.close()
    return get(sid)


def delete(sid: int) -> None:
    with store._lock:
        c = store.con()
        c.execute("DELETE FROM sources WHERE id=? AND builtin=''", (sid,))
        c.commit()
        c.close()


def set_status(sid: int, st: dict) -> None:
    with store._lock:
        c = store.con()
        c.execute("UPDATE sources SET status=? WHERE id=?", (json.dumps(st), sid))
        c.commit()
        c.close()


def adapter(sid: int) -> "Adapter":
    d = _raw(sid)
    if d is None:
        raise SourceError("this source no longer exists")
    return adapter_for(d)


def adapter_for(d: dict) -> "Adapter":
    k = d["kind"]
    if k == "prometheus":
        return Prometheus(d)
    if k == "loki":
        return Loki(d)
    if k in ("elasticsearch", "opensearch"):
        return Elastic(d)
    if k == "sokkan":
        return Sokkan(d)
    if k == "external":
        return External(d)
    raise SourceError(f"unknown source kind {k!r}")


def test(d: dict) -> dict:
    t0 = time.monotonic()
    try:
        detail = adapter_for(d).test()
        return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000), "detail": detail,
                "error": None}
    except SourceError as e:
        return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000), "detail": "",
                "error": str(e)}


# =============================================================================================
# adapters
# =============================================================================================
class Adapter:
    kind = ""
    metric = False      # series() available
    logs = False        # events() / counts() available

    def __init__(self, d: dict):
        self.d = d
        self.url = d.get("url") or ""
        self.opts = d.get("options") or {}

    def _headers(self) -> dict:
        ak, sec = self.d.get("auth_kind") or "none", self.d.get("secret") or ""
        if ak == "apikey" and sec:
            return {"Authorization": f"ApiKey {sec}"}
        if ak == "bearer" and sec:
            return {"Authorization": f"Bearer {sec}"}
        return {}

    def _auth(self):
        if (self.d.get("auth_kind") or "") == "basic":
            return (self.d.get("auth_user") or "", self.d.get("secret") or "")
        return None

    def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = httpx.request(method, self.url + path, headers=self._headers(), auth=self._auth(),
                              timeout=TIMEOUT, **kw)
        except httpx.TimeoutException:
            raise SourceError(f"{self.kind}: no answer after {TIMEOUT:.0f} s ({self.url})") from None
        except httpx.HTTPError as e:
            raise SourceError(f"{self.kind}: cannot reach {self.url} ({e.__class__.__name__})") from None
        if r.status_code in (401, 403):
            raise SourceError(f"{self.kind}: access refused (HTTP {r.status_code}) — check the credentials")
        if r.status_code >= 400:
            msg = r.text[:300]
            try:
                jj = r.json()
                msg = jj.get("error") if isinstance(jj.get("error"), str) else \
                    (jj.get("error") or {}).get("reason") or jj.get("message") or msg
            except ValueError:
                pass
            raise SourceError(f"{self.kind}: {msg} (HTTP {r.status_code})")
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"{self.kind}: the answer is not JSON") from None

    def test(self) -> str:
        raise NotImplementedError

    def series(self, q, start, end, step):
        raise SourceError(f"{self.kind} has no metrics: use an event rule type")

    def counts(self, q, start, end, step, by=None):
        raise SourceError(f"{self.kind} has no events: use a metric rule type")

    def events(self, q, start, end, limit=100):
        raise SourceError(f"{self.kind} has no events: use a metric rule type")

    def suggest(self, kind, metric="", label="", field="", q=""):
        return []


def _clip_step(start: float, end: float, step: float) -> float:
    span = max(end - start, 1)
    return max(step, math.ceil(span / MAX_POINTS))


class Prometheus(Adapter):
    kind, metric = "prometheus", True

    def test(self) -> str:
        bi = self._req("GET", "/api/v1/status/buildinfo").get("data") or {}
        n = len(self._req("GET", "/api/v1/label/__name__/values").get("data") or [])
        return f"Prometheus {bi.get('version', '')} — {n} metrics".replace("  ", " ")

    def series(self, q, start, end, step):
        step = _clip_step(start, end, step)
        d = self._req("GET", "/api/v1/query_range",
                      params={"query": q, "start": start, "end": end, "step": step})
        out = []
        for s in (d.get("data") or {}).get("result") or []:
            pts = []
            for t, v in s.get("values") or []:
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if math.isnan(fv) or math.isinf(fv):
                    continue
                pts.append([float(t), fv])
            lab = {k: v for k, v in (s.get("metric") or {}).items() if k != "__name__"}
            out.append({"group": lab, "points": pts})
        return out

    def suggest(self, kind, metric="", label="", field="", q=""):
        if kind == "metrics":
            items = self._req("GET", "/api/v1/label/__name__/values").get("data") or []
        elif kind == "labels":
            params = {"match[]": metric} if metric else None
            items = [x for x in (self._req("GET", "/api/v1/labels", params=params).get("data") or [])
                     if x != "__name__"]
        elif kind == "values" and label:
            params = {"match[]": metric} if metric else None
            items = self._req("GET", f"/api/v1/label/{label}/values", params=params).get("data") or []
        else:
            items = []
        ql = q.lower()
        return [x for x in items if ql in x.lower()][:200]


class Loki(Adapter):
    kind, logs = "loki", True

    def test(self) -> str:
        n = len(self._req("GET", "/loki/api/v1/labels").get("data") or [])
        return f"Loki — {n} labels"

    def counts(self, q, start, end, step, by=None):
        step = _clip_step(start, end, step)
        by_s = f" by ({', '.join(by)})" if by else ""
        mq = f"sum{by_s} (count_over_time({q} [{dtext(step)}]))"
        d = self._req("GET", "/loki/api/v1/query_range",
                      params={"query": mq, "start": int(start * 1e9), "end": int(end * 1e9),
                              "step": step})
        out = []
        for s in (d.get("data") or {}).get("result") or []:
            out.append({"group": s.get("metric") or {},
                        "points": [[float(t), float(v)] for t, v in s.get("values") or []]})
        return out

    def events(self, q, start, end, limit=100):
        d = self._req("GET", "/loki/api/v1/query_range",
                      params={"query": q, "start": int(start * 1e9), "end": int(end * 1e9),
                              "limit": limit, "direction": "backward"})
        out = []
        for s in (d.get("data") or {}).get("result") or []:
            lab = s.get("stream") or {}
            for ts, line in s.get("values") or []:
                fields = dict(lab)
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        fields.update({k: v for k, v in obj.items() if isinstance(v, (str, int, float, bool))})
                except ValueError:
                    pass
                out.append({"ts": int(ts) / 1e9, "text": line[:500], "fields": fields})
        out.sort(key=lambda e: -e["ts"])
        return out[:limit]

    def suggest(self, kind, metric="", label="", field="", q=""):
        if kind in ("labels", "fields"):
            items = self._req("GET", "/loki/api/v1/labels").get("data") or []
        elif kind == "values" and (label or field):
            items = self._req("GET", f"/loki/api/v1/label/{label or field}/values").get("data") or []
        else:
            items = []
        return [x for x in items if q.lower() in x.lower()][:200]


class Elastic(Adapter):
    logs = True

    def __init__(self, d):
        super().__init__(d)
        self.kind = d.get("kind") or "elasticsearch"
        self.index = self.opts.get("index") or "*"
        self.tf = self.opts.get("time_field") or "@timestamp"
        self.mf = self.opts.get("message_field") or "message"

    def test(self) -> str:
        info = self._req("GET", "/")
        ver = (info.get("version") or {}).get("number", "")
        dist = (info.get("version") or {}).get("distribution") or self.kind
        cnt = self._req("POST", f"/{self.index}/_count", json={"query": {"match_all": {}}})
        return f"{dist} {ver} — {cnt.get('count', 0)} documents in {self.index}"

    def _query(self, q, start, end) -> dict:
        rng = {"range": {self.tf: {"gte": int(start * 1000), "lte": int(end * 1000),
                                   "format": "epoch_millis"}}}
        q = (q or "").strip()
        if q.startswith("{"):
            try:
                user = json.loads(q)
            except ValueError as e:
                raise SourceError(f"the query DSL is not valid JSON ({e})") from None
            user = user.get("query", user)
            return {"bool": {"filter": [rng], "must": [user]}}
        must = [{"query_string": {"query": q, "analyze_wildcard": True}}] if q and q != "*" else []
        return {"bool": {"filter": [rng], "must": must}}

    def counts(self, q, start, end, step, by=None):
        step = max(_clip_step(start, end, step), 1)
        hist = {"date_histogram": {"field": self.tf, "fixed_interval": f"{int(step)}s",
                                   "min_doc_count": 0, "extended_bounds": {
                                       "min": int(start * 1000), "max": int(end * 1000)}}}
        if by:
            aggs = {"g": {"terms": {"field": by[0], "size": 20}, "aggs": {"h": hist}}}
        else:
            aggs = {"h": hist}
        d = self._req("POST", f"/{self.index}/_search",
                      json={"size": 0, "query": self._query(q, start, end), "aggs": aggs})
        ag = d.get("aggregations") or {}

        def pts(h):
            return [[b["key"] / 1000.0, float(b["doc_count"])] for b in (h.get("buckets") or [])]
        if by:
            return [{"group": {by[0]: str(b["key"])}, "points": pts(b["h"])}
                    for b in (ag.get("g") or {}).get("buckets") or []]
        return [{"group": {}, "points": pts(ag.get("h") or {})}]

    def events(self, q, start, end, limit=100):
        d = self._req("POST", f"/{self.index}/_search",
                      json={"size": min(limit, 10000), "query": self._query(q, start, end),
                            "sort": [{self.tf: {"order": "desc"}}]})
        out = []
        for h in (d.get("hits") or {}).get("hits") or []:
            src = h.get("_source") or {}
            flat = _flatten(src)
            ts = _parse_ts(flat.get(self.tf))
            text = flat.get(self.mf)
            out.append({"ts": ts, "text": str(text if text is not None else json.dumps(src))[:500],
                        "fields": {k: v for k, v in flat.items() if isinstance(v, (str, int, float, bool))}})
        return out

    def suggest(self, kind, metric="", label="", field="", q=""):
        if kind in ("fields", "labels"):
            m = self._req("GET", f"/{self.index}/_mapping")
            names: set[str] = set()
            for idx in m.values():
                _walk_mapping((idx.get("mappings") or {}).get("properties") or {}, "", names)
            items = sorted(names)
        elif kind == "values" and (field or label):
            f = field or label
            d = self._req("POST", f"/{self.index}/_search",
                          json={"size": 0, "aggs": {"v": {"terms": {"field": f, "size": 100}}}})
            items = [str(b["key"]) for b in ((d.get("aggregations") or {}).get("v") or {}).get("buckets") or []]
        else:
            items = []
        return [x for x in items if q.lower() in x.lower()][:200]


def _walk_mapping(props: dict, prefix: str, out: set) -> None:
    for k, v in props.items():
        name = f"{prefix}{k}"
        if "properties" in v:
            _walk_mapping(v["properties"], name + ".", out)
        else:
            out.add(name)
            for sub, sv in (v.get("fields") or {}).items():
                if sv.get("type") == "keyword":
                    out.add(f"{name}.{sub}")


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _parse_ts(v) -> float:
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


class Sokkan(Adapter):
    """SOKKAN's own events. Query grammar (space-separated, glob values):
    `audit action=agent.run.* user=*@acme.ch` — the project's audit journal ;
    `agents.failed` — agent runs that ended failed / timeout / budget ;
    `sessions.waiting` — approvals waiting for a person."""
    kind, logs = "sokkan", True

    def __init__(self, d):
        super().__init__(d)
        self.project = d.get("_project") or "default"

    def test(self) -> str:
        return "SOKKAN events — audit journal, agent runs, approvals"

    @staticmethod
    def _parse(q: str) -> tuple[str, dict]:
        parts = (q or "audit").split()
        what = parts[0] if parts and "=" not in parts[0] else "audit"
        flt = dict(p.split("=", 1) for p in parts if "=" in p)
        return what, flt

    def events(self, q, start, end, limit=100):
        what, flt = self._parse(q)
        out: list[dict] = []
        if what == "audit":
            import audit
            c = audit._con()
            rows = c.execute("SELECT ts, user, action, resource, detail, project FROM events "
                             "WHERE ts >= ? AND ts <= ? AND project = ? ORDER BY ts DESC LIMIT 5000",
                             (start, end, self.project)).fetchall()
            c.close()
            for r in rows:
                f = {k: r[k] for k in ("user", "action", "resource", "detail")}
                out.append({"ts": r["ts"], "text": f"{r['action']} {r['resource']} — {r['user']}".strip(),
                            "fields": f})
        elif what == "agents.failed":
            import agents
            for r in agents.runs_with_status("failed", "timeout", "budget"):
                ts = r.get("ended_at") or r.get("started_at") or 0
                if not (start <= (ts or 0) <= end):
                    continue
                if (r.get("project") or "default") != self.project:
                    continue
                f = {"agent": r.get("agent_name") or str(r.get("agent_id")), "status": r.get("status"),
                     "error": (r.get("error") or "")[:200], "run": r.get("id")}
                out.append({"ts": ts, "text": f"run #{r.get('id')} of {f['agent']}: {f['status']}",
                            "fields": f})
        elif what == "sessions.waiting":
            return []
        else:
            raise SourceError(f"unknown SOKKAN event stream {what!r} (audit, agents.failed)")
        out = [e for e in out if all(fnmatch.fnmatch(str(e["fields"].get(k, "")), v)
                                     for k, v in flt.items())]
        out.sort(key=lambda e: -e["ts"])
        return out[:limit]

    def counts(self, q, start, end, step, by=None):
        return bucketize(self.events(q, start, end, limit=100000), start, end, step, by)

    def suggest(self, kind, metric="", label="", field="", q=""):
        if kind in ("fields", "labels"):
            items = ["action", "user", "resource", "detail", "agent", "status", "error"]
        elif kind == "values" and (field or label) == "action":
            import audit
            c = audit._con()
            items = [r[0] for r in c.execute("SELECT DISTINCT action FROM events ORDER BY action LIMIT 500")]
            c.close()
        else:
            items = []
        return [x for x in items if q.lower() in x.lower()][:200]


class External(Adapter):
    """Alerts POSTed by an external system (Grafana alerting, any webhook) to
    /api/observability/alert: they are recorded as events and as alerts of a read-only rule."""
    kind, logs = "external", True

    def test(self) -> str:
        return "External alerts — POST /api/observability/alert (token SOKKAN_OBS_ALERT_TOKEN)"

    def events(self, q, start, end, limit=100):
        c = store.con()
        rows = c.execute("SELECT * FROM alerts WHERE rule_id IN (SELECT id FROM rules WHERE "
                         "external != '') AND started_at BETWEEN ? AND ? ORDER BY started_at DESC "
                         "LIMIT ?", (start, end, limit)).fetchall()
        c.close()
        return [{"ts": r["started_at"], "text": r["summary"], "fields": store.j(r["grp"], {})}
                for r in rows]

    def counts(self, q, start, end, step, by=None):
        return bucketize(self.events(q, start, end, limit=100000), start, end, step, by)


def bucketize(evts: list[dict], start: float, end: float, step: float, by=None) -> list[dict]:
    """Events → counts per step (and per `by` field values)."""
    step = max(step, 1)
    n = int(math.ceil((end - start) / step)) or 1
    groups: dict[str, dict] = {}
    for e in evts:
        g = {b: str(e["fields"].get(b, "")) for b in (by or [])}
        key = json.dumps(g, sort_keys=True)
        slot = groups.setdefault(key, {"group": g, "counts": [0] * n})
        i = int((e["ts"] - start) // step)
        if 0 <= i < n:
            slot["counts"][i] += 1
    if not groups and not by:
        groups["{}"] = {"group": {}, "counts": [0] * n}
    return [{"group": s["group"], "points": [[start + (i + 1) * step, float(c)]
                                             for i, c in enumerate(s["counts"])]}
            for s in groups.values()]


# =============================================================================================
# query builder — the form of the UI → a query, so nobody has to write PromQL
# =============================================================================================
_LABEL_RX = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_METRIC_RX = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _matchers(filters: list[dict], ops=("=", "!=", "=~", "!~")) -> str:
    out = []
    for f in filters or []:
        lab, op, val = (f.get("label") or f.get("field") or "").strip(), f.get("op") or "=", f.get("value", "")
        if not lab:
            continue
        if not _LABEL_RX.match(lab):
            raise ValueError(f"not a label name: {lab!r}")
        if op not in ops:
            raise ValueError(f"operator {op!r} not allowed here")
        out.append(f'{lab}{op}"{_esc(val)}"')
    return ", ".join(out)


def compile_prometheus(b: dict) -> str:
    metric = (b.get("metric") or "").strip()
    if not metric or not _METRIC_RX.match(metric):
        raise ValueError("choose a metric")
    m = _matchers(b.get("filters"))
    sel = f"{metric}{{{m}}}" if m else metric
    agg = b.get("agg") or "last"
    rng = dtext(seconds(b.get("range") or "5m"))
    by = [x for x in (b.get("by") or []) if x]
    for x in by:
        if not _LABEL_RX.match(x):
            raise ValueError(f"not a label name: {x!r}")
    by_s = f" by ({', '.join(by)})" if by else ""
    if agg in ("rate", "increase"):
        expr = f"sum{by_s} ({agg}({sel}[{rng}]))"
    elif agg in ("avg", "max", "min", "sum", "count"):
        expr = f"{agg}{by_s} ({sel})"
    elif agg == "p95":
        if metric.endswith("_bucket"):
            le_by = ", ".join(["le"] + by)
            expr = f"histogram_quantile(0.95, sum by ({le_by}) (rate({sel}[{rng}])))"
        else:
            expr = f"quantile_over_time(0.95, {sel}[{rng}])"
    elif agg == "last":
        expr = sel if not by else f"max{by_s} ({sel})"
    else:
        raise ValueError(f"unknown aggregation {agg!r}")
    den = b.get("ratio_of")
    if den:
        expr = f"100 * ({expr}) / ({compile_prometheus({**den, 'by': by})})"
    return expr


def compile_loki(b: dict) -> str:
    m = _matchers(b.get("filters"))
    sel = "{" + (m or 'job=~".+"') + "}"
    out = sel
    if (b.get("text") or "").strip():
        out += f' |= "{_esc(b["text"].strip())}"'
    if (b.get("level") or "").strip():
        lv = re.escape(b["level"].strip())
        out += f' |~ "(?i)(level[=:\\"]+{lv}|\\\\b{lv}\\\\b)"'
    return out


def _lucene_val(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def compile_elastic(b: dict) -> str:
    parts = []
    for f in b.get("filters") or []:
        fld, op, val = (f.get("field") or f.get("label") or "").strip(), f.get("op") or "=", f.get("value", "")
        if not fld:
            continue
        if not re.match(r"^[\w.@-]+$", fld):
            raise ValueError(f"not a field name: {fld!r}")
        if op == "=":
            parts.append(f"{fld}:{_lucene_val(val)}")
        elif op == "!=":
            parts.append(f"NOT {fld}:{_lucene_val(val)}")
        elif op == "exists":
            parts.append(f"_exists_:{fld}")
        elif op == "contains":
            esc = re.sub(r"([^\w])", r"\\\1", str(val))
            parts.append(f"{fld}:*{esc}*")
        else:
            raise ValueError(f"operator {op!r} not allowed here")
    if (b.get("text") or "").strip():
        parts.append(_lucene_val(b["text"].strip()))
    return " AND ".join(parts) or "*"


def compile_sokkan(b: dict) -> str:
    stream = (b.get("metric") or b.get("index") or "audit").strip() or "audit"
    flt = " ".join(f"{(f.get('field') or f.get('label'))}={f.get('value', '')}"
                   for f in b.get("filters") or [] if (f.get("field") or f.get("label")))
    return f"{stream} {flt}".strip()


def compile_query(kind: str, query: dict) -> str:
    """RuleIn.query → the query string the adapter runs."""
    q = query or {}
    if (q.get("mode") or "builder") == "raw":
        raw = (q.get("raw") or "").strip()
        if not raw:
            raise ValueError("write a query, or switch back to the builder")
        return raw
    b = q.get("builder") or {}
    if kind == "prometheus":
        return compile_prometheus(b)
    if kind == "loki":
        return compile_loki(b)
    if kind in ("elasticsearch", "opensearch"):
        return compile_elastic(b)
    if kind == "sokkan":
        return compile_sokkan(b)
    if kind == "external":
        return "*"
    raise ValueError(f"unknown source kind {kind!r}")
