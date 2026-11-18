// 4.0 — job kits of SOKKAN Atelier (frontend/lib/simple/roles.ts): the home's groups of
// templates, Claude never offered for a job that keeps its data local, the picker's plan.
// Run by tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { groupTemplates, localOnly, setupPlan, toggle } from "../../frontend/lib/simple/roles.ts";
import type { Job, TaskTemplate } from "../../frontend/lib/simple/api.ts";

const job = (id: string, claude = true): Job => ({ id, label: id, description: "", project: id, level: "team", claude,
  guardrails: [], notes: [], templates_count: 1 });
const tpl = (id: string, role?: string, claude?: boolean): TaskTemplate => ({ id, label: id, description: "", hint: "",
  deliverable: "", tag: "docs", role, claude });

test("one group per job in the server's order, the general templates last", () => {
  const g = groupTemplates([tpl("hr.a", "hr"), tpl("sales.b", "sales"), tpl("hr.c", "hr"), tpl("training-plan")],
    [job("sales"), job("hr")]);
  assert.deepEqual(g.map((x) => x.role?.id ?? null), ["sales", "hr", null]);
  assert.deepEqual(g[1].templates.map((t) => t.id), ["hr.a", "hr.c"]);
  assert.deepEqual(groupTemplates([tpl("x")]).map((x) => x.role), [null]);
  assert.deepEqual(groupTemplates([], [job("hr")]), []);
});

test("Claude is never offered for a job that keeps its data on the machine", () => {
  assert.equal(localOnly(tpl("hr.a", "hr", false), "", []), true);
  assert.equal(localOnly(tpl("training-plan"), "hr", [job("hr", false)]), true);   // a project set up for HR
  assert.equal(localOnly(tpl("training-plan"), "sales", [job("sales")]), false);
  assert.equal(localOnly(null, "", []), false);
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
