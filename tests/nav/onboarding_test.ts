// 4.0 — the pure rules of the first launch (frontend/lib/simple/onboarding.ts): which step shows,
// what « next » means once a step is settled, the progress of the drawer, the classification's
// Claude ceiling. Run by tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { claudePossible, currentStep, duration, looksLikeEmail, money, nextAfter, progress, STEPS, type Step }
  from "../../frontend/lib/simple/onboarding.ts";

const steps = (done: Record<string, "done" | "skipped">): Step[] =>
  STEPS.map((id) => ({ id, state: done[id] || "todo", at: null }));

test("a fresh instance starts at the organisation", () => {
  assert.equal(currentStep(steps({})), "org");
  assert.deepEqual(progress(steps({})), { settled: 0, done: 0, total: 6 });
});

test("resuming lands on the first step still to do, a skipped one counts as settled", () => {
  const s = steps({ org: "done", model: "skipped", projects: "done" });
  assert.equal(currentStep(s), "people");
  assert.deepEqual(progress(s), { settled: 3, done: 2, total: 6 });
  assert.equal(currentStep(s, "model"), "model");          // a step asked by name opens
  assert.equal(currentStep(s, "nonsense"), "people");
});

test("next after a step wraps to the first one left, null when everything is settled", () => {
  const s = steps({ org: "done", projects: "done", people: "done", documents: "done", first_work: "done" });
  assert.equal(nextAfter(s, "first_work"), "model");
  assert.equal(nextAfter(steps({ org: "done", model: "done", projects: "done", people: "done", documents: "done", first_work: "done" }), "documents"), null);
  assert.equal(nextAfter(steps({ org: "done" }), "org"), "model");
});

test("Claude is never possible above the project level, whatever the instance ceiling says", () => {
  assert.equal(claudePossible("public"), true);
  assert.equal(claudePossible("team"), true);
  assert.equal(claudePossible("project"), false);              // default ceiling: team
  assert.equal(claudePossible("project", "restricted"), true);  // raised, but the hard ceiling holds
  assert.equal(claudePossible("confidential", "restricted"), false);
  assert.equal(claudePossible("restricted", "restricted"), false);
});

test("small formats", () => {
  assert.equal(looksLikeEmail("ana@example.org"), true);
  assert.equal(looksLikeEmail("ana@example"), false);
  assert.equal(looksLikeEmail("ana example.org"), false);
  assert.equal(duration(754, "fr"), "12 min 34 s");
  assert.equal(duration(42, "en"), "42 s");
  assert.equal(duration(null, "fr"), "");
  assert.equal(money(0), "0.00 USD");
  assert.equal(money(0.004, "CHF"), "< 0.01 CHF");
  assert.equal(money(12.345), "12.35 USD");
});
