// 3.4.3 — the CortHeXis tab never crashes on a partial review / note payload (seen live on a
// project without its own review: `Cannot read properties of undefined (reading 'crit')`).
// Run by tests/test_nav_planes.py (node --test, type stripping).
import { test } from "node:test";
import assert from "node:assert/strict";
import { EMPTY_REPORT, safeNote, safeReport, type CxNote, type CxOverview } from "../../frontend/lib/corthexis.ts";

test("an empty or missing report gets every field the panels read", () => {
  for (const ov of [null, undefined, {}, { report: {} }, { report: null }, { report: { score: 7 } }]) {
    const r = safeReport(ov as CxOverview | null);
    assert.equal(r.counts.crit, 0);
    assert.deepEqual(r.findings, []);
    assert.deepEqual(r.skipped, []);
    assert.deepEqual(r.flags, {});
    assert.equal(r.at, null);
  }
  assert.equal(safeReport({ report: { score: 7 } } as unknown as CxOverview).score, 7);
  assert.equal(safeReport({ report: { score: "x" } } as unknown as CxOverview).score, null);
});

test("a real report passes through untouched", () => {
  const rep = { ...EMPTY_REPORT, at: "2026-10-08T20:00:00", score: 83, counts: { crit: 1, warn: 2, info: 3 },
    findings: [{ id: "x", severity: "crit", category: "chain", title: "t", detail: "d", notes: ["n"], remedy: "r",
      count: 1, items: [], action: null, judgement: false }], skipped: ["bench"], flags: { n: ["x"] } };
  const r = safeReport({ report: rep } as unknown as CxOverview);
  assert.deepEqual(r, rep);
});

test("a note without its lists renders as an empty one", () => {
  const n = safeNote({ id: "decision-x", body: "b" } as unknown as CxNote);
  assert.deepEqual([n.flags, n.in, n.out, n.warnings], [[], [], [], []]);
  assert.equal(n.body, "b");
});
