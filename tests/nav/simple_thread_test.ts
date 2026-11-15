// 3.6.4 — the thread of a work in the simple profile keeps ONE card per document: an agent that
// saved the same plan five times in a turn (a section at a time) showed five cards « version
// 2…5 ». Run by tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { reduce, type Item } from "../../frontend/lib/simple/threadModel.ts";
import type { PlainEvent } from "../../frontend/lib/simple/api.ts";

const run = (evs: PlainEvent[]) => evs.reduce<Item[]>((items, e) => reduce(items, e), []);
const save = (id: string, name: string, version: number): PlainEvent[] => [
  { type: "activity", id, kind: "write_draft", subject: "Plan de cours", detail: {} },
  { type: "activity_done", id, ok: true, detail: {} },
  { type: "deliverable", name, title: "Plan de cours", version },
];

test("five saves of one document are one card, at the last save, with its version", () => {
  const items = run([
    { type: "user", text: "Prépare le plan de cours.", documents: [] },
    ...[1, 2, 3, 4, 5].flatMap((v) => save(`t${v}`, "Plan de cours.md", v)),
    { type: "text", text: "Le plan est prêt." },
  ]);
  const cards = items.filter((it) => it.k === "deliverable");
  assert.deepEqual(cards, [{ k: "deliverable", name: "Plan de cours.md", title: "Plan de cours", version: 5 }]);
  assert.equal(items[items.length - 2].k, "deliverable");        // right before the closing sentence
  assert.equal(items[items.length - 1].k, "text");
  // what the agent did is still told, save by save
  assert.equal(items.filter((it) => it.k === "acts").flatMap((it) => it.k === "acts" ? it.acts : []).length, 5);
});

test("a card per document when a work saves two", () => {
  const items = run([...save("a", "Plan.md", 1), ...save("b", "Quiz.md", 1), ...save("c", "Plan.md", 2)]);
  assert.deepEqual(items.filter((it) => it.k === "deliverable").map((it) => it.k === "deliverable" && [it.name, it.version]),
    [["Quiz.md", 1], ["Plan.md", 2]]);
});

// ---- 3.6.7 « Icebreaker » finishes -------------------------------------------------------
const acts = (items: Item[]) => items.flatMap((it) => it.k === "acts" ? it.acts : []);

test("the person's « No » is not a failure: the card says it once, the step says nothing more", () => {
  const items = run([
    { type: "activity", id: "t1", kind: "write_draft", subject: "Quiz", detail: {} },
    { type: "approval", id: "p1", kind: "write_draft", title: "Quiz", version: 1, sources: [], attached: 0,
      stays_inside: true, draft: null, detail: {}, tool_id: "t1" },
    { type: "approval_resolved", id: "p1", decision: "deny" },
    { type: "activity_done", id: "t1", ok: false, detail: {}, reason: "denied" },
  ]);
  const card = items.find((it) => it.k === "approval");
  assert.equal(card && card.k === "approval" && card.decided, "deny");          // « Refused by you — … »
  assert.deepEqual(acts(items).map((a) => a.state), ["skipped"]);              // no orange line
});

test("a refusal replayed without its card is « refused by you », never « did not succeed »", () => {
  const items = run([
    { type: "activity", id: "t1", kind: "write_draft", subject: "Quiz", detail: {} },
    { type: "activity_done", id: "t1", ok: false, detail: {}, reason: "denied" },
  ]);
  assert.deepEqual(acts(items).map((a) => a.state), ["denied"]);
});

test("a call a rule of the space turned away (a refused mkdir) is skipped, a real failure stays one", () => {
  const items = run([
    { type: "activity", id: "t1", kind: "run_command", subject: "", detail: {} },
    { type: "activity_done", id: "t1", ok: false, detail: {}, reason: "policy" },
    { type: "activity", id: "t2", kind: "read_file", subject: "x", detail: {} },
    { type: "activity_done", id: "t2", ok: false, detail: {} },
  ]);
  assert.deepEqual(acts(items).map((a) => a.state), ["skipped", "fail"]);
});

test("a replayed decision keeps the line « Authorised / Refused by you » instead of dropping the card", () => {
  const items = run([
    { type: "approval", id: "p1", kind: "write_draft", title: "Quiz", version: 1, sources: [], attached: 0,
      stays_inside: true, draft: null, detail: {} },
    { type: "approval_resolved", id: "p1", decision: "allow" },
  ]);
  const card = items.find((it) => it.k === "approval");
  assert.equal(card && card.k === "approval" && card.decided, "allow");
  // an older server (no decision): the card goes, as before
  assert.equal(run([
    { type: "approval", id: "p1", kind: "write_draft", title: "Quiz", version: 1, sources: [], attached: 0,
      stays_inside: true, draft: null, detail: {} },
    { type: "approval_resolved", id: "p1" },
  ]).length, 0);
});

test("the end of a turn settles every step still « in progress » (no spinner next to a finished card)", () => {
  const items = run([
    { type: "activity", id: "t1", kind: "write_draft", subject: "Quiz", detail: {} },
    { type: "deliverable", name: "Quiz.md", title: "Quiz", version: 1 },
    { type: "status", state: "idle" },
  ]);
  assert.deepEqual(acts(items).map((a) => a.state), ["done"]);
  assert.deepEqual(run([{ type: "activity", id: "t1", kind: "x", subject: "", detail: {} }, { type: "status", state: "working" }])
    .flatMap((it) => it.k === "acts" ? it.acts.map((a) => a.state) : []), ["live"]);
});
