// 4.0 — the organisation's profile (backend/orgprofile_api.py) and « remember for next time »
// after a validation (backend/learn.py, routes in simple_api.py). Apart from api.ts so that the
// lots working on the Atelier at the same time do not edit the same lines.
import { ApiError } from "./api";

export interface OrgPerson { name: string; function: string; initials?: string; email?: string; team?: string }
export interface OrgProfile {
  name: string; legal_name: string; legal_form: string; street: string; postcode: string; city: string;
  country: string; uid: string; vat: string; iban: string; bank: string; phone: string; email: string;
  website: string; lang: string; address_form: "vous" | "tu" | ""; formats: string; tone: string; sector: string;
  signatories: OrgPerson[]; people: OrgPerson[]; logo: boolean; can_edit?: boolean; iban_masked?: boolean; updated_at?: number | null;
  completeness?: { filled: string[]; missing: string[]; ready: boolean };
}
export interface LearnFact { id: string; kind: "correction" | "template" | "structure"; why: string; text: string }
export interface LearnOffer { state: "offer" | "none" | "retained" | "dismissed" | "not_validated"; facts: LearnFact[]; notes?: string[] }

async function call<T>(url: string, method = "GET", body?: unknown, raw?: BodyInit): Promise<T> {
  const r = await fetch(url, {
    method, cache: "no-store",
    headers: raw ? { "content-type": "application/octet-stream" } : body ? { "content-type": "application/json" } : undefined,
    body: raw ?? (body ? JSON.stringify(body) : undefined),
  });
  if (r.ok) return r.json();
  let code = "", message = `${r.status}`;
  try {
    const d = (await r.json()).detail;
    if (d && typeof d === "object") { code = d.code || ""; message = d.message || message; }
    else if (typeof d === "string") message = d;
  } catch { /* not JSON */ }
  throw new ApiError(r.status, code, message);
}

export const fetchOrgProfile = () => call<OrgProfile>("/api/org/profile");
export const fetchOrgStatus = () => call<{ filled: string[]; missing: string[]; ready: boolean }>("/api/org/profile/status");
export const saveOrgProfile = (p: Partial<OrgProfile>) => call<OrgProfile>("/api/org/profile", "PUT", p);
export const fetchOrgPreview = (ask = "") => call<{ text: string; chars: number; tokens_est: number }>(
  `/api/org/profile/preview?ask=${encodeURIComponent(ask)}`);
export const uploadOrgLogo = (f: File) => call<{ ok: boolean }>(`/api/org/logo?name=${encodeURIComponent(f.name)}`, "PUT", undefined, f);
export const dropOrgLogo = () => call<{ ok: boolean }>("/api/org/logo", "DELETE");

const learnUrl = (sid: string, name: string) =>
  `/api/simple/sessions/${encodeURIComponent(sid)}/deliverables/${encodeURIComponent(name)}/learn`;
export const fetchLearn = (sid: string, name: string, lang: string) => call<LearnOffer>(`${learnUrl(sid, name)}?lang=${lang}`);
export const retainLearn = (sid: string, name: string, facts: { text: string; kind: string }[], lang: string) =>
  call<LearnOffer>(learnUrl(sid, name), "POST", { facts, lang });
export const dismissLearn = (sid: string, name: string) => call<LearnOffer>(learnUrl(sid, name), "POST", { dismiss: true });
