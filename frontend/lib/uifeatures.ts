"use client";
// 3.2 — client of three cockpit features: shared_review (/api/shares), byok_admin
// (/api/admin/model-keys), connect_ai (/api/connect-ai). Server-side gated: these calls
// 404 when the feature is off; the UI asks `useFeatureOn` first.
import { useFeatures } from "./features";

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    cache: "no-store",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail ?? detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return r.json();
}

/** Effective state of a registry feature (backend/features.py). */
export function useFeatureOn(id: string): boolean {
  const f = useFeatures();
  return !!f.registry?.items.find((i) => i.id === id)?.enabled;
}

// ---------------------------------------------------------------- shared_review
export type ShareKind = "session" | "preview";
export interface Share {
  id: number;
  project: string;
  kind: ShareKind;
  target: string;
  title: string;
  preview: { path?: string; env?: string } | null;
  principal_kind: "user" | "team";
  principal: string;
  access: "read" | "write";
  approver: boolean;
  note: string;
  created_by: string;
  created_at: number;
  expires_at: number | null;
  revoked_at: number | null;
  revoked_by: string;
  /** recipient view */
  effective_access?: "read" | "write";
  my_role?: string;
  pending?: { id: string; tool: string; title: string }[];
}
export interface SharePerson { kind: "user" | "team"; id: string; role: string; write_ok: boolean }
export interface ShareIn {
  kind: ShareKind; target: string; principal_kind: "user" | "team"; principal: string;
  access: "read" | "write"; expires_in_s?: number | null; approver?: boolean; note?: string;
  title?: string; path?: string; env?: string;
}
export const sharesInbox = () => call<Share[]>("/api/shares/inbox");
export const sharesOf = (kind: ShareKind, target: string) =>
  call<Share[]>(`/api/shares?${new URLSearchParams({ kind, target })}`);
export const sharePeople = (kind: ShareKind, target: string) =>
  call<SharePerson[]>(`/api/shares/people?${new URLSearchParams({ kind, target })}`);
export const shareCreate = (b: ShareIn) => call<Share>("/api/shares", "POST", b);
export const shareRevoke = (id: number) => call<Share>(`/api/shares/${id}`, "DELETE");
export const shareOpen = (id: number) => call<Share>(`/api/shares/${id}`);
export const shareApprover = (id: number, approver: boolean) =>
  call<Share>(`/api/shares/${id}/approver`, "POST", { approver });
export const shareDecide = (id: number, permission_id: string, decision: "allow" | "deny") =>
  call<{ ok: boolean }>(`/api/shares/${id}/decide`, "POST", { permission_id, decision });
export const shareShotUrl = (id: number) => `/api/shares/${id}/shot?_=${Date.now()}`;

// ---------------------------------------------------------------- byok_admin
export interface ModelKey {
  scope: string; provider: string; label: string; masked: string; set_by: string;
  set_at: number | null; base_url: string; tested_at: number | null; test_ok: boolean | null;
  test_detail: string; pushed_at: number | null; push_error: string;
}
export interface ModelKeysView {
  keys: ModelKey[];
  providers: { id: string; label: string; hint: string; sessions: boolean; testable: boolean }[];
  scopes: string[];
  per_project: boolean;
  gateway: { configured: boolean; client: string; url: string };
  sessions: { mode: string; key_ref: string | null; operator_managed: boolean };
}
export const modelKeys = () => call<ModelKeysView>("/api/admin/model-keys");
export const modelKeySet = (provider: string, key: string, opts: { test?: boolean; use_for_sessions?: boolean; base_url?: string } = {}) =>
  call<{ key: ModelKey; test?: { ok: boolean | null; detail: string }; sessions?: string; gateway: { pushed: boolean; detail: string } }>(
    `/api/admin/model-keys/${provider}`, "PUT", { key, scope: "instance", ...opts });
export const modelKeyDelete = (provider: string) =>
  call<{ ok: boolean; gateway: { pushed: boolean; detail: string } }>(`/api/admin/model-keys/${provider}?scope=instance`, "DELETE");
export const modelKeyTest = (provider: string) =>
  call<{ ok: boolean | null; detail: string }>(`/api/admin/model-keys/${provider}/test?scope=instance`, "POST");

// ---------------------------------------------------------------- connect_ai
export interface Engine {
  id: string; label: string; vendor: string; avatar: string; auths: ("key" | "login" | "none")[];
  bridge: "native" | "anthropic"; blurb: string; default_base_url: string; zone: string;
  allowed: boolean; connected: boolean; recommended: boolean; preselected: boolean;
  is_default: boolean; crew_value: string;
  connection: null | { auth: string; base_url: string; model: string; small_model: string;
    by: string; at: number | null; masked: string | null };
}
export interface EnginePolicy {
  allowed: string[]; zones: Record<string, string>; allowed_zones: string[]; tiers: string[];
  per_project: boolean;
}
export interface ConnectView {
  mode: "personal" | "governed";
  engines: Engine[];
  default: string | null;
  welcome_url: string;
  login_note: string;
  zones: string[];
  policy: EnginePolicy | null;
  can_admin: boolean;
  operator_managed: boolean;
  llm: { mode: string; configured: boolean };
  project: { slug: string | null; engine: string | null; can_choose: boolean };
}
export const connectView = () => call<ConnectView>("/api/connect-ai");
export const connectEngine = (id: string, b: { auth: string; key?: string; base_url?: string; model?: string; small_model?: string }) =>
  call<ConnectView>(`/api/connect-ai/engines/${id}`, "PUT", b);
export const disconnectEngine = (id: string) => call<ConnectView>(`/api/connect-ai/engines/${id}`, "DELETE");
export const connectDefault = (engine: string) => call<ConnectView>("/api/connect-ai/default", "POST", { engine });
export const connectPolicy = (p: EnginePolicy) => call<ConnectView>("/api/connect-ai/policy", "PUT", p);
export const connectProject = (slug: string, engine: string | null) =>
  call<{ ok: boolean }>(`/api/connect-ai/project/${slug}`, "PUT", { engine });
export const crewEngines = () => call<{ value: string; label: string; model: string }[]>("/api/connect-ai/crew-engines");

export const when = (ts: number | null | undefined) =>
  ts ? new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
