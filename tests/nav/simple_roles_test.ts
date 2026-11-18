// 4.0 — job kits of SOKKAN Atelier (frontend/lib/simple/roles.ts): the home's groups of
// templates and the picker's plan. (4.0, 18.11: no job refuses Claude any more — the project's
// classification does, server side, and personal data goes through the PII sandwich.)
// Run by tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { groupTemplates, setupPlan, toggle } from "../../frontend/lib/simple/roles.ts";
import type { Job, TaskTemplate } from "../../frontend/lib/simple/api.ts";

const job = (id: string): Job => ({ id, label: id, description: "", project: id, level: "team",
  guardrails: [], notes: [], templates_count: 1 });
const tpl = (id: string, role?: string): TaskTemplate => ({ id, label: id, description: "", hint: "",
  deliverable: "", tag: "docs", role });

test("one group per job in the server's order, the general templates last", () => {
  const g = groupTemplates([tpl("hr.a", "hr"), tpl("sales.b", "sales"), tpl("hr.c", "hr"), tpl("training-plan")],
    [job("sales"), job("hr")]);
  assert.deepEqual(g.map((x) => x.role?.id ?? null), ["sales", "hr", null]);
  assert.deepEqual(g[1].templates.map((t) => t.id), ["hr.a", "hr.c"]);
  assert.deepEqual(groupTemplates([tpl("x")]).map((x) => x.role), [null]);
  assert.deepEqual(groupTemplates([], [job("hr")]), []);
});

test("the picker toggles and plans one project per newly chosen job", () => {
  assert.deepEqual(toggle(["hr"], "sales"), ["hr", "sales"]);
  assert.deepEqual(toggle(["hr", "sales"], "hr"), ["sales"]);
  const base = { create: true, canCreate: true, here: false, canSetupHere: false, project: "default", already: ["hr"] };
  assert.deepEqual(setupPlan(["hr", "sales"], base), [{ rid: "sales", project: "" }]);
  assert.deepEqual(setupPlan(["sales"], { ...base, canCreate: false }), []);
  assert.deepEqual(setupPlan(["sales"], { ...base, create: false, here: true, canSetupHere: true }), [{ rid: "sales", project: "default" }]);
  assert.deepEqual(setupPlan(["sales", "hr"], { ...base, create: false, here: true, canSetupHere: true }), []);
});
