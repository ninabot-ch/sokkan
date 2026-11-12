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
