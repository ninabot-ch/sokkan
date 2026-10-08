// 3.3 Helm — API of the management view (backend/helm_api.py). Spec: docs/HELM.md.
import type { Card, CardDetail, CardEvent } from "./types";

export type HelmState = "todo" | "in_progress" | "waiting" | "blocked" | "done";

export interface Rollup {
  card_id: number;
  state: HelmState;
  label: string;
  progress: number;
  counts: Record<HelmState, number>;
  total: number;
  children: number;
  descendants: number;
  working: number;
  signals: {
    sessions: number; sessions_working: number; runs: number; runs_active: number; runs_failed: number;
    mrs: { card_id: number; url: string }[];
    incidents: { id: number; title: string; status: string; severity?: string; card_id: number }[];
  };
}

export interface Suggestion {
  id: number;
  project: string;
  card_id: number | null;
  target_id: number | null;
  kind: "drift" | "contradiction" | "scope" | "unparented" | "unassigned" | "slowing" | "racing" | "incident";
  title: string;
  detail: string;
  evidence: Record<string, unknown>;
  status: "open" | "approved" | "ignored" | "resolved";
  created_at: number;
}

export interface DeckItem {
  card: Card;
  rollup: Rollup;
  people: string[];
  suggestions: number;
  breathing: boolean;
}

export interface HelmDeck {
  projects: string[];
  items: DeckItem[];
  states: HelmState[];
  labels: Record<HelmState, string>;
  project_suggestions: Suggestion[];
}

export type KanbanCard = Card & { rollup: Rollup };

export interface HelmDetail extends CardDetail {
  rollup: Rollup;
  kanban: { buckets: string[]; cards: Record<string, KanbanCard[]> };
  suggestions: Suggestion[];
  context_note: string | null;
}

export interface HelmCosts {
  card_id: number;
  total_usd: number;
  sessions: { session_id: string; card_id: number; title: string; cost_usd: number }[];
  runs: { id: number; status: string; agent_id: number; cost_usd: number; card_id: number }[];
  note: string;
}

export interface ProjectProposal {
  project?: string;
  title: string;
  intent?: string;
  scope?: string;
  constraints?: string;
  deadline?: string;
  team?: string[];
  decisions?: string[];
  children?: { title: string; description?: string; assignee?: string; priority?: number }[];
}

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method, cache: "no-store",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

const qs = (o: Record<string, string>) => {
  const p = new URLSearchParams(Object.entries(o).filter(([, v]) => v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

/** steers = projects this person manages; reads (3.2.2 Captains demo) = projects whose Helm they
 *  may read; read_only = they read without steering (every action greyed). */
export const helmAccess = () => call<{ enabled: boolean; steers: string[]; reads?: string[]; read_only?: boolean }>("/api/helm/access");
export const helmDeck = (f: { project?: string; team?: string; person?: string } = {}) =>
  call<HelmDeck>(`/api/helm/deck${qs({ project: f.project || "", team: f.team || "", person: f.person || "" })}`);
export const helmFilters = () =>
  call<{ projects: { slug: string; name: string }[]; teams: string[]; people: string[] }>("/api/helm/filters");
export const helmCard = (id: number) => call<HelmDetail>(`/api/helm/cards/${id}`);
export const helmActivity = (id: number) => call<(CardEvent & { card_id: number; card_title: string })[]>(`/api/helm/cards/${id}/activity`);
export const helmCosts = (id: number) => call<HelmCosts>(`/api/helm/cards/${id}/costs`);
export const helmRefresh = (id: number) =>
  call<{ new: number; resolved: number; notes: string[]; suggestions: Suggestion[] }>(`/api/helm/cards/${id}/suggestions/refresh`, "POST");
export const helmBaseline = (id: number) => call<Card>(`/api/helm/cards/${id}/baseline`, "POST");
export const helmApprove = (sid: number) => call<Suggestion & { reframe_card: Card }>(`/api/helm/suggestions/${sid}/approve`, "POST");
export const helmIgnore = (sid: number) => call<Suggestion>(`/api/helm/suggestions/${sid}/ignore`, "POST");
export const helmCreateProject = (p: ProjectProposal) =>
  call<{ card: Card; children: Card[]; project: string; dropped_owners: { title: string; assignee: string }[] }>("/api/helm/projects", "POST", p);

/** 3.4 — where the person can create a project card (developer+), whether they steer it
 *  there (then it shows in their Helm), and who can own its cards. Steered projects first. */
export interface HelmTarget { slug: string; name: string; role: string; steers: boolean; people: { email: string; name: string }[] }
export const helmTargets = () => call<HelmTarget[]>("/api/helm/targets");

export interface HelmBrief { project: string; person: string; team: string; markdown: string; since: number; now: number }
export const helmBrief = (project: string, opts: { person?: string; team?: string; all?: boolean } = {}) =>
  call<HelmBrief>(`/api/helm/brief${qs({ project, person: opts.person || "", team: opts.team || "", all: opts.all ? "1" : "" })}`);

/** Opens Nina (the floating assistant) on the current tab and sends `message`. */
export const askNina = (message: string) =>
  window.dispatchEvent(new CustomEvent("sokkan:nina", { detail: { message } }));
export const agentTemplates = () =>
  call<{ id: string; label: string; description: string; params: Record<string, string> }[]>("/api/agent-templates");
export const agentTemplate = (id: string, person = "", team = "") =>
  call<{ id: string; fields: Record<string, unknown> }>(`/api/agent-templates/${id}${qs({ person, team })}`);

export const STATE_META: Record<HelmState, { label: string; short: string; icon: string; color: string; hint: string }> = {
  todo: { label: "To do", short: "To do", icon: "●", color: "var(--crew-idle)", hint: "nothing started under it yet" },
  in_progress: { label: "In progress", short: "In progress", icon: "▶", color: "var(--crew-running)", hint: "work is moving under it" },
  waiting: { label: "Waiting for approval", short: "Waiting", icon: "◐", color: "#eab308", hint: "something under it waits for a human (Review, an agent or a tool call to approve)" },
  blocked: { label: "Blocked", short: "Blocked", icon: "✕", color: "var(--crew-error)", hint: "an open incident or a failed agent run under it" },
  done: { label: "Done", short: "Done", icon: "✓", color: "var(--crew-armed)", hint: "every card under it is done" },
};

export const SUGGESTION_LABEL: Record<Suggestion["kind"], string> = {
  drift: "drifts from the goal",
  contradiction: "contradicts a decision",
  scope: "scope grows",
  unparented: "cards outside the project",
  unassigned: "no owner",
  slowing: "slowing down",
  racing: "racing ahead",
  incident: "incident linked",
};
