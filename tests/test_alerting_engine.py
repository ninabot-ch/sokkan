"""SOKKAN 3.5 — Operate › Alerts: the engine without HTTP routes.

Rule types on synthetic data (each type fires where it should and only there), the backtest
folded with `for`, the state machine (pending → firing → resolved, realert, ack, silences,
quiet hours), the query builder (and its quoting), the source adapters against fake
Prometheus / Loki / Elasticsearch servers, and the channels' payloads (secrets never shown)."""
import hashlib
import hmac
import json
import math

import httpx
import pytest


@pytest.fixture()
def al(tmp_path, monkeypatch):
    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SOKKAN_PROM", raising=False)
    monkeypatch.delenv("SOKKAN_LOKI", raising=False)
    from alerting import alerts, channels, engine, rules, scheduler, sources, store, templates
    return {"alerts": alerts, "channels": channels, "engine": engine, "rules": rules,
            "scheduler": scheduler, "sources": sources, "store": store, "templates": templates,
            "tmp": tmp_path}


T0 = 1_800_000_000.0


class FakeAdapter:
    """Series / counts / events served from lists (what a source would return)."""
    kind = "fake"

    def __init__(self, series=None, events=None):
        self._series = series or []
        self._events = events or []

    def series(self, q, start, end, step):
        return [{"group": s["group"], "points": [p for p in s["points"] if start <= p[0] <= end]}
                for s in self._series]

    def events(self, q, start, end, limit=100):
        out = [e for e in self._events if start <= e["ts"] <= end]
        return sorted(out, key=lambda e: -e["ts"])[:limit]

    def counts(self, q, start, end, step, by=None):
        from alerting import sources
        return sources.bucketize(self.events(q, start, end, 100000), start, end, step, by)


PROM = {"id": 1, "kind": "prometheus", "name": "Prometheus"}
LOGS = {"id": 2, "kind": "loki", "name": "Loki"}


def _rule(engine, src, **kw):
    body = {"name": "r", "source_id": src["id"], "query": {"mode": "raw", "raw": "x"},
            "type": "threshold", "params": {"op": ">", "value": 5}, "every": "1m", "for": "0s"}
    body.update(kw)
    return engine.normalize(body, src)


def _line(values, step=60.0, start=T0, group=None):
    return {"group": group or {}, "points": [[start + i * step, float(v)] for i, v in enumerate(values)]}


# ---- durations / normalisation -------------------------------------------------------------
def test_durations():
    from alerting.durations import BadDuration, human, seconds, text
    assert seconds("5m") == 300 and seconds("1h") == 3600 and seconds("7d") == 604800 and seconds(90) == 90
    assert text(300) == "5m" and text(86400) == "1d" and text(90) == "90s"
    assert human(300) == "5 minutes" and human(3600) == "1 hour"
    with pytest.raises(BadDuration):
        seconds("five minutes")


def test_a_type_on_the_wrong_kind_of_source_is_refused_in_words(al):
    e = al["engine"]
    with pytest.raises(e.RuleError, match="works on events"):
        _rule(e, PROM, type="frequency", params={"count": 3, "window": "5m"})
    with pytest.raises(e.RuleError, match="works on metrics"):
        _rule(e, LOGS, type="absence", params={"window": "5m"})
    with pytest.raises(e.RuleError, match="name"):
        e.normalize({"source_id": 1}, PROM)
    with pytest.raises(e.RuleError, match="source"):
        e.normalize({"name": "x"}, None)
    with pytest.raises(e.RuleError, match="every 15 s"):
        _rule(e, PROM, every="5s")
    with pytest.raises(e.RuleError, match="ratio"):
        _rule(e, PROM, type="spike", params={"ratio": 1})


def test_sentences_say_what_the_rule_means(al):
    e = al["engine"]
    r = _rule(e, PROM, query={"mode": "builder", "builder": {
        "metric": "http_requests_total", "agg": "rate", "range": "5m",
        "filters": [{"label": "status", "op": "=~", "value": "5.."}]}},
        params={"op": ">", "value": 2}, group_by=["job"], **{"for": "5m"})
    assert r["sentence"] == ("Alert when the rate of http_requests_total is above 2/s, "
                             "where status matches 5.., by job, for 5 minutes")
    r = _rule(e, LOGS, type="frequency", params={"count": 50, "window": "10m"},
              query={"mode": "builder", "builder": {"text": "error"}})
    assert r["sentence"] == "Alert when there are 50 or more events containing « error » within 10 minutes"
    r = _rule(e, LOGS, type="flatline", params={"count": 1, "window": "10m"})
    assert "fewer than 1" in r["sentence"]


# ---- query builder --------------------------------------------------------------------------
def test_prometheus_builder(al):
    s = al["sources"]
    assert s.compile_prometheus({"metric": "http_requests_total", "agg": "rate", "range": "5m",
                                 "filters": [{"label": "status", "op": "=~", "value": "5.."}],
                                 "by": ["job"]}) == \
        'sum by (job) (rate(http_requests_total{status=~"5.."}[5m]))'
    assert s.compile_prometheus({"metric": "up", "agg": "last"}) == "up"
    assert s.compile_prometheus({"metric": "req_duration_seconds_bucket", "agg": "p95", "range": "5m"}) == \
        "histogram_quantile(0.95, sum by (le) (rate(req_duration_seconds_bucket[5m])))"
    ratio = s.compile_prometheus({"metric": "h", "agg": "rate", "range": "5m",
                                  "filters": [{"label": "code", "op": "=~", "value": "5.."}],
                                  "ratio_of": {"metric": "h", "agg": "rate", "range": "5m"}})
    assert ratio == '100 * (sum (rate(h{code=~"5.."}[5m]))) / (sum (rate(h[5m])))'


def test_builder_quotes_values_and_refuses_injected_names(al):
    s = al["sources"]
    q = s.compile_prometheus({"metric": "up", "agg": "last",
                              "filters": [{"label": "job", "op": "=", "value": 'a"}) or vector(1) #'}]})
    assert q == 'up{job="a\\"}) or vector(1) #"}'
    with pytest.raises(ValueError):
        s.compile_prometheus({"metric": "up) or vector(1", "agg": "last"})
    with pytest.raises(ValueError):
        s.compile_prometheus({"metric": "up", "agg": "last", "filters": [{"label": "a}b", "value": 1}]})
    assert s.compile_loki({"filters": [{"label": "app", "op": "=", "value": "api"}], "text": 'x"y'}) == \
        '{app="api"} |= "x\\"y"'
    assert s.compile_loki({}) == '{job=~".+"}'
    assert s.compile_elastic({"filters": [{"field": "level", "op": "=", "value": "error"},
                                          {"field": "host", "op": "!=", "value": "db1"},
                                          {"field": "trace", "op": "exists"}], "text": "timeout"}) == \
        'level:"error" AND NOT host:"db1" AND _exists_:trace AND "timeout"'
    with pytest.raises(ValueError):
        s.compile_elastic({"filters": [{"field": "a b", "op": "=", "value": 1}]})
    assert s.compile_sokkan({"metric": "audit", "filters": [{"field": "action", "value": "auth.*"}]}) == \
        "audit action=auth.*"


# ---- rule types ------------------------------------------------------------------------------
def test_threshold_backtest_with_for(al):
    e = al["engine"]
    vals = [1, 1, 9, 9, 9, 9, 1, 1, 9, 1]       # 4 steps above, then a single blip
    ad = FakeAdapter(series=[_line(vals, group={"job": "api"})])
    r = _rule(e, PROM, **{"for": "2m"})
    ev = e.evaluate(r, PROM, ad, T0, T0 + 9 * 60, 60)
    fi = e.fired_intervals(ev, 120)
    assert len(fi) == 1 and fi[0]["start"] == T0 + 4 * 60 and fi[0]["end"] == T0 + 6 * 60 and fi[0]["peak"] == 9
    assert len(e.fired_intervals(ev, 0)) == 2           # without « for » the blip rings too


def test_threshold_reduce_over_a_window(al):
    e = al["engine"]
    ad = FakeAdapter(series=[_line([0, 0, 30, 0, 0])])
    r = _rule(e, PROM, params={"op": ">", "value": 5, "reduce": "avg", "window": "3m"})
    ev = e.evaluate(r, PROM, ad, T0, T0 + 4 * 60, 60)
    assert [round(v, 1) for _, v in ev["series"][0]["points"]] == [0, 0, 10, 10, 10]


def test_frequency_and_flatline_on_events(al):
    e = al["engine"]
    evts = [{"ts": T0 + 600 + i, "text": "error", "fields": {}} for i in range(12)]
    ad = FakeAdapter(events=evts)
    r = _rule(e, LOGS, type="frequency", params={"count": 10, "window": "5m"})
    p = e.preview(r, LOGS, ad, "1h", T0 + 3600)
    assert p["fires"] == 1 and p["kind"] == "count" and p["threshold"] == {"op": ">=", "value": 10}
    assert p["sample_events"] and p["sample_events"][0]["text"] == "error"
    r = _rule(e, LOGS, type="flatline", params={"count": 1, "window": "10m"})
    p = e.preview(r, LOGS, ad, "1h", T0 + 3600)
    # silent before the burst and again 10 min after it; never while it is in the window
    assert p["fires"] == 2 and all(not (iv["start"] <= T0 + 900 <= (iv["end"] or 1e12))
                                   for iv in p["fired_intervals"])


def test_spike_against_the_reference(al):
    e = al["engine"]
    base = [{"ts": T0 + i * 60, "text": "", "fields": {}} for i in range(120)]          # 1 / min
    burst = [{"ts": T0 + 7000 + i, "text": "", "fields": {}} for i in range(100)]      # 100 in 100 s
    ad = FakeAdapter(events=base + burst)
    r = _rule(e, LOGS, type="spike", params={"ratio": 3, "window": "10m", "reference": "1h", "min_count": 20})
    p = e.preview(r, LOGS, ad, "1h", T0 + 7200)
    assert p["fires"] == 1 and p["reference"] is not None
    calm = FakeAdapter(events=base)
    assert e.preview(r, LOGS, calm, "1h", T0 + 7200)["fires"] == 0


def test_cardinality_any_new_term_change(al):
    e = al["engine"]
    evts = [{"ts": T0 + 60 * i, "text": f"login {u}", "fields": {"user": u, "host": "h1",
                                                                 "status": "up" if i < 5 else "down"}}
            for i, u in enumerate(["a", "b", "c", "d", "e", "f", "g"])]
    ad = FakeAdapter(events=evts)
    r = _rule(e, LOGS, type="cardinality", params={"field": "user", "op": ">", "value": 4, "window": "10m"})
    ev = e.evaluate(r, LOGS, ad, T0, T0 + 600, 60)
    assert any(ev["series"][0]["cond"]) and max(v for _, v in ev["series"][0]["points"]) == 7
    r = _rule(e, LOGS, type="any", params={})
    p = e.preview(r, LOGS, ad, "1h", T0 + 600)
    assert p["kind"] == "events" and p["fires"] >= 1 and len(p["sample_events"]) == 5
    r = _rule(e, LOGS, type="new_term", params={"field": "user", "lookback": "1d"})
    ev = e.evaluate(r, LOGS, ad, T0 + 290, T0 + 600, 60, known_terms={"a", "b", "c", "d", "e"})
    assert set(ev["new_terms"]) == {"f", "g"}
    r = _rule(e, LOGS, type="change", params={"field": "status", "key_field": "host"})
    ev = e.evaluate(r, LOGS, ad, T0, T0 + 600, 60, last_values={})
    assert sum(sum(1 for c in s["cond"] if c) for s in ev["series"]) == 1
    assert ev["changes"] == {"h1": "down"}
    assert "up → down" in ev["sample_events"][0]["text"]


def test_absence_and_anomaly(al):
    e = al["engine"]
    pts = _line([1] * 30 + [], step=60)
    ad = FakeAdapter(series=[pts])
    r = _rule(e, PROM, type="absence", params={"window": "5m"})
    ev = e.evaluate(r, PROM, ad, T0, T0 + 60 * 60, 60)
    cond = ev["series"][0]["cond"]
    assert not any(cond[:29]) and all(cond[36:])          # silent 5 min after the last point
    vals = [10 + (i % 3) * 0.5 for i in range(60)] + [40]
    ad = FakeAdapter(series=[_line(vals)])
    r = _rule(e, PROM, type="anomaly", params={"z": 3, "lookback": "1h"})
    ev = e.evaluate(r, PROM, ad, T0, T0 + 60 * 60, 60)
    assert ev["series"][0]["cond"][-1] and not any(ev["series"][0]["cond"][:-1])
    assert ev["reference"]["upper"]


def test_preview_reports_a_source_error_in_the_graph_not_a_500(al):
    e, s = al["engine"], al["sources"]

    class Broken(FakeAdapter):
        def series(self, *a):
            raise s.SourceError("prometheus: cannot reach http://x (ConnectError)")
    p = e.preview(_rule(e, PROM), PROM, Broken(), "24h", T0)
    assert p["error"].startswith("prometheus: cannot reach") and p["series"] == [] and p["fires"] == 0


def test_many_series_are_capped_and_said(al):
    e = al["engine"]
    ad = FakeAdapter(series=[_line([i] * 5, group={"pod": f"p{i}"}) for i in range(30)])
    ev = e.evaluate(_rule(e, PROM), PROM, ad, T0, T0 + 240, 60)
    assert len(ev["series"]) == 20 and "only the 20 largest" in ev["warnings"][0]
    assert ev["series"][0]["group"] == {"pod": "p29"}


# ---- state machine ---------------------------------------------------------------------------
def _stored_rule(al, **kw):
    s = al["sources"]
    s.ensure_builtins()
    src = next(x for x in s.list_sources("default") if x["kind"] == "sokkan")
    body = {"name": "API errors", "source_id": src["id"], "type": "any", "params": {},
            "query": {"mode": "raw", "raw": "audit"}, "every": "1m", "for": "0s", "realert": "10m",
            "no_notification": True}
    body.update(kw)
    return al["rules"].create("default", body, "bob@x")


def _obs(cond, gk="job=api", value=7.0):
    return [{"group": {"job": "api"}, "group_key": gk, "cond": cond, "value": value, "samples": []}]


def test_pending_firing_resolved_and_realert(al):
    A = al["alerts"]
    r = {**_stored_rule(al, **{"for": "2m"}), "_threshold": {"op": ">", "value": 5}}
    assert A.apply(r, _obs(True), T0) == []                         # pending
    a = A.list_alerts("default")[0][0]
    assert a["state"] == "pending" and a["summary"] == "API errors: 7 events > 5 (api)"
    assert A.apply(r, _obs(True), T0 + 60) == []                    # still pending (60 s < 2 m)
    ev = A.apply(r, _obs(True), T0 + 120)
    assert [k for k, _ in ev] == ["firing"]
    A.mark_notified(ev[0][1]["id"], T0 + 120)
    assert A.apply(r, _obs(True), T0 + 300) == []                   # realert 10 m not reached
    assert [k for k, _ in A.apply(r, _obs(True), T0 + 721)] == ["renotify"]
    assert [k for k, _ in A.apply(r, _obs(False), T0 + 800)] == ["resolved"]
    h = A.history(r["id"])
    assert [(x["from"], x["to"]) for x in reversed(h)] == [("ok", "pending"), ("pending", "firing"),
                                                          ("firing", "resolved")]


def test_a_pending_alert_that_clears_never_rings_and_a_vanished_group_resolves(al):
    A = al["alerts"]
    r = {**_stored_rule(al, **{"for": "5m"}), "_threshold": None}
    A.apply(r, _obs(True), T0)
    assert A.apply(r, _obs(False), T0 + 60) == []
    r0 = {**_stored_rule(al, name="b"), "_threshold": None}
    assert [k for k, _ in A.apply(r0, _obs(True, gk="pod=a"), T0)] == ["firing"]
    assert [k for k, _ in A.apply(r0, [], T0 + 60)] == ["resolved"]
    assert A.history(r0["id"])[0]["note"] == "group no longer reported"


def test_ack_stops_reminders_and_silence_mutes_without_hiding(al, monkeypatch):
    A, S = al["alerts"], al["scheduler"]
    sent = []
    monkeypatch.setattr(al["channels"], "deliver", lambda cid, kind, a, r: sent.append((cid, kind)) or "ok")
    monkeypatch.setattr(S, "_pool", type("P", (), {"submit": staticmethod(lambda f, *a: f(*a))})())
    r = {**_stored_rule(al, channels=[]), "_threshold": None}
    r["channels"] = [7]
    ev = A.apply(r, _obs(True), T0)
    S._on_event("firing", ev[0][1]["id"], r, T0)
    assert sent == [(7, "firing")]
    aid = ev[0][1]["id"]
    A.ack(aid, "bob@x")
    S._on_event("renotify", aid, r, T0 + 700)
    assert sent == [(7, "firing")]
    A.add_silence("default", r["id"], {"job": "api"}, 0, 4e9, "deploy", "bob@x")
    r2 = {**_stored_rule(al, name="other"), "_threshold": None, "channels": [7]}
    a2 = A.apply(r2, _obs(True), T0)
    S._on_event("firing", a2[0][1]["id"], r2, T0)
    assert sent == [(7, "firing"), (7, "firing")]                     # other rule: not silenced
    A.apply(r, _obs(False), T0 + 800)
    ev = A.apply(r, _obs(True), T0 + 900)
    S._on_event("firing", ev[0][1]["id"], r, T0 + 900)
    assert len(sent) == 2                                             # silenced: no ring …
    got = A.get(ev[0][1]["id"])
    assert got["state"] == "firing" and got["silenced_until"] == 4e9  # … but visible as firing


def test_quiet_hours(al):
    A = al["alerts"]
    qh = {"enabled": True, "start": "22:00", "end": "07:00", "tz": "UTC", "days": list(range(7))}
    from datetime import datetime, timezone
    night = datetime(2026, 10, 9, 23, 30, tzinfo=timezone.utc).timestamp()
    day = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc).timestamp()
    assert A.in_quiet_hours(qh, night) and not A.in_quiet_hours(qh, day)
    assert not A.in_quiet_hours({**qh, "enabled": False}, night)
    assert not A.in_quiet_hours({**qh, "days": [0]}, night)            # 09.10.2026 is a Friday


# ---- adapters against fake servers -----------------------------------------------------------
class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers=None, auth=None, timeout=None, params=None, json=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "auth": auth,
                           "params": params, "json": json})
        for prefix, fn in self.routes.items():
            if url.endswith(prefix) or prefix in url:
                status, body = fn(params, json)
                return httpx.Response(status, json=body, request=httpx.Request(method, url))
        return httpx.Response(404, json={"error": "nope"}, request=httpx.Request(method, url))


def test_prometheus_adapter(al, monkeypatch):
    s = al["sources"]
    fake = FakeHTTP({
        "/api/v1/query_range": lambda p, j: (200, {"status": "success", "data": {"result": [
            {"metric": {"__name__": "up", "job": "api"}, "values": [[T0, "1"], [T0 + 60, "NaN"], [T0 + 120, "0"]]}]}}),
        "/api/v1/status/buildinfo": lambda p, j: (200, {"data": {"version": "2.55.1"}}),
        "/api/v1/label/__name__/values": lambda p, j: (200, {"data": ["up", "node_load1"]}),
    })
    monkeypatch.setattr(s.httpx, "request", fake)
    d = {"kind": "prometheus", "url": "http://prom:9090", "auth_kind": "bearer", "secret": "tok"}
    ad = s.adapter_for(d)
    assert ad.series("up", T0, T0 + 120, 60) == [{"group": {"job": "api"}, "points": [[T0, 1.0], [T0 + 120, 0.0]]}]
    assert fake.calls[0]["headers"] == {"Authorization": "Bearer tok"}
    assert s.test(d)["detail"] == "Prometheus 2.55.1 — 2 metrics"
    assert ad.suggest("metrics", q="node") == ["node_load1"]
    bad = FakeHTTP({"/api/v1/query_range": lambda p, j: (400, {"error": "parse error at char 3"})})
    monkeypatch.setattr(s.httpx, "request", bad)
    with pytest.raises(s.SourceError, match="parse error at char 3"):
        ad.series("up(", T0, T0 + 60, 60)
    den = FakeHTTP({"/": lambda p, j: (401, {})})
    monkeypatch.setattr(s.httpx, "request", den)
    assert "access refused" in s.test(d)["error"]


def test_loki_adapter(al, monkeypatch):
    s = al["sources"]
    seen = {}

    def qr(p, j):
        seen.setdefault("q", []).append(p["query"])
        if p["query"].startswith("sum"):
            return 200, {"data": {"result": [{"metric": {"app": "api"}, "values": [[T0, "3"]]}]}}
        return 200, {"data": {"result": [{"stream": {"app": "api"}, "values": [
            [str(int((T0 + 5) * 1e9)), '{"level":"error","msg":"boom"}'], [str(int(T0 * 1e9)), "plain line"]]}]}}
    monkeypatch.setattr(s.httpx, "request", FakeHTTP({"/loki/api/v1/query_range": qr}))
    ad = s.adapter_for({"kind": "loki", "url": "http://loki:3100"})
    assert ad.counts('{app="api"}', T0, T0 + 600, 60, ["app"]) == [{"group": {"app": "api"}, "points": [[T0, 3.0]]}]
    assert seen["q"][0] == 'sum by (app) (count_over_time({app="api"} [1m]))'
    ev = ad.events('{app="api"}', T0, T0 + 600)
    assert ev[0]["fields"] == {"app": "api", "level": "error", "msg": "boom"} and ev[1]["text"] == "plain line"


def test_elastic_adapter(al, monkeypatch):
    s = al["sources"]
    calls = []

    def search(p, j):
        calls.append(j)
        if j.get("size") == 0:
            return 200, {"aggregations": {"h": {"buckets": [{"key": T0 * 1000, "doc_count": 4}]}}}
        return 200, {"hits": {"hits": [{"_source": {"@timestamp": "2027-01-15T08:00:00Z", "message": "oops",
                                                    "error": {"type": "Timeout"}, "host": {"name": "web1"}}}]}}
    monkeypatch.setattr(s.httpx, "request", FakeHTTP({"/_search": search}))
    d = {"kind": "elasticsearch", "url": "https://es:9200", "auth_kind": "apikey", "secret": "k",
         "options": {"index": "logs-*", "time_field": "@timestamp", "message_field": "message"}}
    ad = s.adapter_for(d)
    assert ad.counts('level:"error"', T0, T0 + 600, 60) == [{"group": {}, "points": [[T0, 4.0]]}]
    q = calls[0]["query"]["bool"]
    assert q["must"] == [{"query_string": {"query": 'level:"error"', "analyze_wildcard": True}}]
    assert q["filter"][0]["range"]["@timestamp"]["gte"] == int(T0 * 1000)
    ev = ad.events("*", T0, T0 + 600)
    assert ev[0]["text"] == "oops" and ev[0]["fields"]["error.type"] == "Timeout"
    assert ad.events('{"term": {"host.name": "web1"}}', T0, T0 + 1)
    assert calls[-1]["query"]["bool"]["must"] == [{"term": {"host.name": "web1"}}]
    with pytest.raises(s.SourceError, match="not valid JSON"):
        ad.events("{bad", T0, T0 + 1)


def test_sokkan_source_reads_the_projects_journal_only(al):
    import audit
    s = al["sources"]
    audit.log("x@y", "auth.login.refused", "x@y", "no role", project="default")
    audit.log("x@y", "auth.login.refused", "x@y", "no role", project="radio")
    audit.log("x@y", "board.card.create", "#1", "", project="default")
    ad = s.adapter_for({"kind": "sokkan", "_project": "default"})
    import time
    ev = ad.events("audit action=auth.*", time.time() - 60, time.time() + 1)
    assert len(ev) == 1 and ev[0]["fields"]["action"] == "auth.login.refused"


# ---- channels --------------------------------------------------------------------------------
def test_channel_secrets_are_sealed_and_payloads_signed(al, monkeypatch):
    C = al["channels"]
    posted = []

    def post(url, json=None, content=None, headers=None, timeout=None):
        posted.append({"url": url, "json": json, "content": content, "headers": headers or {}})
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr(C.httpx, "post", post)
    wh = C.create("default", {"kind": "webhook", "name": "hook",
                              "config": {"url": "https://hooks.example/x", "secret": "Sign-Me-S3cr3t-42"}}, "a@x")
    assert wh["config"] == {"url_set": True, "secret_set": True}
    raw = b"".join(p.read_bytes() for p in al["tmp"].glob("alerting.db*"))
    assert b"hooks.example" not in raw and b"Sign-Me-S3cr3t-42" not in raw
    tg = C.create("default", {"kind": "telegram", "config": {"bot_token": "123:ABC", "chat_id": "-100"}}, "a@x")
    assert tg["config"] == {"chat_id": "-100", "bot_token_set": True}
    alert = {"id": 3, "severity": "critical", "summary": "API errors: 9 > 5", "group": {"job": "api"},
             "group_key": "job=api", "link": "/?plane=operate&tab=alerts&alert=3"}
    rule = {"id": 1, "name": "API errors", "sentence": "Alert when …", "severity": "critical"}
    assert C.deliver(wh["id"], "firing", alert, rule) == "ok"
    body = posted[-1]["content"]
    sig = hmac.new(b"Sign-Me-S3cr3t-42", body, hashlib.sha256).hexdigest()
    assert posted[-1]["headers"]["x-sokkan-signature"] == f"sha256={sig}"
    assert json.loads(body)["event"] == "firing" and json.loads(body)["rule"]["name"] == "API errors"
    assert C.deliver(tg["id"], "resolved", alert, rule) == "ok"
    assert posted[-1]["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert posted[-1]["json"]["text"].startswith("✅ Resolved — API errors")
    with pytest.raises(ValueError, match="required"):
        C.create("default", {"kind": "slack", "config": {}}, "a@x")
    with pytest.raises(ValueError, match="https://"):
        C.create("default", {"kind": "slack", "config": {"webhook_url": "http://x"}}, "a@x")
    upd = C.update(tg["id"], {"kind": "telegram", "config": {"chat_id": "-200"}})
    assert upd["config"] == {"chat_id": "-200", "bot_token_set": True}   # empty secret = unchanged


def test_a_failing_channel_reports_never_raises(al, monkeypatch):
    C = al["channels"]

    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(C.httpx, "post", boom)
    sl = C.create("default", {"kind": "slack", "config": {"webhook_url": "https://hooks.slack.com/x"}}, "a@x")
    res = C.test(sl["id"], "default")
    assert not res["ok"] and res["detail"].startswith("error: ConnectError")
    assert C.get(sl["id"])["last_test"]["ok"] is False


def test_pagerduty_and_teams_card(al, monkeypatch):
    C = al["channels"]
    posted = []
    monkeypatch.setattr(C.httpx, "post", lambda url, json=None, timeout=None, **k: posted.append(json) or
                        httpx.Response(202, request=httpx.Request("POST", url)))
    pd = C.create("default", {"kind": "pagerduty", "config": {"routing_key": "R"}}, "a@x")
    alert = {"id": 3, "severity": "warning", "summary": "s", "group": {}, "group_key": "g", "link": "/x"}
    assert C.deliver(pd["id"], "resolved", alert, {"id": 9, "name": "n"}) == "ok"
    assert posted[-1]["event_action"] == "resolve" and posted[-1]["dedup_key"] == "sokkan-9-g"
    card = C.teams_card(C.message("firing", alert, {"id": 9, "name": "n"}), alert)
    verbs = [a.get("verb") for a in card["body"][-1]["actions"]]
    assert verbs == ["alert.ack", "alert.silence", "alert.incident", None]


# ---- templates -------------------------------------------------------------------------------
def test_templates_say_what_they_need(al, monkeypatch):
    s, tpl = al["sources"], al["templates"]
    monkeypatch.setenv("SOKKAN_PROM", "http://prom:9090")
    monkeypatch.setattr(s.httpx, "request", FakeHTTP({
        "/api/v1/label/__name__/values": lambda p, j: (200, {"data": ["up", "node_cpu_seconds_total",
                                                                      "node_memory_MemAvailable_bytes"]}),
        "/api/v1/labels": lambda p, j: (200, {"data": ["job", "instance"]})}))
    ts = {t["id"]: t for t in tpl.build("default")}
    assert ts["host-cpu"]["available"] and ts["target-down"]["available"]
    assert not ts["http-5xx-rate"]["available"] and "http_requests_total" in ts["http-5xx-rate"]["missing"]
    assert not ts["log-errors"]["available"] and "No log source" in ts["log-errors"]["missing"]
    assert ts["agent-run-failed"]["available"]
    e = al["engine"]
    for t in ts.values():                       # every available template is a valid rule
        if t["available"]:
            src = s.get(t["source_id"])
            e.normalize(t["rule"], src)
    r = tpl.apply_variables(ts["host-cpu"], {"threshold": 75, "for": "15m"}, "default")
    assert r["params"]["value"] == 75 and r["for"] == "15m"


# ---- optional: a real Prometheus (read only) ----------------------------------------------------
@pytest.mark.skipif(not __import__("os").environ.get("SOKKAN_TEST_PROM"),
                    reason="set SOKKAN_TEST_PROM=http://host:9090 to backtest on a real Prometheus")
def test_backtest_on_a_real_prometheus(al, monkeypatch):
    import os
    import time
    monkeypatch.setenv("SOKKAN_PROM", os.environ["SOKKAN_TEST_PROM"])
    s, e, tpl = al["sources"], al["engine"], al["templates"]
    s.ensure_builtins()
    prom = next(x for x in s.list_sources("default") if x["kind"] == "prometheus")
    assert s.test(s._raw(prom["id"]))["ok"]
    t = {x["id"]: x for x in tpl.build("default")}["target-down"]
    r = e.normalize(t["rule"], prom)
    p = e.preview(r, prom, s.adapter(prom["id"]), "24h", time.time())
    assert p["error"] is None and p["series"] and all(len(x["points"]) > 100 for x in p["series"])
    assert math.isfinite(p["series"][0]["points"][-1][1])


# ---- 09.10 journey (Inès): what the person reads ----------------------------------------------
def test_a_raw_template_query_is_said_in_words(al):
    """The memory / CPU / disk templates are PromQL: the sentence said « the query (Prometheus) »
    and the form quoted the PromQL. Their `label` is kept and said; a raw query without one is not
    dressed up."""
    e = al["engine"]
    r = _rule(e, PROM, query={"mode": "raw", "raw": "(1 - a / b) * 100", "label": "memory used (%)"},
              params={"op": ">", "value": 85})
    assert r["query"]["label"] == "memory used (%)"
    assert r["sentence"] == "Alert when memory used is above 85 %" and r["unit"] == "%"
    bare = _rule(e, PROM)
    assert "label" not in bare["query"] and "the query (Prometheus)" in bare["sentence"]


def test_the_rule_value_is_the_worst_toward_the_line(al, monkeypatch):
    """« below 1 » on 18 targets, one of them down: the list showed 1 (the max) next to « Firing »;
    the value that matters is the one breaking the rule (min for a « below » rule)."""
    s, sch = al["sources"], al["scheduler"]
    s.ensure_builtins()
    monkeypatch.setenv("SOKKAN_PROM", "http://prom:9090")
    s.ensure_builtins()
    src = next(x for x in s.list_sources("default") if x["kind"] == "prometheus")
    up = [_line([1] * 10, group={"job": "a"}), _line([0] * 10, group={"job": "b"})]
    monkeypatch.setattr(sch, "_adapter", lambda r, src: FakeAdapter(series=up))
    r = al["rules"].create("default", {"name": "down", "source_id": src["id"], "type": "threshold",
                                       "query": {"mode": "builder", "builder": {"metric": "up", "agg": "last", "by": ["job"]}},
                                       "params": {"op": "<", "value": 1}, "group_by": ["job"],
                                       "every": "1m", "for": "0s", "no_notification": True}, "ines@x")
    sch.evaluate_rule(al["rules"].get(r["id"]), T0 + 600)
    assert al["rules"].get(r["id"])["state"]["last_value"] == 0.0
