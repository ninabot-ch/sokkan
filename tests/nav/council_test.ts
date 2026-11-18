// Launch council room (frontend/lib/council.ts): what a tile shows, the budget meter, the journal
// grouped by round, and what the CEO can do in each state. Run by tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { ROLE_ORDER, actions, ceilingError, ceilingProposal, groupRounds, isLive, isOver, isTextFile, mergeTurns, meter, splitSections, step, tileStatus, type Turn } from "../../frontend/lib/council.ts";

const turn = (id: number, round: number, seq: number, role: string, kind: Turn["kind"] = "role"): Turn => ({
  id, round, seq, ts: id, kind, role, addressed_to: "", model: "m", route: "ship", tokens_in: 1, tokens_out: 1, ms: 1, cost_chf: 0, text: "x" });

test("nine roles, DAF first and the coach last", () => {
  assert.equal(ROLE_ORDER.length, 9);
  assert.equal(ROLE_ORDER[0], "daf");
  assert.equal(ROLE_ORDER[8], "coach");
});

test("a tile says who speaks, who has spoken, who waits — in words, not only by colour", () => {
  const c = { state: "round1" as const, speaking: ["code", "ops"] };
  const ts = [turn(1, 1, 1, "daf")];
  assert.equal(tileStatus("code", c, ts), "speaking");
  assert.equal(tileStatus("daf", c, ts), "spoken");
  assert.equal(tileStatus("qa", c, ts), "waiting");
  assert.equal(tileStatus("qa", { state: "brief", speaking: [] }, []), "idle");
  assert.equal(tileStatus("code", { state: "card", speaking: ["code"] }, []), "idle");   // not live: nobody speaks
  assert.equal(tileStatus("ceo", c, [turn(2, 1, 50, "ceo", "ceo")]), "waiting");
});

test("the budget meter follows the tighter ceiling and warns before stopping", () => {
  assert.deepEqual(meter({ max_tokens: 1000, max_cost_chf: 5, tokens: 100, cost_chf: 0.5, calls: 1 }),
    { pct: 10, tokensPct: 10, costPct: 10, level: "ok" });
  assert.equal(meter({ max_tokens: 1000, max_cost_chf: 5, tokens: 100, cost_chf: 4.2, calls: 1 }).level, "warn");
  const m = meter({ max_tokens: 1000, max_cost_chf: 5, tokens: 1200, cost_chf: 1, calls: 1 });
  assert.equal(m.pct, 100); assert.equal(m.level, "stop");
});

test("the journal is grouped by round, in reading order, and polls merge without duplicates", () => {
  const g = groupRounds([turn(3, 1, 2, "code"), turn(1, 1, 1, "daf"), turn(9, 2, 1, "code", "reply"), turn(5, 1, 50, "ceo", "ceo")]);
  assert.deepEqual(g.map((x) => [x.round, x.turns.map((t) => t.id)]), [[1, [1, 3, 5]], [2, [9]]]);
  assert.equal(mergeTurns([turn(1, 1, 1, "daf")], [turn(1, 1, 1, "daf"), turn(2, 1, 2, "code")]).length, 2);
});

test("what the CEO can do in each state", () => {
  assert.deepEqual(actions("brief"), { start: true, say: true, stop: true, decide: false, attach: true, exportable: true, resume: false, raise: false });
  assert.equal(actions("round2").say, true);
  assert.equal(actions("round2").decide, false);
  assert.equal(actions("card").decide, true);
  for (const s of ["accepted", "refused", "stopped", "stopped_budget", "failed"] as const) {
    assert.equal(actions(s).say, false); assert.equal(actions(s).decide, false); assert.equal(isOver(s), true);
  }
  assert.equal(isLive("round1"), true); assert.equal(isLive("card"), false);
  assert.equal(step("synthesis"), 3); assert.equal(step("stopped_budget"), -1);
});

test("an interrupted council can be resumed or stopped, and is not over", () => {
  const a = actions("interrupted");
  assert.equal(a.resume, true); assert.equal(a.stop, true); assert.equal(a.say, true); assert.equal(a.decide, false);
  assert.equal(isLive("interrupted"), false); assert.equal(isOver("interrupted"), false);
  assert.equal(actions("round2").resume, false);
  assert.equal(step("interrupted"), -1);
});

test("the finance lead's synthesis is a summary in words and folded sections, never one line", () => {
  const text = "Synthèse v1 : budget CHF 721'000.00, 3 risque(s).\n\n## Budget\n\n| Poste | Montant |\n|---|---:|\n| A | 1.00 |\n\n## Risques\n\n- (haute) Délai — achats";
  const s = splitSections(text);
  assert.match(s.lead, /^Synthèse v1/);
  assert.deepEqual(s.sections.map((x) => x.title), ["Budget", "Risques"]);
  assert.match(s.sections[0].body, /\| A \| 1\.00 \|/);          // the table is whole
  assert.deepEqual(splitSections("juste du texte"), { lead: "juste du texte", sections: [] });
});

test("a file can be read in the browser when it is text, and only then", () => {
  assert.equal(isTextFile({ name: "notes.MD" }), true);
  assert.equal(isTextFile({ name: "budget.csv" }), true);
  assert.equal(isTextFile({ name: "x.bin", type: "text/plain" }), true);
  assert.equal(isTextFile({ name: "plan.pdf", type: "application/pdf" }), false);
});

test("after stopped_budget the CEO can raise the ceiling: only there, with a proposal above the consumption, and a lower ceiling is refused", () => {
  for (const s of ["brief", "round1", "round2", "synthesis", "card", "interrupted", "accepted", "refused", "stopped", "failed"] as const) assert.equal(actions(s).raise, false);
  assert.equal(actions("stopped_budget").raise, true);
  assert.equal(actions("stopped_budget").resume, false);          // not the interrupted-round resume: it is the raise that resumes
  const b = { max_tokens: 5000, max_cost_chf: 1, tokens: 5100, cost_chf: 0.4, calls: 6 };
  const p = ceilingProposal(b);
  assert.deepEqual(p, { max_tokens: 10000, max_cost_chf: 2 });
  assert.equal(ceilingError(b, p.max_tokens, p.max_cost_chf), null);
  assert.equal(ceilingError(b, 5100, 2), "tokens");               // equal to the consumption: it would stop again at once
  assert.equal(ceilingError(b, 9000, 0.3), "chf");
  assert.ok(ceilingProposal({ ...b, tokens: 9000 }).max_tokens >= 13500);
});
