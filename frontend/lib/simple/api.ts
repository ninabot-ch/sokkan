// Simple profile — API client (backend/simple_api.py, backend/documents_api.py) and the
// plain-language event types of the session WebSocket (`?view=simple`, backend/plain.py).
import type { AgentQuestion } from "@/lib/agent";

export interface Prefs { profile: "simple" | "technical"; chosen: boolean; lang: "auto" | "fr" | "en";
  theme: "light" | "dark" | "auto"; available: boolean; locked: boolean;
  /** 3.6.7 — the project this person last worked in ('' = none yet) */
  project?: string;
  /** 4.0 — the person's jobs (« Postes »); null = not chosen yet, [] = « later » */
  roles?: string[] | null }
export interface Limits { max_bytes: number; extensions: string[]; max_per_project: number }
export interface TaskTemplate { id: string; label: string; description: string; hint: string; deliverable: string; tag: string;
  /** 4.0 job kits (backend/roles.py): what to attach, what the agent produces, what a person checks */
  attach?: string; produces?: string; checks?: string[]; format?: "docx" | "xlsx" | "md"; kind?: string;
  role?: string; role_label?: string; claude?: boolean }
/** 4.0 — a job of SOKKAN Atelier (« Poste ») */
export interface Job { id: string; label: string; description: string; project: string; level: string; claude: boolean;
  guardrails: string[]; notes: { name: string; title: string; description: string }[]; templates_count: number;
  templates?: TaskTemplate[] }
export interface RolesCatalog { roles: Job[]; mine: string[] | null; project: string; project_role: string;
  can_create_project: boolean; can_setup_here: boolean }
export type WorkState = "waiting" | "working" | "idle";
/** 3.6.5 — the model that answered (the model of the API's response, never the one asked for) */
export interface Engine { model: string; label: string; where: "local" | "anthropic" | "other"; place: string }
/** May this project's work be written with Claude (backend/claudetier.gate) */
export interface ClaudeGate { allowed: boolean; code: string; reason: string; project_name?: string; level: string; level_label: string;
  max_level: string; max_label: string; models: { id: string; label: string }[]; default_model: string; destination: string }
export interface ClaudeSettings { key: { masked: string; test_ok: boolean | null; test_detail: string } | null;
  models: { id: string; label: string; available: boolean }[]; default_model: string; appliance: boolean; router: boolean;
  test?: { ok: boolean | null; detail: string } }
export interface Recent { sid: string; title: string; state: WorkState; last_line: string; created_at: number;
  updated_at: number; deliverables: number; validated: boolean; to_validate: number; items?: Deliverable[] }
export interface HomeData { first_name: string; project: string; project_name?: string; can_start: boolean; playbooks: TaskTemplate[];
  recents: Recent[]; works_total: number; usage: { pct: number | null; basis: string }; nina: boolean;
  limits: Limits | null; prefs: Prefs; claude?: ClaudeGate | null;
  /** 4.0 job kits: the person's jobs (the project's own first), the project's job, offer the choice */
  roles?: Job[]; project_role?: string; ask_roles?: boolean }
export interface Doc { id: string; name: string; ext: string; size: number; owner: string; level: number;
  created_at: number; modified_at: number; text_state: string; text_chars: number; uses: number }
export interface Deliverable { name: string; title: string; ext: string; size: number; updated_at: number; version: number;
  previewable: boolean; validated: boolean; validated_by: string; validated_at: number | null;
  changed_since_validation: boolean; text?: string; model?: string; models?: string[]; engine?: Engine | null }
export interface Work { sid: string; title: string; project: string; state: WorkState; can_drive: boolean;
  documents: Doc[]; deliverables: Deliverable[];
  engine?: { tier: "claude" | "local"; model: string; label: string; destination: string } }

export interface Source { id: string; name: string; ext: string; modified_at: number }
export interface TechDetail { tool?: string; title?: string; input?: unknown; result?: string }
export type PlainEvent =
  | { type: "status"; state: "idle" | "working" }
  | { type: "user"; text: string; documents: { id: string; name: string }[] }
  | { type: "text"; text: string; engine?: Engine; pending?: boolean }
  | { type: "activity"; id: string; kind: string; subject: string; detail: TechDetail }
  /** 3.6.7 — `reason` tells a refusal from a failure: « denied » = the person said no,
   *  « policy » = a rule of the space turned the call away (the agent goes on otherwise) */
  | { type: "activity_done"; id: string; ok: boolean; detail: TechDetail; reason?: "denied" | "policy" }
  | { type: "approval"; id: string; kind: string; title: string; version: number; sources: Source[]; attached: number;
      stays_inside: boolean; draft: { text: string; partial: boolean; truncated: boolean } | null; detail: TechDetail;
      tool_id?: string }
  | { type: "approval_resolved"; id: string; decision?: "allow" | "deny" }
  | { type: "deliverable"; name: string; title: string; version: number; engine?: Engine }
  | { type: "notice"; code: string; detail: string; title?: string }
  | { type: "error"; message: string }
  | { type: "question"; id: string; questions: AgentQuestion[] }
  | { type: "question_resolved"; id: string };

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

async function fail(r: Response): Promise<never> {
  let code = "", message = `${r.status}`;
  try {
    const d = (await r.json()).detail;
    if (d && typeof d === "object") { code = d.code || ""; message = d.message || message; }
    else if (typeof d === "string") message = d;
  } catch { /* not JSON */ }
  throw new ApiError(r.status, code, message);
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url, { cache: "no-store" });
  return r.ok ? r.json() : fail(r);
}
async function send<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method, headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined });
  return r.ok ? r.json() : fail(r);
}

export const fetchPrefs = () => get<Prefs>("/api/me/prefs");
export const savePrefs = (p: Partial<Pick<Prefs, "profile" | "lang" | "theme" | "project" | "roles">>) => send<Prefs>("/api/me/prefs", "PUT", p);
export const fetchRoles = (lang: string) => get<RolesCatalog>(`/api/simple/roles?lang=${lang}`);
/** a project for a job: "" = a new one (instance admin), else the job in that project (its admin) */
export const setupRole = (rid: string, project: string, lang: string) =>
  send<{ project: string; name: string; created: boolean; notes: string[]; level: string }>(
    `/api/simple/roles/${encodeURIComponent(rid)}/setup`, "POST", { project, lang });
export const fetchRolePeople = () => get<{ people: { email: string; name: string; roles: string[] | null }[] }>("/api/simple/roles/people");
export const saveRolesOf = (email: string, roles: string[]) =>
  send<Prefs>(`/api/admin/prefs/${encodeURIComponent(email)}`, "PUT", { roles });
export const fetchHome = (lang: string) => get<HomeData>(`/api/simple/home?lang=${lang}`);
export const fetchWorks = () => get<{ works: Recent[] }>("/api/simple/works");
/** One id per click on « Start »: the server opens ONE work per id, so the same id can be
 *  sent again when the answer was lost on the way (network change, proxy timeout). */
export function newRequestId(): string {
  const b = new Uint8Array(16);
  try { crypto.getRandomValues(b); } catch { for (let i = 0; i < b.length; i++) b[i] = Math.floor(Math.random() * 256); }
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
const pause = (ms: number) => new Promise((r) => setTimeout(r, ms));
/** Start a work. A failure that says nothing about the work itself — no answer at all, or
 *  a 5xx from the way — is asked again with the SAME id before anything is shown: the work
 *  may well have started. A refusal (4xx) is final and thrown as it is. */
async function retrying<T>(call: () => Promise<T>): Promise<T> {
  let last: unknown;
  for (const wait of [0, 1500, 4000]) {
    if (wait) await pause(wait);
    try {
      return await call();
    } catch (e) {
      if (e instanceof ApiError && e.status < 500) throw e;
      last = e;
    }
  }
  throw last;
}
export function startWork(ask: string, playbook: string, documents: string[], lang: string, requestId: string,
  claude?: { model: string }) {
  return retrying(() => send<{ session_id: string; title: string }>("/api/simple/start", "POST",
    { ask, playbook, documents, lang, request_id: requestId,
      ...(claude ? { tier: "claude", claude_model: claude.model } : {}) }));
}
export const fetchClaudeSettings = () => get<ClaudeSettings>("/api/claude/settings");
export const saveClaudeKey = (key: string) => send<ClaudeSettings>("/api/claude/key", "PUT", { key });
export interface ProjectRow { slug: string; name: string; role: string; shared: boolean }
export const fetchMyProjects = () => get<{ projects: ProjectRow[]; multi: boolean; default: string }>("/api/projects");
/** every account of the instance once — IAM users and local accounts (personal access code) */
export const fetchPeople = () => get<{ people: { email: string; name: string; local: boolean }[] }>("/api/claude/people");
export const createProject = (slug: string, name: string) =>
  send<{ slug: string; name: string }>("/api/admin/projects", "POST", { slug, name, access_source: "sso_group" });
export const grantProject = (slug: string, email: string, role: string) =>
  send<unknown>(`/api/admin/projects/${encodeURIComponent(slug)}/grants`, "POST", { principal_kind: "user", principal: email, role });
export const fetchClaudeGate = (lang: string) => get<ClaudeGate>(`/api/claude/gate?lang=${lang}`);
export const saveProjectLevel = (slug: string, level: string, reason: string) =>
  send<{ level: string; label: string; claude: ClaudeGate }>(`/api/projects/${encodeURIComponent(slug)}/level`, "PUT", { level, reason });
export const saveClaudeDefault = (model: string) => send<ClaudeSettings>("/api/claude/settings", "PUT", { default_model: model });
export const fetchWork = (sid: string) => get<Work>(`/api/simple/sessions/${sid}`);
/** A further message (3.6.4): the same remedy as a start — one id per click on « Send », the
 *  server takes it once, and a failure that says nothing (no answer, a 5xx from the way) is
 *  asked again with the SAME id before anything is shown: the message may well have arrived. */
export const sendMessage = (sid: string, text: string, documents: string[], requestId: string) =>
  retrying(() => send<{ ok: boolean; replayed?: boolean }>(`/api/simple/sessions/${sid}/message`, "POST",
    { text, documents, request_id: requestId }));
const dl = (sid: string, name: string) => `/api/simple/sessions/${sid}/deliverables/${encodeURIComponent(name)}`;
export const fetchDeliverable = (sid: string, name: string) => get<Deliverable>(dl(sid, name));
export const downloadUrl = (sid: string, name: string, format: "md" | "docx" | "xlsx") => `${dl(sid, name)}/download?format=${format}`;
export const validateDeliverable = (sid: string, name: string) =>
  send<{ ok: boolean; deliverables: Deliverable[] }>(`${dl(sid, name)}/validate`, "POST");
export const keepAsDeliverable = (sid: string, title: string, text: string) =>
  send<{ name: string; deliverables: Deliverable[] }>(`/api/simple/sessions/${sid}/deliverables`, "POST", { title, text });

export const fetchDocuments = () => get<{ project: string; limits: Limits; documents: Doc[] }>("/api/documents");
export const deleteDocument = (id: string) => send<{ ok: boolean }>(`/api/documents/${id}`, "DELETE");
export const documentUrl = (id: string) => `/api/documents/${id}/content`;

/** One file → PUT, the raw file as the body (no multipart). */
export async function uploadDocument(file: File, session = ""): Promise<Doc> {
  const q = new URLSearchParams({ name: file.name, modified: String(file.lastModified || 0) });
  if (session) q.set("session", session);
  const r = await fetch(`/api/documents?${q}`, { method: "PUT", body: file,
    headers: { "content-type": "application/octet-stream" } });
  return r.ok ? r.json() : fail(r);
}

/** The session's plain-language stream. Reconnects with backoff until closed. */
export class PlainSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private backoff = 500;
  constructor(private sid: string, private onEvents: (first: boolean) => void,
    private onEvent: (e: PlainEvent) => void, private onOpen: (open: boolean) => void) { this.connect(); }
  private connect() {
    if (this.closed) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/agent/ws/${this.sid}?view=simple`);
    this.ws = ws;
    ws.onopen = () => { this.backoff = 500; this.onEvents(true); this.onOpen(true); };
    ws.onmessage = (ev) => { try { this.onEvent(JSON.parse(ev.data) as PlainEvent); } catch { /* ignore */ } };
    ws.onclose = () => {
      this.onOpen(false);
      if (this.closed) return;
      setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    };
    ws.onerror = () => ws.close();
  }
  private send(o: unknown) { if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(o)); }
  /** `draftText`: the draft as the person corrected it — the server rebuilds the save from it */
  decide(id: string, decision: "allow" | "deny", draftText?: string) {
    this.send({ type: "permission", id, decision, ...(draftText ? { draft_text: draftText } : {}) });
  }
  answer(id: string, answers: Record<string, string | string[]>) { this.send({ type: "answer", id, answers }); }
  interrupt() { this.send({ type: "interrupt" }); }
  close() { this.closed = true; this.ws?.close(); }
}
