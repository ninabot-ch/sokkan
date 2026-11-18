// Simple profile — the pure part of a work's thread (components/simple/Thread.tsx): how the
// plain-language events of the session (backend/plain.py) become the items the person reads.
// No React here, so that tests/nav/simple_thread_test.ts runs it with node.
import type { AgentQuestion } from "@/lib/agent";
import type { Engine, PlainEvent, TechDetail } from "@/lib/simple/api";

/** 4.0 — every kind of approval card the backend sends (backend/approvalcard.py): each has its
 *  headline (`ap.<kind>`) and what « Allow » does (`ap.yes.<kind>`) in both dictionaries; an
 *  unknown kind falls back on `ap.other` and still shows its facts, never a blind « an action ». */
export const APPROVAL_KINDS = ["write_draft", "write_file", "save_memory", "run_command", "web_search", "web_read",
  "delegate", "board_card", "board_change", "create_agent", "agent_change", "mail_draft", "calendar_event",
  "shared_file", "tool"];

/** `denied` = the person said no (neutral, never orange); `skipped` = a rule of the space
 *  turned the call away and the agent went on otherwise (not shown in the thread) — 3.6.7 */
export interface Act { id: string; kind: string; subject: string; state: "live" | "done" | "fail" | "denied" | "skipped"; detail: TechDetail }
export type Approval = Extract<PlainEvent, { type: "approval" }>;
export type Item =
  | { k: "user"; text: string; docs: { id: string; name: string }[] }
  | { k: "text"; text: string; engine?: Engine; pending?: boolean }
  | { k: "acts"; acts: Act[] }
  | { k: "approval"; ev: Approval; decided?: "allow" | "deny" }
  | { k: "deliverable"; name: string; title: string; version: number; engine?: Engine }
  | { k: "notice"; code: string; detail: string; title?: string }
  | { k: "question"; id: string; questions: AgentQuestion[] };

export function reduce(items: Item[], e: PlainEvent): Item[] {
  switch (e.type) {
    case "user": return [...items, { k: "user", text: e.text, docs: e.documents || [] }];
    // `pending`: written while a decision is awaited (an engine whose approval ends its turn):
    // a discreet note, the answer comes after the decision
    case "text": {
      const item: Item = { k: "text", text: e.text, ...(e.engine ? { engine: e.engine } : {}), ...(e.pending ? { pending: true } : {}) };
      if (e.supersedes) {
        // 4.0: SOKKAN rewrote the delivery message from the saved file (the agent's was false):
        // it takes the place of the agent's last answer
        for (let i = items.length - 1; i >= 0 && items[i].k !== "user"; i--) {
          if (items[i].k === "text") return [...items.slice(0, i), item, ...items.slice(i + 1)];
        }
      }
      return [...items, item];
    }
    case "activity": {
      const act: Act = { id: e.id, kind: e.kind, subject: e.subject, state: "live", detail: e.detail };
      const last = items[items.length - 1];
      if (last?.k === "acts") return [...items.slice(0, -1), { k: "acts", acts: [...last.acts, act] }];
      return [...items, { k: "acts", acts: [act] }];
    }
    case "activity_done": {
      const state: Act["state"] = e.ok ? "done" : e.reason === "denied" ? "denied" : e.reason === "policy" ? "skipped" : "fail";
      // the person's « No » is already said by the line of the approval card: the step itself
      // then says nothing more (one sentence, not two)
      const said = state === "denied" && items.some((it) => it.k === "approval" && it.decided === "deny" && it.ev.tool_id === e.id);
      return items.map((it) => it.k !== "acts" ? it : {
        k: "acts", acts: it.acts.map((a) => a.id === e.id
          ? { ...a, state: said ? "skipped" : state, detail: { ...a.detail, result: e.detail?.result } } : a),
      });
    }
    case "approval": return [...items.filter((it) => !(it.k === "approval" && it.ev.id === e.id)), { k: "approval", ev: e }];
    // 3.6.7: a replayed thread knows who decided — the card becomes the line « Authorised /
    // Refused by you » instead of vanishing (before: only a card decided in this tab kept it)
    case "approval_resolved":
      if (e.decision) {
        const decision = e.decision;
        return items.map((it) => it.k === "approval" && it.ev.id === e.id && !it.decided ? { ...it, decided: decision } : it);
      }
      return items.filter((it) => !(it.k === "approval" && it.ev.id === e.id && !it.decided));
    // the turn is over: no step of it is still « in progress » (a step whose result never came
    // — interrupted, replayed — kept its spinner next to a finished card)
    case "status":
      if (e.state !== "idle") return items;
      return items.map((it) => it.k !== "acts" || !it.acts.some((a) => a.state === "live") ? it : {
        k: "acts", acts: it.acts.map((a) => a.state === "live" ? { ...a, state: "done" } : a),
      });
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
