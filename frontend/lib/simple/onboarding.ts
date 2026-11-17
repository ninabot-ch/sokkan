// 4.0 — first launch (administrator), first sign-in (person), Team / Usage: the API client of
// backend/onboarding.py and the few pure rules the screens share (tested in
// tests/nav/onboarding_test.ts — no React here).

export const STEPS = ["org", "model", "projects", "people", "documents", "first_work"] as const;
export type StepId = typeof STEPS[number];
export type StepState = "todo" | "done" | "skipped";
export interface Step { id: StepId; state: StepState; at: number | null }
export interface SetupState { started_at: number | null; finished_at: number | null; finished: boolean; next: StepId | null;
  steps: Step[]; lang: string; preexisting: boolean; needed?: boolean }
export interface Counts { projects: number; people: number; documents: number; works: number; validated: number }
export type ModelKind = "local" | "sokkan" | "key" | "custom" | "none";
export interface ModelState { kind: ModelKind; configured: boolean; local_available: boolean; model: string;
  operator_managed: boolean; sokkan_url: string }
export interface Onboarding {
  admin: boolean; org_name: string; org_lang: string;
  me: { role_id: string; tour_done: boolean; welcomed: boolean; first_sid: string; needs_welcome: boolean };
  auth: { mode: string; codes: boolean; sso: boolean };
  setup?: SetupState; counts?: Counts; model?: ModelState;
  features?: { multi_project: boolean; documents: boolean; budgets: boolean };
  ttfv?: { started_at: number | null; first_validated_at: number | null; seconds: number | null };
}
export interface TestResult { ok: boolean; code: string; title: string; message: string; latency_ms: number | null; detail: string }
/** A job of the job kits (backend/roles.py, the one catalogue) as the onboarding shows it. */
export interface Role { id: string; label: string; description: string; sample: boolean; claude?: boolean; templates_count?: number }
export interface ProjectRow { slug: string; name: string; level: string; people: number }
export interface Person { email: string; name: string; instance_role: string; owner: boolean; admin: boolean;
  projects: { slug: string; name: string; role: string }[]; has_code: boolean; last_login: number | null;
  disabled: boolean; invite_pending: boolean; invite_expires_at: number | null; position: string }
export interface Added { email: string; projects: string[]; admin: boolean; url: string; code?: string; link?: string; expires_at?: number }
export interface Budget { currency: string; day: number; month: number; state: "ok" | "warn" | "stop" | "off";
  spent_day: number; spent_month: number; pct_day?: number; pct_month?: number; message: string }
export interface UsageRow { cost: number; api_equiv: number; turns: number; out_tokens: number; today: number }
export interface Usage { days: number; from: string; to: string; people: (UsageRow & { email: string; name: string })[];
  projects: (UsageRow & { slug: string; name: string; budget: Budget | null })[]; total: UsageRow;
  instance_budget: { budget_day_usd: number; budget_session_usd: number }; today_total: number; project_budgets: boolean;
  alerts: { project: string; name: string; state: "warn" | "stop"; pct_day?: number; pct_month?: number; currency?: string }[] }
export interface Validation { kind: string; decision: string; title: string; actor: string; owner: string; project: string;
  session_id: string; at: number; auto: boolean; edited: boolean; auto_reason?: string }

export class ObError extends Error {
  status: number;
  code: string;
  // no parameter properties: this file also runs under node's type stripping (tests/nav)
  constructor(status: number, code: string, message: string) { super(message); this.status = status; this.code = code; }
}
async function fail(r: Response): Promise<never> {
  let code = "", message = `${r.status}`;
  try {
    const d = (await r.json()).detail;
    if (d && typeof d === "object") { code = d.code || ""; message = d.message || message; }
    else if (typeof d === "string") { message = d; code = d; }
  } catch { /* not JSON */ }
  throw new ObError(r.status, code, message);
}
async function get<T>(url: string, project?: string): Promise<T> {
  const r = await fetch(url, { cache: "no-store", headers: project ? { "x-sokkan-project": project } : undefined });
  return r.ok ? r.json() : fail(r);
}
async function send<T>(url: string, method: string, body?: unknown, project?: string): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["content-type"] = "application/json";
  if (project) headers["x-sokkan-project"] = project;
  const r = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  return r.ok ? r.json() : fail(r);
}

export const fetchOnboarding = (lang: string) => get<Onboarding>(`/api/onboarding?lang=${lang}`);
export const markStep = (step: StepId, state: StepState) => send<SetupState>("/api/onboarding/setup", "PUT", { step, state });
export const finishSetup = (reopen = false) => send<SetupState>("/api/onboarding/setup", "PUT", { finish: !reopen, reopen });
export const saveOrg = (org_name: string, lang: string) => send<{ org_name: string; lang: string }>("/api/onboarding/org", "PUT", { org_name, lang });
export const testModel = (kind: ModelKind, key: string, base_url: string, model: string, lang: string) =>
  send<TestResult>("/api/onboarding/model/test", "POST", { kind, key, base_url, model, lang });
export const saveModel = (kind: ModelKind, key: string, base_url: string, model: string, lang: string) =>
  send<{ saved: boolean; test: TestResult; model?: ModelState }>("/api/onboarding/model", "POST", { kind, key, base_url, model, lang });
export const fetchSetupProjects = () => get<{ projects: ProjectRow[]; multi: boolean }>("/api/onboarding/projects");
export const createSetupProject = (name: string, level: string) =>
  send<{ slug: string; name: string; level: string }>("/api/onboarding/projects", "POST", { name, level });
export const fetchTeam = () => get<{ people: Person[]; auth: { mode: string; codes: boolean; sso: boolean };
  projects: { slug: string; name: string }[]; roles: string[] }>("/api/team");
export const addPerson = (p: { email: string; name: string; admin: boolean; projects: string[]; via: "code" | "link" }) =>
  send<Added>("/api/team", "POST", p);
export const patchPerson = (email: string, p: { admin?: boolean; projects?: Record<string, string>; name?: string }) =>
  send<{ person: Person | null }>(`/api/team/${encodeURIComponent(email)}`, "PATCH", p);
export const newCode = (email: string, via: "code" | "link") =>
  send<{ email: string; code?: string; link?: string; expires_at?: number }>(`/api/team/${encodeURIComponent(email)}/code?via=${via}`, "POST");
export const disablePerson = (email: string) => send<{ ok: boolean }>(`/api/team/${encodeURIComponent(email)}/disable`, "POST");
export const enablePerson = (email: string) => send<{ ok: boolean }>(`/api/team/${encodeURIComponent(email)}/enable`, "POST");
export const fetchUsage = (days: number) => get<Usage>(`/api/team/usage?days=${days}`);
export const saveBudget = (b: { project: string; day?: number; month?: number; currency?: string }) => send<Usage>("/api/team/budget", "PUT", b);
export const fetchRoles = (lang: string) => get<{ source: string; roles: Role[] }>(`/api/onboarding/roles?lang=${lang}`);
export const saveMe = (p: { role_id?: string; tour_done?: boolean; welcomed?: boolean; first_sid?: string }) =>
  send<Onboarding["me"]>("/api/onboarding/me", "PUT", p);
export const fetchSample = (role_id: string, lang: string, project?: string) =>
  send<{ ask: string; document: { id: string; name: string } | null }>("/api/onboarding/sample", "POST", { role_id, lang }, project);
export const fetchValidations = (q: { project?: string; actor?: string; since?: string; decision?: string }) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== "") s.set(k, String(v));
  return get<{ entries: Validation[]; count: number }>(`/api/team/validations?${s}`);
};
export const validationsCsvUrl = (q: { project?: string; actor?: string; since?: string; decision?: string }) => {
  const s = new URLSearchParams({ format: "csv" });
  for (const [k, v] of Object.entries(q)) if (v) s.set(k, v);
  return `/api/team/validations?${s}`;
};
/** A work started in a NAMED project (the wizard's first work may not be the one on screen). */
export function startIn(project: string, ask: string, documents: string[], lang: string, requestId: string) {
  return send<{ session_id: string; title: string }>("/api/simple/start", "POST",
    { ask, playbook: "", documents, lang, request_id: requestId }, project);
}
/** One file into a NAMED project's documents (raw body, like the Documents page). */
export async function uploadTo(project: string, file: File): Promise<{ id: string; name: string }> {
  const q = new URLSearchParams({ name: file.name, modified: String(file.lastModified || 0) });
  const r = await fetch(`/api/documents?${q}`, { method: "PUT", body: file,
    headers: { "content-type": "application/octet-stream", "x-sokkan-project": project } });
  return r.ok ? r.json() : fail(r);
}
/** Open a work of another project: the project is remembered, the page reloads on it. */
export function openWorkIn(project: string, sid: string): void {
  try { localStorage.setItem("sokkan_project", project); } catch { /* private mode */ }
  window.location.assign(`/?project=${encodeURIComponent(project)}&w=${sid}`);
}

// ---- pure rules -------------------------------------------------------------------------------
/** How many steps are settled (done or skipped) — the « 3 of 6 » of the drawer. */
export function progress(steps: Step[]): { settled: number; done: number; total: number } {
  return { settled: steps.filter((s) => s.state !== "todo").length, done: steps.filter((s) => s.state === "done").length,
    total: steps.length };
}
/** The step to show: the one asked, if it exists, else the first one still to do, else the last. */
export function currentStep(steps: Step[], asked?: string | null): StepId {
  if (asked && STEPS.includes(asked as StepId)) return asked as StepId;
  return (steps.find((s) => s.state === "todo")?.id) || STEPS[STEPS.length - 1];
}
/** The step after `id` that is still to do (wrapping to the first one left), null when all are settled. */
export function nextAfter(steps: Step[], id: StepId): StepId | null {
  const i = STEPS.indexOf(id);
  const order = [...STEPS.slice(i + 1), ...STEPS.slice(0, i)];
  return order.find((x) => steps.find((s) => s.id === x)?.state === "todo") ?? null;
}
export const LEVELS = ["public", "team", "project", "confidential", "restricted"] as const;
/** May a project at this level be written with Claude (the hard ceiling of claudetier: `project`)? */
export const claudePossible = (level: string, maxLevel = "team") =>
  LEVELS.indexOf(level as typeof LEVELS[number]) <= Math.min(LEVELS.indexOf(maxLevel as typeof LEVELS[number]), 2);
/** A plausible e-mail address (the server checks again). */
export const looksLikeEmail = (s: string) => /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(s.trim());
/** Minutes and seconds in words for the « first validated deliverable after … » line. */
export function duration(seconds: number | null | undefined, lang: "fr" | "en"): string {
  if (seconds === null || seconds === undefined || seconds < 0) return "";
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  if (lang === "fr") return m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
  return m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}
/** Money with 2 decimals, never a scientific notation; a value under a cent says so. */
export function money(v: number, cur = "USD"): string {
  if (!v) return `0.00 ${cur}`;
  if (v < 0.01) return `< 0.01 ${cur}`;
  return `${v.toFixed(2)} ${cur}`;
}
