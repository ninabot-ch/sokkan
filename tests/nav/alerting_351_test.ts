// 3.5.1 — what the first day of Operate › Alerts on sokkan.ninabot.ch showed (10.10.2026): no channel
// ticked with 2 channels, label filters missing from the sentence (or printed as a raw address in
// brackets), « sending… » forever, a saved channel not in the list. Run by tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  defaultChannels, emptyRule, filtersPhrase, namesFrom, needsLogSource, ruleSentence, selectorFilters, testOutcome,
  upsertChannel, validateRule, type AlertSource, type RuleIn,
} from "../../frontend/lib/alertingModel.ts";

const PROM: AlertSource = { id: 1, name: "Prometheus", kind: "prometheus", builtin: true };

test("a new rule tells every channel of the project, not only when there is one", () => {
  const chs = [{ id: 0, enabled: true, builtin: true }, { id: 3, enabled: true }, { id: 4, enabled: true }, { id: 5, enabled: false }];
  assert.deepEqual(defaultChannels(chs), [3, 4]);
  assert.deepEqual(defaultChannels([{ id: 0, enabled: true, builtin: true }]), [0]);
  assert.deepEqual(defaultChannels([]), []);
});

test("a rule without channel is refused unless « no notification » is ticked", () => {
  const r: RuleIn = { ...emptyRule(PROM), name: "x", query: { mode: "raw", raw: "up", builder: {} }, type: "threshold",
    params: { op: "<", value: 1, reduce: "last", window: "0s" } };
  assert.ok(validateRule(r, "prometheus").some((p) => p.step === 3 && p.msg.includes("no notification")));
  assert.ok(!validateRule({ ...r, no_notification: true }, "prometheus").some((p) => p.step === 3));
  assert.ok(!validateRule({ ...r, channels: [3] }, "prometheus").some((p) => p.step === 3));
});

test("the sentence says the label filters, with host names, never a bracket", () => {
  assert.deepEqual(selectorFilters('up{host!="raspberrypi"}'), [{ label: "host", op: "!=", value: "raspberrypi" }]);
  const r: RuleIn = { ...emptyRule(PROM), query: { mode: "raw", raw: 'up{host!="raspberrypi"}', builder: {} }, type: "threshold",
    params: { op: "<", value: 1 }, for: "2m" };
  const s = ruleSentence(r, "prometheus");
  assert.ok(s.startsWith("Alert when a target is down") && s.includes("except host raspberrypi"), s);
  const names = namesFrom([{ group: { instance: "100.76.30.90:9100" }, group_display: { instance: "rpi1" }, points: [] }]);
  const b: RuleIn = { ...emptyRule(PROM), query: { mode: "builder", raw: "", builder: { metric: "node_load1", agg: "last",
    filters: [{ label: "instance", op: "=", value: "100.76.30.90:9100" }] } }, type: "threshold", params: { op: ">", value: 4 } };
  const sb = ruleSentence(b, "prometheus", "", names);
  assert.ok(sb.includes("on rpi1") && !sb.includes("100.76") && !sb.includes("(") && !sb.includes(")"), sb);
  assert.equal(filtersPhrase([{ label: "mountpoint", op: "=", value: "/" }, { label: "fstype", op: "!=", value: "tmpfs" }]), "on the / filesystem");
});

test("a channel test answers: sent, failed, or no answer in time", () => {
  assert.equal(testOutcome({ ok: true, detail: "ok" }).tone, "ok");
  const t = testOutcome({ ok: false, timed_out: true, detail: "no answer after 15 s" });
  assert.equal(t.tone, "warn"); assert.ok(t.text.includes("15 s"));
  assert.equal(testOutcome({ ok: false, detail: "http 403" }).text, "✕ http 403");
});

test("a saved channel goes into the list at once; a logs source is proposed when missing", () => {
  assert.deepEqual(upsertChannel([{ id: 1, n: "a" }], { id: 2, n: "b" }).map((c) => c.id), [1, 2]);
  assert.equal(upsertChannel([{ id: 1, n: "a" }], { id: 1, n: "z" })[0].n, "z");
  assert.equal(needsLogSource([{ kind: "prometheus" }, { kind: "sokkan" }]), true);
  assert.equal(needsLogSource([{ kind: "prometheus" }, { kind: "loki" }]), false);
});
