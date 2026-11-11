"use client";
// Feature `study_mode` — client of /api/study/* (backend/study_api.py) and of the validations
// journal export (/api/hitl/journal). Server-side gated: everything but /api/study/me 404s
// when the feature is off; the UI asks `useStudy()` first.
import { useEffect, useState } from "react";

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

export interface StudyConsent {
  given: boolean;
  /** given AND for the current version of the text */
  current: boolean;
  at: number | null;
  version: string | null;
  withdrawn_at: number | null;
  content_scenarios: string[];
  study_id: string;
}

export interface StudyLimits {
  tokens_day: number | null; tokens_month: number | null; deep_per_user_day: number | null;
  /** personal ceiling per day (null = none) */
  user_tokens_day?: number | null;
}
export interface StudyUsed { tokens_day: number | null; tokens_month: number | null }

export interface StudyState {
  enabled: boolean;
  active: boolean;
  study_id: string;
  expires_at: string;
  expired: boolean;
  days_left: number | null;
  audit_required: boolean;
  /** the gateway announces the end of the free period (its notice window) */
  notice?: boolean;
  limits: StudyLimits;
  used: StudyUsed;
  /** gateway = read from the inference gateway; local = declared on the instance */
  source: "gateway" | "local" | "none";
  /** the gateway did not answer: last known state */
  stale: boolean;
}

export interface StudyMe extends Partial<StudyState> {
  enabled: boolean;
  active: boolean;
  scenarios?: string[];
  consent_version?: string;
  pseudonym?: string;
  consent?: StudyConsent | null;
  pending_feedback?: number;
  /** my own consumption today, as the gateway counts it under my pseudonym */
  mine?: { tokens_day: number | null; deep_day: number | null } | null;
  /** a study that requires the audit is active and I have not consented: no session */
  blocked?: boolean;
}

export interface StudySession {
  session_id: string;
  title: string;
  scenario: string;
  reference_minutes: number | null;
  tagged_at: number;
  closed_at: number | null;
  turns: number;
  useful: number | null;
  verify_minutes: number | null;
  feedback_at: number | null;
}

export interface StudyParticipant {
  pseudonym: string;
  email: string;
  consent: "none" | "given" | "withdrawn";
  consent_at: number | null;
  content_scenarios: string[];
}

export interface StudyToken {
  id: number; label: string; hint: string; study_id: string; created_at: number;
  expires_at: number | null; revoked_at: number | null; last_used_at: number | null; uses: number;
}

export interface StudyExportLog { id: number; at: number; by: string; study_id: string; mode: string; counts: Record<string, number> }

export interface StudyAdmin extends StudyState {
  scenarios: string[];
  participants: StudyParticipant[];
  tokens: StudyToken[];
  exports: StudyExportLog[];
  auth_mode: string;
  /** local sign-in: one identity for everybody (every volunteer would share a pseudonym) */
  single_identity: boolean;
}

export const studyMe = () => call<StudyMe>("/api/study/me");
export const studyConsent = (content_scenarios: string[]) =>
  call<StudyConsent>("/api/study/consent", "POST", { content_scenarios });
export const studySetContent = (content_scenarios: string[]) =>
  call<StudyConsent>("/api/study/consent/content", "PUT", { content_scenarios });
export const studyWithdraw = () => call<StudyConsent>("/api/study/consent", "DELETE");
export const studySessions = () => call<{ sessions: StudySession[]; scenarios: string[] }>("/api/study/sessions");
export const studyTag = (sid: string, body: { scenario?: string; reference_minutes?: number }) =>
  call<{ scenario: string; reference_minutes: number | null }>(`/api/sessions/${sid}/study`, "PUT", body);
export const studyFeedback = (sid: string, useful: number, verify_minutes: number) =>
  call<{ useful: number; verify_minutes: number }>(`/api/sessions/${sid}/study/feedback`, "POST", { useful, verify_minutes });
export const studyAdmin = () => call<StudyAdmin>("/api/admin/study");
export const studyRefresh = () => call<StudyState>("/api/admin/study/refresh", "POST", {});
export const studyTokenCreate = (label: string, days: number) =>
  call<{ id: number; token: string; label: string; expires_at: number | null }>("/api/admin/study/tokens", "POST", { label, days });
export const studyTokenRevoke = (id: number) => call<{ ok: boolean }>(`/api/admin/study/tokens/${id}`, "DELETE");

export const STUDY_EXPORT_URL = "/api/study/export";
export function journalUrl(p: { project?: string; actor?: string; since?: string; until?: string; format: "csv" | "json" }): string {
  const q = new URLSearchParams();
  let until = p.until;
  if (until && /^\d{4}-\d{2}-\d{2}$/.test(until)) {
    // the API's `until` is exclusive; a « to » date on screen includes that day
    const d = new Date(`${until}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + 1);
    until = d.toISOString().slice(0, 10);
  }
  for (const [k, v] of Object.entries({ ...p, until })) if (v) q.set(k, v);
  return `/api/hitl/journal?${q}`;
}

// ---- one shared state for the banner and the page (asked once a minute, like the API) ----
let cache: StudyMe | null = null;
let inflight: Promise<StudyMe> | null = null;
const listeners = new Set<(m: StudyMe) => void>();

export function refreshStudy(): Promise<StudyMe> {
  if (!inflight) {
    inflight = studyMe()
      .catch(() => ({ enabled: false, active: false }) as StudyMe)
      .then((m) => { cache = m; inflight = null; listeners.forEach((l) => l(m)); return m; });
  }
  return inflight;
}

/** null while loading; `enabled: false` when the feature is off (nothing to show). */
export function useStudy(): StudyMe | null {
  const [m, setM] = useState<StudyMe | null>(cache);
  useEffect(() => {
    listeners.add(setM);
    refreshStudy();
    const iv = setInterval(refreshStudy, 60000);
    return () => { listeners.delete(setM); clearInterval(iv); };
  }, []);
  return m;
}
