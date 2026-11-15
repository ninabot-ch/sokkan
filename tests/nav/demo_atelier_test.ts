// 3.6.7 — the public demo of SOKKAN Atelier keeps the visitor's choices in THEIR browser
// (frontend/lib/simple/demo.ts): the demo has one identity, shared by every visitor. Run by
// tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { demoInterface, demoPrefs, saveDemoPrefs } from "../../frontend/lib/simple/demo.ts";
import type { Prefs } from "../../frontend/lib/simple/api.ts";

const store = new Map<string, string>();
const g = globalThis as unknown as Record<string, unknown>;
g.localStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => { store.set(k, String(v)); },
};
const at = (search: string) => { g.window = { location: { search } }; };
const SERVER: Prefs = { profile: "technical", chosen: true, lang: "en", theme: "light", available: true, locked: true, project: "demo-formation" };

test("without a choice, the instance's interface and the device's theme", () => {
  store.clear(); at("");
  assert.equal(demoInterface(), null);
  const p = demoPrefs(SERVER, null);
  // what another visitor stored on the shared identity is not this visitor's
  assert.deepEqual([p.profile, p.lang, p.theme, p.locked, p.project], ["technical", "auto", "auto", false, "demo-formation"]);
});

test("?ui= opens one interface and is remembered in this browser only", () => {
  store.clear(); at("?ui=atelier&project=demo-support");
  assert.equal(demoInterface(), "simple");
  at("");
  assert.equal(demoInterface(), "simple");                 // remembered
  at("?ui=cockpit");
  assert.equal(demoInterface(), "technical");
  at("?ui=nonsense");
  assert.equal(demoInterface(), "technical");              // an unknown value changes nothing
});

test("language and theme are the visitor's own", () => {
  store.clear(); at("");
  saveDemoPrefs({ lang: "en", theme: "dark" });
  const p = demoPrefs({ ...SERVER, lang: "fr", theme: "light" }, "simple");
  assert.deepEqual([p.profile, p.lang, p.theme], ["simple", "en", "dark"]);
  store.set("sokkan_demo_theme", "purple");
  assert.equal(demoPrefs(SERVER, "simple").theme, "auto");
});

test("blocked storage: the defaults, no exception", () => {
  g.localStorage = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  at("?ui=atelier");
  assert.equal(demoInterface(), "simple");
  saveDemoPrefs({ lang: "fr" });
  assert.equal(demoPrefs(SERVER, null).lang, "auto");
});
