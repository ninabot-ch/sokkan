// Simple profile — the pure part of a work's thread (components/simple/Thread.tsx): how the
// plain-language events of the session (backend/plain.py) become the items the person reads.
// No React here, so that tests/nav/simple_thread_test.ts runs it with node.
import type { AgentQuestion } from "@/lib/agent";
import type { Engine, PlainEvent, TechDetail } from "@/lib/simple/api";

export interface Act { id: string; kind: string; subject: string; state: "live" | "done" | "fail"; detail: TechDetail }
export type Approval = Extract<PlainEvent, { type: "approval" }>;
export type Item =
  | { k: "user"; text: string; docs: { id: string; name: string }[] }
  | { k: "text"; text: string; engine?: Engine }
  | { k: "acts"; acts: Act[] }
  | { k: "approval"; ev: Approval; decided?: "allow" | "deny" }
  | { k: "deliverable"; name: string; title: string; version: number; engine?: Engine }
  | { k: "notice"; code: string; detail: string; title?: string }
  | { k: "question"; id: string; questions: AgentQuestion[] };

export function reduce(items: Item[], e: PlainEvent): Item[] {
  switch (e.type) {
    case "user": return [...items, { k: "user", text: e.text, docs: e.documents || [] }];
    case "text": return [...items, { k: "text", text: e.text, ...(e.engine ? { engine: e.engine } : {}) }];
    case "activity": {
      const act: Act = { id: e.id, kind: e.kind, subject: e.subject, state: "live", detail: e.detail };
      const last = items[items.length - 1];
      if (last?.k === "acts") return [...items.slice(0, -1), { k: "acts", acts: [...last.acts, act] }];
      return [...items, { k: "acts", acts: [act] }];
    }
    case "activity_done":
      return items.map((it) => it.k !== "acts" ? it : {
        k: "acts", acts: it.acts.map((a) => a.id === e.id
          ? { ...a, state: e.ok ? "done" : "fail", detail: { ...a.detail, result: e.detail?.result } } : a),
      });
    case "approval": return [...items.filter((it) => !(it.k === "approval" && it.ev.id === e.id)), { k: "approval", ev: e }];
    case "approval_resolved": return items.filter((it) => !(it.k === "approval" && it.ev.id === e.id && !it.decided));
    // ONE card per document, where it was last saved, with that save's version: an agent that
    // saves the same plan five times in a turn (a section at a time) showed five cards
    // « version 2…5 » (3.6.4). A card for another document stays.
    case "deliverable": {
      const rest = items.filter((it) => !(it.k === "deliverable" && it.name === e.name));
      return [...rest, { k: "deliverable", name: e.name, title: e.title, version: e.version, ...(e.engine ? { engine: e.engine } : {}) }];
    }
    case "notice": return [...items, { k: "notice", code: e.code, detail: e.detail, ...(e.title ? { title: e.title } : {}) }];
    case "error": return [...items, { k: "notice", code: "engine", detail: e.message }];
    case "question": return [...items, { k: "question", id: e.id, questions: e.questions }];
    case "question_resolved": return items.filter((it) => !(it.k === "question" && it.id === e.id));
    default: return items;
  }
}
