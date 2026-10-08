// 3.2.2 — navigation by planes: deep links (legacy and new), visibility by role/feature,
// landing by role. Run by tests/test_nav_planes.py (node --test, type stripping).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  LEGACY, PLANES, PLANE_OF, href, landingPlane, pickTab, planeForKey, resolveTarget, visiblePlanes,
  type VisCtx,
} from "../../frontend/lib/planes.ts";

const q = (s: string) => new URLSearchParams(s);
const ALL = { preview: true, infra: true, observe: true, magnitude: true, agents: true, helm: true };
const ctx = (o: Partial<VisCtx> = {}): VisCtx => ({ f: ALL, project: "default", canDev: true, ops: true, steers: true, ...o });
const ids = (c: VisCtx) => Object.fromEntries(visiblePlanes(c).map((p) => [p.id, p.tabs.map((t) => t.id)]));

test("structure validated by Nick (08.10.2026)", () => {
  assert.deepEqual(PLANES.map((p) => [p.id, p.tabs.map((t) => t.id)]), [
    ["control", ["helm", "board", "corthexis"]],
    ["build", ["sessions", "crew", "preview"]],
    ["operate", ["operate", "infra", "costs", "journal"]],
    ["setup", ["organization", "engines", "magnitude", "secrets", "account", "notifications"]],
  ]);
});

test("every 3.2.1 tab deep link opens its new place", () => {
  const table: Record<string, [string, string]> = {
    board: ["control", "board"], sessions: ["build", "sessions"], crew: ["build", "crew"],
    helm: ["control", "helm"], preview: ["build", "preview"], corthexis: ["control", "corthexis"],
    costs: ["operate", "costs"], magnitude: ["setup", "magnitude"], infra: ["operate", "infra"],
    operate: ["operate", "operate"], journal: ["operate", "journal"],
  };
  for (const [tab, [plane, sub]] of Object.entries(table)) {
    for (const spelled of [tab, tab.toUpperCase(), tab[0].toUpperCase() + tab.slice(1)]) {
      const t = resolveTarget(q(`tab=${spelled}`));
      assert.deepEqual([t?.plane, t?.tab], [plane, sub], `?tab=${spelled}`);
    }
  }
  // the links the backend and the components write today, with their parameters
  assert.deepEqual(resolveTarget(q("tab=crew&agent=12&run=34")), { plane: "build", tab: "crew", section: undefined });
  assert.equal(resolveTarget(q("tab=operate&incident=7"))?.plane, "operate");
  assert.equal(resolveTarget(q("tab=corthexis&note=x"))?.tab, "corthexis");
  assert.equal(resolveTarget(q("tab=helm&card=3"))?.plane, "control");
  // every legacy entry lands in an existing plane/tab
  for (const [k, v] of Object.entries(LEGACY)) assert.ok(PLANE_OF[v.tab], k);
});

test("Profile sections and the GitLab return open Setup", () => {
  assert.deepEqual(resolveTarget(q("tab=keys")), { plane: "setup", tab: "engines", section: undefined });
  assert.deepEqual(resolveTarget(q("tab=model")), { plane: "setup", tab: "engines", section: undefined });
  assert.deepEqual(resolveTarget(q("tab=members")), { plane: "setup", tab: "organization", section: "members" });
  assert.deepEqual(resolveTarget(q("tab=features")), { plane: "setup", tab: "organization", section: "features" });
  assert.deepEqual(resolveTarget(q("forge=linked")), { plane: "setup", tab: "account", section: "linked" });
});

test("new deep links ?plane=&tab= and their round trip", () => {
  assert.deepEqual(resolveTarget(q("plane=build&tab=crew")), { plane: "build", tab: "crew", section: undefined });
  assert.deepEqual(resolveTarget(q("plane=operate")), { plane: "operate", tab: null, section: undefined });
  // a tab that lives elsewhere wins over a wrong plane
  assert.equal(resolveTarget(q("plane=setup&tab=crew"))?.plane, "build");
  assert.equal(resolveTarget(q("plane=nowhere")), null);
  assert.equal(resolveTarget(q("")), null);
  for (const p of PLANES) for (const t of p.tabs) {
    const back = resolveTarget(new URL(`http://x${href(t.id, { agent: "1" })}`).searchParams);
    assert.deepEqual([back?.plane, back?.tab], [p.id, t.id]);
  }
});

test("visibility by feature and role: a missing sub-tab disappears, an empty plane too", () => {
  assert.deepEqual(ids(ctx()).control, ["helm", "board", "corthexis"]);
  // Helm only for someone who steers, with the feature on
  assert.deepEqual(ids(ctx({ steers: false })).control, ["board", "corthexis"]);
  assert.deepEqual(ids(ctx({ f: { ...ALL, helm: false } })).control, ["board", "corthexis"]);
  // Preview only in the default project
  assert.deepEqual(ids(ctx({ project: "radio" })).build, ["sessions", "crew"]);
  // Crew: dev, or a viewer when the read-only Crew is on
  assert.deepEqual(ids(ctx({ canDev: false })).build, ["sessions", "preview"]);
  assert.deepEqual(ids(ctx({ canDev: false, f: { ...ALL, agents_viewer_readonly: true } })).build, ["sessions", "crew", "preview"]);
  // Operate and Infra: the ops team (or not known yet); Costs and Journal for everyone
  assert.deepEqual(ids(ctx({ ops: false })).operate, ["costs", "journal"]);
  assert.deepEqual(ids(ctx({ ops: undefined })).operate, ["operate", "infra", "costs", "journal"]);
  assert.deepEqual(ids(ctx({ f: { ...ALL, observe: false } })).operate, ["infra", "costs", "journal"]);
  // Magnitude off → not in Setup
  assert.ok(!ids(ctx({ f: { ...ALL, magnitude: false } })).setup.includes("magnitude"));
  // a plane with no visible sub-tab disappears (pure check on a reduced table)
  const reduced = visiblePlanes(ctx({ steers: false, f: { helm: false } }));
  assert.ok(reduced.every((p) => p.tabs.length > 0));
});

test("landing plane by role, always a visible plane", () => {
  const all = visiblePlanes(ctx());
  const base = { instanceAdmin: false, steers: false, ops: false, canDev: true, last: null, projectRole: "dev" };
  assert.equal(landingPlane(base, all), "build");                                   // dev
  assert.equal(landingPlane({ ...base, projectRole: "maintainer" }, all), "control"); // maintainer
  assert.equal(landingPlane({ ...base, steers: true }, all), "control");             // manager (Helm)
  assert.equal(landingPlane({ ...base, ops: true }, all), "operate");                // ops team
  assert.equal(landingPlane({ ...base, instanceAdmin: true, ops: true, last: "setup" }, all), "setup"); // admin → last
  assert.equal(landingPlane({ ...base, instanceAdmin: true, ops: true, last: null }, all), "control");
  assert.equal(landingPlane({ ...base, instanceAdmin: true, last: "bogus" }, all), "control");
  assert.equal(landingPlane({ ...base, canDev: false, projectRole: "viewer" }, all), "build");     // viewer
  // the role's plane is not there → the next one that is
  const noOps = visiblePlanes(ctx({ ops: false }));
  assert.equal(landingPlane({ ...base, ops: false, canDev: false, projectRole: "viewer" }, noOps), "build");
});

test("sub-tab pick and keyboard", () => {
  const all = visiblePlanes(ctx({ steers: false }));
  assert.equal(pickTab("control", null, all), "board");
  assert.equal(pickTab("control", "helm", all), "board");      // not visible → first
  assert.equal(pickTab("build", "crew", all), "crew");
  assert.equal(planeForKey("o", all), "operate");
  assert.equal(planeForKey("S", all), "setup");
  assert.equal(planeForKey("x", all), null);
});
