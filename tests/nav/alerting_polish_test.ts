// 3.5 polish — what a person reads in Operate › Alerts: host names, values with their unit, « down »,
// raw PromQL said in words (never shown in the sentence). Run by tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  alertValueText, describeRaw, emptyRule, fmtValue, groupTitle, ruleSentence, ruleValueText, subjectOf, valueUnit,
  type AlertSource, type Rule, type RuleIn,
} from "../../frontend/lib/alertingModel.ts";

const PROM: AlertSource = { id: 1, name: "Prometheus", kind: "prometheus", builtin: true };
const MEM = "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100";

test("raw PromQL is said in words, with its unit", () => {
  assert.deepEqual(describeRaw(MEM), ["memory used", "%"]);
  assert.deepEqual(describeRaw('100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100'), ["CPU used", "%"]);
  assert.deepEqual(describeRaw("rate(node_network_receive_bytes_total[5m])"), ["node network receive bytes", "/s"]);
  assert.equal(describeRaw("up == 0")[0], "target up");
  assert.deepEqual(describeRaw("x"), ["", ""]);
});

test("the plain sentence of a raw rule never quotes the query", () => {
  const r: RuleIn = { ...emptyRule(PROM), query: { mode: "raw", raw: MEM, builder: {} }, type: "threshold",
    params: { op: ">", value: 85, reduce: "last" } };
  const unit = valueUnit(r.query);
  assert.equal(unit, "%");
  const s = ruleSentence(r, "prometheus", unit);
  assert.ok(!s.includes("node_memory"), s);
  assert.ok(s.includes("memory used") && s.includes("85 %"), s);
  // a template label « memory used (%) » does not say « % » twice
  assert.equal(subjectOf({ mode: "raw", raw: "x", builder: {}, label: "memory used (%)" }, "prometheus"), "memory used");
  // the server's unit wins
  assert.equal(valueUnit({ mode: "raw", raw: "x", builder: {} }, "bytes"), "bytes");
});

test("values with their unit", () => {
  assert.equal(fmtValue(77.834, "%"), "77.8 %");
  assert.equal(fmtValue(1.5 * 2 ** 30, "bytes"), "1.5 GB");
  assert.equal(fmtValue(0.35, "s"), "350 ms");
  assert.equal(fmtValue(300, "s"), "5 min");
  assert.equal(fmtValue(4.2, "/s"), "4.2/s");
  assert.equal(fmtValue(12, "events"), "12");
});

test("an alert's value line: the server's words, else « down » / value vs threshold", () => {
  assert.deepEqual(alertValueText({ value_text: "77.8 % > 50 %" }), { value: "77.8 %", rest: "> 50 %" });
  assert.deepEqual(alertValueText({ value_text: "down" }), { value: "down", rest: "" });
  assert.deepEqual(alertValueText({ value_text: "12 events in 5 minutes (≥ 10)" }), { value: "12 events", rest: "in 5 minutes (≥ 10)" });
  assert.deepEqual(alertValueText({ value: 0, threshold: { op: "<", value: 1 } }), { value: "down", rest: "" });
  assert.deepEqual(alertValueText({ value: 77.8, threshold: { op: ">", value: 50 }, unit: "%" }), { value: "77.8 %", rest: "> 50 %" });
});

test("a rule row's value: « down » for a target rule, the unit otherwise", () => {
  const base = { type: "threshold", params: { op: "<", value: 1 } } as unknown as Rule;
  assert.equal(ruleValueText({ ...base, state: { state: "firing", last_value: 0, firing: 1 } }, null), "down");
  assert.equal(ruleValueText({ ...base, state: { state: "firing", last_value: 0, firing: 3 } }, null), "3 down");
  assert.equal(ruleValueText({ type: "threshold", params: { op: ">", value: 85 }, state: { state: "ok", last_value: 80.21 } } as unknown as Rule, "%"), "80.2 %");
});

test("groups are named by host, the address stays for the tooltip", () => {
  assert.equal(groupTitle({ group_title: "rpi1 · node", group_key: "instance=100.76.30.90:9100,job=node" }), "rpi1 · node");
  assert.equal(groupTitle({ group_key: "job=api" }), "api");
});

test("a rule just saved says « first check », not « OK »", async () => {
  const { ruleState } = await import("../../frontend/lib/alertingModel.ts");
  const r = { enabled: true, state: { state: "ok" } } as unknown as Rule;
  assert.equal(ruleState(r), "new");
  assert.equal(ruleState({ ...r, state: { state: "ok", last_eval: 1 } } as unknown as Rule), "ok");
  assert.equal(ruleState({ ...r, state: { state: "firing" } } as unknown as Rule), "firing");
});

test("the next templates to suggest: what every ops team wants first, not the catalogue order", async () => {
  const { suggestTemplates } = await import("../../frontend/lib/alertingModel.ts");
  const ts = [
    { id: "http-5xx-rate", name: "Too many 5xx errors", available: true },
    { id: "host-cpu", name: "Host CPU busy", available: true },
    { id: "host-memory", name: "Host memory almost full", available: true },
    { id: "target-down", name: "Service down", available: true },
    { id: "log-errors", name: "Errors in the logs", available: false },
  ];
  assert.deepEqual(suggestTemplates(ts, ["Service down"]).map((t) => t.id), ["host-memory", "http-5xx-rate", "host-cpu"]);
});
