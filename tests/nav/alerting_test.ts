// 3.5 — Operate › Alerts: the pure part of the UI (frontend/lib/alertingModel.ts) — the plain
// sentence of a rule, durations, the wizard's checks and type/source switches, template filling,
// and the backtest chart's scales. Run by tests/test_nav_planes.py (node --test, type stripping).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  areaPath, defaultParams, durS, emptyRule, fmtDur, fmtValue, fromTemplate, groupText, hasQuery, linePath, linear,
  localFires, mergeIntervals, nearest, niceTicks, ruleSentence, sDur, since, sortAlerts, sortRules, suggestName,
  switchSource, switchType, timeTicks, toRuleIn, typesFor, validateRule, yDomain,
  type Alert, type AlertSource, type Rule, type RuleIn, type Template,
} from "../../frontend/lib/alertingModel.ts";

const PROM: AlertSource = { id: 1, name: "Prometheus", kind: "prometheus", builtin: true };
const LOKI: AlertSource = { id: 2, name: "Loki", kind: "loki", builtin: true };
const ES: AlertSource = { id: 3, name: "Logs", kind: "elasticsearch", options: { index: "logs-*" } };

const r5xx = (): RuleIn => ({
  ...emptyRule(PROM), name: "API 5xx",
  query: { mode: "builder", raw: "", builder: {
    metric: "http_requests_total", filters: [{ label: "status", op: "=~", value: "5.." }],
    agg: "rate", range: "5m", by: ["job"], ratio_of: { metric: "http_requests_total", filters: [] } } },
  type: "threshold", params: { op: ">", value: 2, reduce: "avg" }, for: "5m", group_by: ["job"], channels: [2],
});

test("durations: the contract's strings both ways, and in words", () => {
  assert.equal(durS("5m"), 300); assert.equal(durS("1h"), 3600); assert.equal(durS("7d"), 604800);
  assert.equal(durS("30s"), 30); assert.equal(durS(""), 0); assert.equal(durS("five"), 0);
  assert.equal(sDur(300), "5m"); assert.equal(sDur(3600), "1h"); assert.equal(sDur(90), "90s"); assert.equal(sDur(86400), "1d");
  assert.equal(fmtDur("5m"), "5 min"); assert.equal(fmtDur("1d"), "1 day"); assert.equal(fmtDur("7d"), "7 days");
  assert.equal(fmtDur("0s"), "0 s"); assert.equal(fmtDur(7200), "2 h");
});

test("numbers read well on an axis and in a sentence", () => {
  assert.equal(fmtValue(2, "%"), "2 %");
  assert.equal(fmtValue(0.0213), "0.0213");
  assert.equal(fmtValue(12345), "12.3k");
  assert.equal(fmtValue(2_500_000), "2.5M");
  assert.equal(fmtValue(null), "—");
  assert.equal(fmtValue(Number.NaN), "—");
});

test("the sentence says the rule in plain words — every type", () => {
  assert.equal(ruleSentence(r5xx(), "prometheus"),
    "Alert when the share of http_requests_total (status =~ 5..) in http_requests_total goes above 2 % for 5 min, per job.");
  const logs = (type: RuleIn["type"], params: RuleIn["params"]): RuleIn => ({
    ...emptyRule(LOKI), type, params, query: { mode: "builder", raw: "", builder: { text: "ERROR", filters: [{ label: "app", op: "=", value: "api" }] } },
  });
  assert.equal(ruleSentence(logs("frequency", { count: 50, window: "10m" }), "loki"),
    "Alert when at least 50 log lines containing « ERROR » (app = api) arrive within 10 min.");
  assert.equal(ruleSentence(logs("flatline", { count: 1, window: "10m" }), "loki"),
    "Alert when fewer than 1 log lines containing « ERROR » (app = api) arrive within 10 min — the source went quiet.");
  assert.match(ruleSentence(logs("spike", defaultParams("spike")), "loki"), /jumps ×3 above its level of the last 1 h \(ignored under 10\)/);
  assert.match(ruleSentence(logs("spike", { ...defaultParams("spike"), direction: "down" }), "loki"), /falls under 1\/3 of/);
  assert.match(ruleSentence(logs("any", {}), "loki"), /^Alert on every one of the log lines/);
  assert.match(ruleSentence(logs("new_term", { field: "error.type", lookback: "7d" }), "loki"), /error\.type .* not seen in the last 7 days/);
  assert.match(ruleSentence(logs("change", { field: "status", key_field: "host" }), "loki"), /status .* changes for the same host/);
  assert.match(ruleSentence(logs("cardinality", { field: "user", op: ">", value: 100, window: "1h" }), "loki"), /more than 100 distinct user within 1 h/);
  const up = { ...emptyRule(PROM), type: "absence" as const, params: { window: "5m" },
    query: { mode: "builder" as const, raw: "", builder: { metric: "up", filters: [{ label: "job", op: "=", value: "node" }], agg: "last" as const } } };
  assert.equal(ruleSentence(up, "prometheus"), "Alert when up (job = node) has no data for 5 min — the target is gone.");
  assert.match(ruleSentence({ ...up, type: "anomaly", params: { z: 3, lookback: "24h" }, for: "10m" }, "prometheus"),
    /more than 3 standard deviations from its level of the last 1 day for 10 min/);
  const raw: RuleIn = { ...emptyRule(PROM), query: { mode: "raw", raw: "sum(rate(x[5m]))", builder: {} }, params: { op: "<", value: 1, reduce: "last" }, for: "0s" };
  assert.equal(ruleSentence(raw, "prometheus"), "Alert when « sum(rate(x[5m])) » drops below 1.");
});

test("the wizard blocks only on what really misses, at the right step", () => {
  const e = emptyRule(PROM);
  const steps = validateRule(e, "prometheus").map((x) => x.step);
  assert.deepEqual([...new Set(steps)].sort(), [1, 4]); // no metric, no name (incident is on by default)
  const ok = r5xx();
  assert.deepEqual(validateRule(ok, "prometheus"), []);
  assert.ok(validateRule({ ...ok, channels: [], actions: { ...ok.actions, incident: false } }, "prometheus").some((x) => x.step === 3));
  assert.ok(validateRule({ ...ok, every: "5s" }, "prometheus").some((x) => /10 seconds/.test(x.msg)));
  assert.ok(validateRule({ ...ok, type: "spike", params: { ...defaultParams("spike"), ratio: 1 } }, "prometheus").some((x) => /above 1/.test(x.msg)));
  assert.ok(validateRule({ ...ok, type: "frequency", params: { count: 5, window: "soon" } }, "loki").some((x) => /duration/.test(x.msg)));
  const logs = { ...emptyRule(LOKI), name: "x" };
  assert.ok(validateRule(logs, "loki").some((x) => x.step === 1)); // nothing to look for yet
  assert.ok(hasQuery({ ...logs, query: { ...logs.query, builder: { text: "panic" } } }, "loki"));
});

test("switching type keeps what is shared; switching source family resets the builder", () => {
  const f = { ...emptyRule(LOKI), type: "frequency" as const, params: { count: 50, window: "15m" } };
  const s = switchType(f, "flatline");
  assert.deepEqual(s.params, { count: 50, window: "15m" });
  const sp = switchType(f, "spike");
  assert.equal(sp.params.window, "15m"); assert.equal(sp.params.ratio, 3);
  const toProm = switchSource({ ...f, query: { mode: "builder", raw: "", builder: { text: "ERR" } } }, PROM);
  assert.equal(toProm.source_id, 1);
  assert.equal(toProm.query.builder.metric, "");
  assert.ok(typesFor("prometheus").some((d) => d.id === toProm.type));
  const toEs = switchSource({ ...f, query: { mode: "builder", raw: "", builder: { text: "ERR" } } }, ES);
  assert.equal(toEs.query.builder.text, "ERR"); // logs → logs keeps the text
});

test("types offered depend on the source", () => {
  const prom = typesFor("prometheus").map((d) => d.id);
  assert.ok(prom.includes("threshold") && prom.includes("absence") && prom.includes("anomaly"));
  assert.ok(!prom.includes("frequency") && !prom.includes("new_term"));
  const logs = typesFor("elasticsearch").map((d) => d.id);
  assert.ok(logs.includes("frequency") && logs.includes("new_term") && logs.includes("threshold") && !logs.includes("absence"));
  assert.equal(typesFor(undefined).length, 10);
});

test("a template becomes a complete, editable rule on the right source", () => {
  const t: Template = {
    id: "http-5xx-rate", name: "Too many 5xx errors", category: "Web", description: "Share of 5xx",
    source_kind: "prometheus", available: true, source_id: 1,
    rule: { type: "threshold", params: { value: 2 }, query: { mode: "builder", raw: "", builder: { metric: "http_requests_total" } }, for: "5m" },
  };
  const r = fromTemplate(t, [LOKI, PROM]);
  assert.equal(r.name, "Too many 5xx errors");
  assert.equal(r.source_id, 1);
  assert.equal(r.params.op, ">"); assert.equal(r.params.value, 2);
  assert.equal(r.query.builder.metric, "http_requests_total");
  assert.equal(r.query.builder.range, "5m");
  assert.equal(r.quiet_hours.enabled, false);
  const noId = fromTemplate({ ...t, source_id: null }, [LOKI, PROM]);
  assert.equal(noId.source_id, 1); // found by kind
  assert.equal(suggestName(r), "http_requests_total — threshold");
});

test("a saved rule comes back editable without its server fields", () => {
  const saved: Rule = { ...r5xx(), id: 7, sentence: "x", state: { state: "firing" }, spark: [[1, 2]], query_compiled: "q" };
  const back = toRuleIn(saved) as Record<string, unknown>;
  for (const k of ["id", "sentence", "state", "spark", "query_compiled"]) assert.ok(!(k in back), k);
  assert.equal((back as unknown as RuleIn).params.value, 2);
});

test("scales: linear, nice ticks, a y domain that keeps the threshold in view", () => {
  const s = linear([0, 10], [0, 100]);
  assert.equal(s(5), 50); assert.equal(s.invert(25), 2.5);
  assert.equal(linear([3, 3], [0, 100])(3), 50);
  assert.deepEqual(niceTicks(0, 10, 4), [0, 2.5, 5, 7.5, 10]);
  assert.deepEqual(niceTicks(0, 0.9, 4), [0, 0.25, 0.5, 0.75, 1]);
  assert.deepEqual(niceTicks(5, 5, 4), [5, 5.25, 5.5, 5.75, 6]);
  const d = yDomain([[[0, 0.4], [60, 0.8]]], 2);
  assert.equal(d[0], 0); assert.ok(d[1] >= 2);
  assert.deepEqual(yDomain([], null), [0, 1]);
  const neg = yDomain([[[0, -3], [60, 4]]]);
  assert.ok(neg[0] <= -3 && neg[1] >= 4);
  const tt = timeTicks(0, 86400);
  assert.equal(tt[1] - tt[0], 4 * 3600);
  assert.equal(timeTicks(0, 7 * 86400)[1] - timeTicks(0, 7 * 86400)[0], 86400);
});

test("a series with holes draws with holes; its area closes on the base line", () => {
  const x = linear([0, 3], [0, 30]); const y = linear([0, 10], [100, 0]);
  assert.equal(linePath([[0, 1], [1, 2], [2, null], [3, 4]], x, y), "M0.0,90.0L10.0,80.0M30.0,60.0");
  const a = areaPath([[0, 1], [1, 2]], x, y);
  assert.ok(a.startsWith("M0.0,100.0L0.0,90.0L10.0,80.0L10.0,100.0Z"));
});

test("dragging the threshold re-computes the fires at once (for, per group)", () => {
  const pts: [number, number | null][] = [[0, 1], [60, 3], [120, 3], [180, 1], [240, 5], [300, null], [360, 5]];
  const one = localFires([{ group_key: "job=api", points: pts }], ">", 2);
  assert.deepEqual(one.map((i) => [i.start, i.end, i.peak]), [[60, 120, 3], [240, 240, 5], [360, 360, 5]]);
  const held = localFires([{ points: pts }], ">", 2, 60);
  assert.deepEqual(held.map((i) => [i.start, i.end]), [[120, 120]]);
  const below = localFires([{ points: pts }], "<", 2);
  assert.deepEqual(below.map((i) => [i.start, i.end, i.peak]), [[0, 0, 1], [180, 180, 1]]);
  const two = localFires([{ group_key: "a", points: [[0, 5], [60, 5]] }, { group_key: "b", points: [[30, 5], [90, 1]] }], ">", 2);
  assert.equal(two.length, 2);
  assert.deepEqual(mergeIntervals(two), [{ start: 0, end: 60, groups: 2 }]);
  assert.deepEqual(nearest(pts, 170), [180, 1]);
});

test("lists: what needs eyes first", () => {
  const mk = (id: number, name: string, st: Rule["state"], sev: Rule["severity"], enabled = true): Rule =>
    ({ ...r5xx(), id, name, severity: sev, enabled, state: st });
  const order = sortRules([
    mk(1, "b ok", { state: "ok" }, "critical"), mk(2, "a off", undefined, "critical", false),
    mk(3, "firing info", { state: "firing" }, "info"), mk(4, "firing crit", { state: "firing" }, "critical"),
    mk(5, "err", { state: "error" }, "warning"), mk(6, "pend", { state: "pending" }, "warning"),
  ]).map((r) => r.id);
  assert.deepEqual(order, [4, 3, 5, 6, 1, 2]);
  const al = (id: number, state: Alert["state"], severity: Alert["severity"], started_at: number): Alert =>
    ({ id, rule_id: 1, rule_name: "r", state, severity, started_at });
  assert.deepEqual(sortAlerts([al(1, "pending", "critical", 1), al(2, "firing", "info", 5), al(3, "firing", "critical", 9), al(4, "firing", "critical", 2)])
    .map((a) => a.id), [4, 3, 2, 1]);
  assert.equal(since(1000, 1000 + 125), "2 min");
  assert.equal(since(0, 100), "—");
  assert.equal(since(1000, 1000 + 3 * 3600 + 300), "3 h 5 min");
  assert.equal(groupText({ job: "api", host: "a" }), "job=api · host=a");
  assert.equal(groupText(null, "job=api"), "job=api");
});
