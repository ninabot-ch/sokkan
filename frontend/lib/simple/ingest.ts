// Review of the ingestion batches (feature `doc_ingest`, backend/docingest/api.py). The shapes
// below are the API's own answers — tests/test_pilot_integration.py reads the same fields.
import { ApiError } from "./api";

export interface BatchRow { id: number; source: string; project: string; pass_id: number | null;
  status: "open" | "pending" | "approved" | "rejected" | "partial"; created_at: number; closed_at: number | null;
  decided_at: number | null; counts?: Record<string, number> }
export interface BatchNote { batch_id: number; name: string; type: string; level: number; level_id: string; rel: string;
  title: string; description: string; flags: string[]; status: "pending" | "approved" | "rejected";
  decided_by: string; decided_at: number | null; reason: string; created_at: number; text?: string }
export interface BatchStats { batch: BatchRow; notes: number; hidden_above_clearance: number;
  by_status: Record<string, number>; by_type: Record<string, number>; by_level: Record<string, number>;
  flags: Record<string, number>; flagged: number; documents: number;
  pass: { id: number; kind: string; status: string; started_at: number; ended_at: number | null; seconds: number;
    counters: Record<string, number>; error: string } | null;
  abstained: { path: string; detail: string }[]; duplicates: { path: string; detail: string }[];
  errors: { path: string; detail: string }[]; viewed_by_you: number; review_min_sample: number }
export interface IngestStatus { enabled: boolean; model: { configured: boolean; model: string; not_ready: string | null };
  review_min_sample: number; pending_batches: number }

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, { method, cache: "no-store",
    headers: body ? { "content-type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
  if (r.ok) return r.json();
  let code = "", message = `${r.status}`;
  try {
    const d = (await r.json()).detail;
    if (d && typeof d === "object") { code = d.code || ""; message = d.message || message; } else if (typeof d === "string") message = d;
  } catch { /* not JSON */ }
  throw new ApiError(r.status, code, message);
}

const B = "/api/doc-ingest";
export const ingestStatus = () => call<IngestStatus>(`${B}/status`);
export const ingestBatches = () => call<{ batches: BatchRow[] }>(`${B}/batches`);
export const batchStats = (id: number) => call<BatchStats>(`${B}/batches/${id}`);
export const batchSample = (id: number, n = 10) => call<{ notes: BatchNote[]; viewed_by_you: number }>(`${B}/batches/${id}/sample?n=${n}`);
export const batchFlagged = (id: number) => call<{ notes: BatchNote[] }>(`${B}/batches/${id}/notes?flag=any&status=pending`);
export const batchNote = (id: number, name: string) => call<BatchNote>(`${B}/batches/${id}/notes/${encodeURIComponent(name)}`);
export const approveBatch = (id: number) =>
  call<{ ok: boolean; approved: number; left_pending: string[]; failed: string[] }>(`${B}/batches/${id}/approve`, "POST");
export const rejectBatch = (id: number, reason: string) => call<{ ok: boolean; rejected: number }>(`${B}/batches/${id}/reject`, "POST", { reason });
export const approveNote = (id: number, name: string) => call<{ ok: boolean }>(`${B}/batches/${id}/notes/${encodeURIComponent(name)}/approve`, "POST");
export const rejectNote = (id: number, name: string, reason: string) =>
  call<{ ok: boolean }>(`${B}/batches/${id}/notes/${encodeURIComponent(name)}/reject`, "POST", { reason });

/** The note as a person reads it: the body, without the front matter the memory keeps and
 *  without its first heading (the card already shows the title). */
export function noteBody(text: string): string {
  const m = /^---\n[\s\S]*?\n---\n?/.exec(text || "");
  return (m ? text.slice(m[0].length) : text || "").trim().replace(/^#\s[^\n]*\n+/, "");
}

/** Title and description of a note often say the same thing (the title is the description cut
 *  short): one line then, the complete one. */
export function noteHead(n: Pick<BatchNote, "title" | "description">): { title: string; more: string } {
  const cut = (n.title || "").replace(/…$/, "").trim();
  const d = (n.description || "").trim();
  if (!d || d === n.title) return { title: n.title, more: "" };
  if (cut && d.startsWith(cut)) return { title: d, more: "" };
  return { title: n.title, more: d };
}
