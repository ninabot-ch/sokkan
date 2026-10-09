"""3.5 polish — what a person reads: host names, units, « down », raw PromQL said in words."""
import json

import pytest

import test_alerting_engine as _eng
from test_alerting_engine import PROM, T0, FakeAdapter, _line, _rule

al = pytest.fixture()(_eng.al.__wrapped__)   # the engine tests' fixture, reused as is

@pytest.fixture()
def names(monkeypatch):
    import hostnames
    monkeypatch.setenv("SOKKAN_HOSTS", json.dumps({"100.76.30.90": "rpi1", "100.124.110.23": "rog1"}))
    hostnames.clear_caches()
    yield
    hostnames.clear_caches()


def test_addresses_become_host_names_everywhere(al, names):
    from alerting import humanize as h
    g = {"job": "node", "instance": "100.76.30.90:9100"}
    assert h.group_title(g) == "rpi1 · node"
    assert h.group_display(g) == {"instance": "rpi1"}
    assert h.group_where(g) == "job=node, rpi1 (100.76.30.90:9100)"
    assert h.group_title({"job": "api"}) == "api"            # not an address: unchanged
    assert h.group_title({"instance": "10.9.9.9:9100"}) == "10.9.9.9:9100"   # unknown: the address


def test_values_with_their_unit():
    from alerting import humanize as h
    assert h.fmt(77.834, "%") == "77.8 %"
    assert h.fmt(1.5 * 2**30, "bytes") == "1.5 GB"
    assert h.fmt(0.35, "s") == "350 ms"
    assert h.fmt(4.25, "/s") == "4.2/s" or h.fmt(4.25, "/s") == "4.3/s"
    assert h.fmt(12, "events") == "12 events" and h.fmt(1, "events") == "1 event"
    assert h.value_text(77.8, {"op": ">", "value": 50}, "%") == "77.8 % > 50 %"
    assert h.value_text(0, {"op": "<", "value": 1}) == "down"                  # up < 1
    assert h.value_text(None, {}, "", "absence") == "no data — unreachable"
    assert h.value_text(12, {"op": ">=", "value": 10, "type": "frequency", "window": "5m"}) \
        == "12 events in 5 minutes (≥ 10)"


def test_raw_promql_is_said_in_words_never_shown():
    from alerting import humanize as h
    assert h.describe_raw("(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100") \
        == ("memory used", "%")
    assert h.describe_raw('100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100') \
        == ("CPU used", "%")
    assert h.describe_raw("rate(node_network_receive_bytes_total[5m])") == ("node network receive bytes", "/s")
    assert h.describe_raw("up == 0")[0] == "target up"


def test_sentence_of_a_raw_rule_without_label_has_no_query_and_has_the_unit(al):
    e = al["engine"]
    r = _rule(e, PROM, query={"mode": "raw", "raw": "(1 - node_memory_MemAvailable_bytes / "
                                                     "node_memory_MemTotal_bytes) * 100"},
              params={"op": ">", "value": 85}, group_by=["instance"])
    assert r["unit"] == "%"
    assert r["sentence"] == "Alert when memory used is above 85 %, by instance"
    assert "node_memory" not in r["sentence"] and "the query" not in r["sentence"]
    # a template label « memory used (%) » does not say « (%) » twice
    r = _rule(e, PROM, query={"mode": "raw", "raw": "x", "label": "memory used (%)"},
              params={"op": ">", "value": 85})
    assert r["sentence"] == "Alert when memory used is above 85 %"
    # the person's unit wins
    r = _rule(e, PROM, unit="bytes", params={"op": ">", "value": 2 * 2**30})
    assert r["sentence"].endswith("above 2 GB")


def test_rules_saved_before_say_the_query_in_words_on_read(al):
    """A 3.5-beta rule stored « the query (Prometheus) » without unit: read back in words."""
    s, rules, store = al["sources"], al["rules"], al["store"]
    src = s.create("default", {"name": "Prometheus", "kind": "prometheus", "url": "http://p.invalid"}, "t") \
        if hasattr(s, "create") else None
    if src is None:
        pytest.skip("no sources.create")
    body = {"name": "old", "source_id": src["id"], "query": {"mode": "raw", "raw":
            "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100", "builder": {}},
            "type": "threshold", "params": {"op": ">", "value": 85, "reduce": "last", "window": "0s"},
            "every": "1m", "for": "0s", "group_by": ["instance"], "realert": "30m", "channels": [],
            "sentence": "Alert when the query (Prometheus) is above 85, by instance", "severity": "warning"}
    c = store.con()
    cur = c.execute("INSERT INTO rules(project, body, enabled, created_by, created_at, updated_at, state,"
                    " next_eval) VALUES('default', ?, 1, 't', 0, 0, '{}', 0)", (json.dumps(body),))
    c.commit()
    rid = cur.lastrowid
    c.close()
    r = rules.get(rid)
    assert r["sentence"] == "Alert when memory used is above 85 %, by instance"
    assert r["unit"] == "%"


def test_alert_summary_and_message_read_like_a_person_wrote_them(al, names):
    a, ch = al["alerts"], al["channels"]
    rule = {"id": 1, "project": "default", "name": "Service down", "severity": "critical",
            "type": "threshold", "unit": "", "_threshold": {"op": "<", "value": 1, "unit": "", "type": "threshold"}}
    s = a.summary(rule, {"job": "node", "instance": "100.76.30.90:9100"}, 0.0)
    assert s == "Service down: down (rpi1 · node)"
    rule2 = {**rule, "name": "Host memory", "_threshold": {"op": ">", "value": 50, "unit": "%", "type": "threshold"}}
    assert a.summary(rule2, {"instance": "100.124.110.23:9100"}, 77.834) == "Host memory: 77.8 % > 50 % (rog1)"
    m = ch.message("firing", {"summary": s, "group": {"instance": "100.76.30.90:9100"}, "link": "/x"},
                   {"name": "Service down", "sentence": "Alert when target up is below 1"})
    assert "Where: rpi1 (100.76.30.90:9100)" in m["text"]


def test_preview_series_carry_readable_titles_and_the_unit(al, names):
    e = al["engine"]
    r = _rule(e, PROM, query={"mode": "raw", "raw": "x", "label": "memory used (%)"},
              params={"op": ">", "value": 50}, group_by=["instance"])
    ad = FakeAdapter(series=[_line([60, 70, 80], group={"instance": "100.124.110.23:9100"})])
    p = e.preview(r, PROM, ad, "1h", T0 + 180)
    assert p["unit"] == "%"
    assert p["series"][0]["group_title"] == "rog1"
    assert p["series"][0]["group_display"] == {"instance": "rog1"}


def test_new_rule_sparkline_is_backfilled_from_24h(al):
    sch, e = al["scheduler"], al["engine"]
    r = _rule(e, PROM, params={"op": ">", "value": 50})
    pts = [[T0 - 86400 + i * 600, float(40 + i % 7)] for i in range(145)]
    ad = FakeAdapter(series=[{"group": {}, "points": pts}])
    spark = sch._backfill(r, PROM, ad, T0, below=False)
    assert 40 <= len(spark) <= 48
    assert all(isinstance(v, float) for _, v in spark)


def test_a_target_rule_says_down(al):
    e = al["engine"]
    r = _rule(e, PROM, query={"mode": "builder", "builder": {"metric": "up"}},
              params={"op": "<", "value": 1}, group_by=["job", "instance"], **{"for": "2m"})
    assert r["sentence"] == "Alert when a target is down, by job, instance, for 2 minutes"
