/** CortHeXis tab — API client and types (backend/corthexis.py). */

export type Severity = "crit" | "warn" | "info";

export interface CxNode {
  id: string; file: string; type: string; desc: string; words: number; priority: boolean;
  modified: string | null; age: number | null; date_source: string;
  in: number; out: number; flags: string[]; level: Severity | null;
  sx: number | null; sy: number | null; indexed: boolean; chunks: number;
}
export interface CxGhost { id: string; label: string; ghost: true; refs: number }
export interface CxGraph {
  version: string; at: string | null; source: string; error: string | null; unchanged?: boolean;
  stats: { notes: number; words: number; links: number; broken: number; chunks: number | null;
           types: Record<string, number> };
  nodes: CxNode[]; ghosts: CxGhost[]; edges: { s: string; t: string; broken?: boolean }[];
}
export interface CxItem { note?: string; [k: string]: unknown }
export interface CxFinding {
  id: string; severity: Severity; category: string; title: string; detail: string;
  notes: string[]; remedy: string; count: number; items: CxItem[];
  action: "relink" | "merge" | "rename" | "close" | null; judgement: boolean;
}
export interface CxReport {
  at: string; duration_ms: number; score: number; signature: string; notes_total: number;
  counts: Record<Severity, number>; source: string | null; skipped: string[];
  findings: CxFinding[]; flags: Record<string, string[]>;
}
export interface CxOverview {
  report: CxReport; history: { at: string; score: number; crit: number; warn: number; info: number }[];
  summary: { open?: number; open_over_7d?: number; fixed?: number; mean_fix_hours?: number | null };
  pending: number; running: boolean; notify: boolean; digest_at: string | null;
}
export interface CxNoteFlag {
  id: string; severity: Severity; category: string; title: string; remedy: string;
  action: CxFinding["action"]; judgement: boolean; items: CxItem[];
}
export interface CxNote {
  id: string; file: string; type: string; desc: string; words: number; priority: boolean;
  modified: string | null; age: number | null; date_source: string; chunks: number | null;
  indexed: boolean; warnings: string[]; body: string; in: string[];
  out: { target: string; resolved: string | null }[]; flags: CxNoteFlag[];
  classification?: string;   // 3.4 level id (public … restricted)
}
export interface CxChange { path: string; before: string | null; after: string | null; diff: string }
export interface CxProposal {
  id: string; kind: string; title: string; summary: string; changes: CxChange[]; files: number;
  params: Record<string, string>; status: "pending" | "applied" | "refused" | "conflict" | "failed";
  created_by: string; created_at: number; decided_by?: string; decided_at?: number; error?: string;
}
export interface CxProposalIn {
  kind: "relink" | "merge" | "rename" | "close"; note?: string; target?: string;
  new_target?: string; keep?: string; drop?: string; new_name?: string; reason?: string;
}

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method, cache: "no-store",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

const B = "/api/corthexis";
export const cxGraph = (since = "") => call<CxGraph>(`${B}/graph${since ? `?since=${since}` : ""}`);
export const cxNote = (name: string) => call<CxNote>(`${B}/note/${encodeURIComponent(name)}`);
export const cxReview = () => call<CxOverview>(`${B}/review`);
export const cxRunReview = () => call<CxOverview>(`${B}/review/run`, "POST");
export const cxProposals = () => call<CxProposal[]>(`${B}/proposals`);
export const cxPropose = (p: CxProposalIn) => call<CxProposal>(`${B}/proposals`, "POST", p);
export const cxDecide = (id: string, approve: boolean) =>
  call<CxProposal>(`${B}/proposals/${id}/${approve ? "approve" : "refuse"}`, "POST");
export const cxCuration = (finding_ids: string[], notes: string[] = []) =>
  call<{ session_id: string }>(`${B}/curation`, "POST", { finding_ids, notes });
