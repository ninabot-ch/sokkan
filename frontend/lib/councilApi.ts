// Launch council — HTTP client (backend/council/api.py). Same error shape as the simple profile's.
import type { AgreementCard, Council, RoleInfo, Turn } from "./council";

export class CouncilApiError extends Error {
  constructor(public status: number, public code: string, message: string, public found?: Record<string, number>) { super(message); }
}

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, { method, cache: "no-store", headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined });
  if (r.ok) return r.json();
  let d: Record<string, unknown> = {};
  try { d = await r.json(); } catch { /* not JSON */ }
  throw new CouncilApiError(r.status, String(d.code ?? ""), String(d.detail ?? r.status), d.found as Record<string, number> | undefined);
}

export interface CouncilConfig {
  roles: RoleInfo[];
  defaults: { max_tokens: number; max_cost_chf: number };
  claude: { allowed: boolean; code: string; reason: string; model: string };
  limits: { parallel: number; max_refusals: number };
  project: { slug: string; level: string; label: string };
  documents: boolean;
}
export interface OpenBody {
  title: string; brief: string; lang: string; claude?: boolean; claude_scope?: "daf" | "all"; pii_ack?: boolean;
  max_tokens?: number; max_cost_chf?: number; documents?: { name: string; text: string }[]; doc_ids?: string[];
}
export const fetchConfig = (lang: string) => call<CouncilConfig>(`/api/council/config?lang=${lang}`);
export const listCouncils = () => call<{ project: string; councils: Council[] }>("/api/council");
export const openCouncil = (b: OpenBody) => call<Council>("/api/council", "POST", b);
export const fetchCouncil = (id: string, after: number, lang: string) =>
  call<{ council: Council; roles: RoleInfo[]; documents: { id: number; name: string; chars: number }[]; journal: Turn[] }>(
    `/api/council/${id}?after=${after}&lang=${lang}`);
export const addDocument = (id: string, d: { name: string; text: string } | { doc_id: string }) =>
  call<{ id: number; name: string }>(`/api/council/${id}/documents`, "POST", d);
/** After `stopped_budget`: raise the ceiling (owner or admin) — the council resumes where it stopped. */
export const raiseBudget = (id: string, b: { max_tokens?: number; max_cost_chf?: number }) => call<Council>(`/api/council/${id}/budget`, "POST", b);
export const resumeCouncil = (id: string) => call<Council>(`/api/council/${id}/resume`, "POST");
/** The project's classification (PUT /api/projects/{slug}/level — available without the Claude tier). */
export const setProjectLevel = (slug: string, level: string, reason: string) =>
  call<{ level: string; label: string }>(`/api/projects/${encodeURIComponent(slug)}/level`, "PUT", { level, reason });
export const startCouncil = (id: string) => call<Council>(`/api/council/${id}/start`, "POST");
export const say = (id: string, text: string) => call<Turn>(`/api/council/${id}/say`, "POST", { text });
export const stopCouncil = (id: string) => call<Council>(`/api/council/${id}/stop`, "POST");
export const acceptCard = (id: string) => call<Council>(`/api/council/${id}/accept`, "POST");
export const refuseCard = (id: string, reason: string) => call<Council>(`/api/council/${id}/refuse`, "POST", { reason });
export const exportUrl = (id: string, format: "json" | "md") => `/api/council/${id}/export?format=${format}`;
export type { AgreementCard };
