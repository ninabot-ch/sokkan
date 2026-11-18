// Launch council (feature `council`, experimental) — the pure part of the room: the nine roles in
// the order they speak, what a tile shows, the budget meter, the journal grouped by round, and
// which actions the CEO has in which state. No React, no import: the room (components/council)
// and tests/nav/council_test.ts both use it.

export const ROLE_ORDER = ["daf", "code", "ops", "conception", "achats", "qa", "marcom", "juridique", "coach"] as const;
export type RoleId = (typeof ROLE_ORDER)[number];

export type CouncilState =
  | "brief" | "round1" | "round2" | "synthesis" | "card" | "interrupted" | "accepted" | "refused"
  | "stopped" | "stopped_budget" | "failed";

export const LIVE: CouncilState[] = ["round1", "round2", "synthesis"];
export const FINAL: CouncilState[] = ["accepted", "refused", "stopped", "stopped_budget", "failed"];

export interface RoleInfo {
  id: RoleId; label: string; short: string; description: string; leads: boolean; tier: string;
  avatar: { initials: string; icon: string; hue: number };
}
export interface Turn {
  id: number; round: number; seq: number; ts: number; kind: "role" | "reply" | "ceo" | "disagreements" | "synthesis" | "system";
  role: string; addressed_to: string; model: string; route: string; tokens_in: number; tokens_out: number; ms: number;
  cost_chf: number; text: string;
}
export interface Budget { max_tokens: number; max_cost_chf: number; tokens: number; cost_chf: number; calls: number }
export interface Council {
  id: string; title: string; brief: string; lang: string; state: CouncilState; project: string; level: number;
  claude: boolean; claude_model: string; budget: Budget; speaking: string[]; refusals: number;
  stop_message: string; stop_reason: string; resume_step?: string; card: AgreementCard | null; positions: Position[];
  accepted: { cards: { id: number; title: string }[]; agents: { id: number; name: string; status: string }[]; skipped: { nom: string; error: string }[] } | null;
}
export interface Position { profil: string; pourcentage_poste: number; perimetre: string; status: string }
export interface Finding { kind: string; severity: string; text: string }
export interface AgreementCard {
  kind: "council_agreement"; subject: string; where: string; facts: { label: string; value: string }[];
  preview: string; after: string; findings: Finding[]; version: number;
  synthesis: {
    perimetre: { resume: string; inclus: string[]; exclus: string[] };
    budget: { lignes: { poste: string; montant_chf: number; source: string }[]; total_chf: number };
    risques: { risque: string; gravite: string; responsable: string }[];
    kanban: { titre: string; responsable: string; critere_de_fin: string }[];
    agents_a_recruter: { nom: string; profil: string; perimetre: string; outils: string[]; palier: string }[];
    roles_humains: { profil: string; pourcentage_poste: number; perimetre: string }[];
    calendrier: { jalon: string; echeance: string; responsable: string }[];
  };
}

export type TileStatus = "speaking" | "spoken" | "waiting" | "idle";

export const isLive = (s: CouncilState) => LIVE.includes(s);
export const isOver = (s: CouncilState) => FINAL.includes(s);

/** What a role's tile shows. « speaks » is the server's own list of who is in a call right now
 *  (at most two in round 1); `spoken` = it has a turn; a live council's other roles wait. */
export function tileStatus(role: string, c: Pick<Council, "state" | "speaking">, turns: Pick<Turn, "role" | "kind">[]): TileStatus {
  if (isLive(c.state) && c.speaking.includes(role)) return "speaking";
  if (turns.some((t) => t.role === role && (t.kind === "role" || t.kind === "reply" || t.kind === "synthesis"))) return "spoken";
  return isLive(c.state) ? "waiting" : "idle";
}

export type MeterLevel = "ok" | "warn" | "stop";
/** The budget meter: the tighter of the two ceilings (tokens, cost) drives it. */
export function meter(b: Budget): { pct: number; tokensPct: number; costPct: number; level: MeterLevel } {
  const tokensPct = b.max_tokens > 0 ? Math.min(100, Math.round((b.tokens / b.max_tokens) * 100)) : 0;
  const costPct = b.max_cost_chf > 0 ? Math.min(100, Math.round((b.cost_chf / b.max_cost_chf) * 100)) : 0;
  const pct = Math.max(tokensPct, costPct);
  return { pct, tokensPct, costPct, level: pct >= 100 ? "stop" : pct >= 80 ? "warn" : "ok" };
}

/** The journal grouped by round, in reading order (the server already sorts; this keeps the
 *  grouping stable when turns arrive in several polls). */
export function groupRounds(turns: Turn[]): { round: number; turns: Turn[] }[] {
  const by = new Map<number, Turn[]>();
  for (const t of turns) by.set(t.round, [...(by.get(t.round) ?? []), t]);
  return [...by.entries()].sort((a, b) => a[0] - b[0]).map(([round, ts]) => ({
    round, turns: ts.sort((a, b) => a.seq - b.seq || a.id - b.id),
  }));
}

/** Merge a poll's new turns into what is shown (by id). */
export function mergeTurns(have: Turn[], incoming: Turn[]): Turn[] {
  const seen = new Set(have.map((t) => t.id));
  return [...have, ...incoming.filter((t) => !seen.has(t.id))];
}

export interface Actions { start: boolean; say: boolean; stop: boolean; decide: boolean; attach: boolean; exportable: boolean; resume: boolean }
/** What the CEO can do in each state. Deciding needs a card; the CEO may write until the end.
 *  `interrupted` (the service restarted during a call): resume the round, or stop. */
export function actions(s: CouncilState): Actions {
  return {
    start: s === "brief", attach: s === "brief",
    say: s === "brief" || isLive(s) || s === "card" || s === "interrupted",
    stop: isLive(s) || s === "brief" || s === "card" || s === "interrupted",
    decide: s === "card", exportable: true, resume: s === "interrupted",
  };
}

/** A long turn (the finance lead's synthesis) cut into its lead (the summary, in words) and its
 *  `## ` sections, which the room folds. A text without a section has an empty list. */
export function splitSections(text: string): { lead: string; sections: { title: string; body: string }[] } {
  const parts = text.split(/^## +/m);
  const lead = (parts.shift() ?? "").trim();
  return { lead, sections: parts.map((p) => {
    const i = p.indexOf("\n");
    return { title: (i < 0 ? p : p.slice(0, i)).trim(), body: (i < 0 ? "" : p.slice(i + 1)).trim() };
  }).filter((x) => x.title) };
}

/** What the room can read as text without the server's Documents feature (no upload route). */
export const TEXT_FILE = /\.(md|markdown|txt|csv|tsv|json|yml|yaml)$/i;
export const isTextFile = (f: { name: string; type?: string }) => TEXT_FILE.test(f.name) || (f.type ?? "").startsWith("text/");

/** The state as a step of the progress line (0..4; -1 = ended before the card). */
export function step(s: CouncilState): number {
  return ({ brief: 0, round1: 1, round2: 2, synthesis: 3, card: 4, accepted: 4, refused: 4 } as Record<string, number>)[s] ?? -1;
}

export const fmtTokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)} k` : String(n));
export const fmtChf = (n: number) => `CHF ${n.toFixed(2)}`;
