import type {
  AuditEvent, Binding, BoardData, CloudEnv, Card, CardDetail, DiffData, IamUser,
  InfraNode, InfraTarget, LiveState, Me, MemNote, MemSearchResult, MemStats,
  PreviewEnv, PreviewRepo, PreviewTrigger, SessionDetail, SessionSummary, TmuxWindow,
  UsageSummary,
} from "./types";

async function mutate<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${method} ${url} → ${r.status}`);
  return r.json();
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

export const fetchSessions = () => getJSON<SessionSummary[]>("/api/sessions");
export const fetchSession = (id: string) => getJSON<SessionDetail>(`/api/sessions/${id}`);
export const fetchLive = (id: string) => getJSON<LiveState>(`/api/sessions/${id}/live`);
export const sendKey = (id: string, key: string) =>
  mutate<{ ok: boolean }>(`/api/sessions/${id}/key`, "POST", { key });
export const fetchTags = () => getJSON<string[]>("/api/tags");
export const fetchTmux = () => getJSON<TmuxWindow[]>("/api/tmux");
export const fetchBindings = () => getJSON<Binding[]>("/api/bindings");

export const spawnSession = (tag: string, prompt = "", title = "", kind: "sdk" | "tmux" = "sdk") =>
  mutate<{ session_id: string; tag: string; window: string; title: string; kind?: string }>(
    "/api/spawn", "POST", { tag, prompt, title, kind }
  );

export const deleteSession = (id: string) =>
  mutate<{ ok: boolean }>(`/api/sessions/${id}`, "DELETE");

export const sendInput = (target: string, text: string) =>
  mutate<{ ok: boolean }>("/api/send", "POST", { target, text });

// board
export const fetchBoard = (archived = false) =>
  getJSON<BoardData>(`/api/board${archived ? "?archived=1" : ""}`);
export const fetchCardDetail = (id: number) => getJSON<CardDetail>(`/api/board/card/${id}`);
export const addCard = (description: string, tag = "backend", title = "", bucket = "Backlog", priority = 2) =>
  mutate<Card>("/api/board/card", "POST", { title, description, tag, bucket, priority });
export const patchCard = (id: number, fields: Partial<Card>) =>
  mutate<Card>(`/api/board/card/${id}`, "PATCH", fields);
export const deleteCard = (id: number) =>
  mutate<{ ok: boolean }>(`/api/board/card/${id}`, "DELETE");

// journal d'audit
export const fetchAudit = (limit = 200, q = "") =>
  getJSON<AuditEvent[]>(`/api/audit?limit=${limit}&q=${encodeURIComponent(q)}`);

// preview — trigger poussé par une session (MCP open_preview)
export const fetchPreviewTrigger = () =>
  getJSON<{ trigger: PreviewTrigger | null }>("/api/preview/trigger");

// coûts / usage (transcripts)
export const fetchUsage = (days = 30) => getJSON<UsageSummary>(`/api/usage?days=${days}`);
// iam
export const fetchMe = () => getJSON<Me>("/api/me");
export const iamUsers = () => getJSON<IamUser[]>("/api/iam/users");
export const iamUpsert = (email: string, role: string, name = "") =>
  mutate<IamUser>("/api/iam/users", "POST", { email, role, name });
export const iamDelete = (email: string) =>
  mutate<{ ok: boolean }>(`/api/iam/users/${encodeURIComponent(email)}`, "DELETE");

// infra
export const infraNodes = () => getJSON<InfraNode[]>("/api/infra/nodes");
export const infraTargets = () => getJSON<InfraTarget[]>("/api/infra/targets");
export interface LlmStatus {
  mode: string; configured: boolean; byok_kind: string | null;
  model: string | null; operator_managed: boolean;
}
export interface LlmUsage {
  client: string; day: string; used_today: number; daily_quota_tokens: number;
  used_month: number; monthly_quota_tokens: number;
}
export interface InstanceInfo { org_name: string; tier: string; public_url: string; owner_email: string; }
export const instanceInfo = () => getJSON<InstanceInfo>("/api/instance");
export const instanceRename = (org_name: string) => mutate<InstanceInfo>("/api/instance", "POST", { org_name });
export const llmStatus = () => getJSON<LlmStatus>("/api/llm");
export const llmUsage = () => getJSON<LlmUsage | null>("/api/llm/usage");
export const llmSetApiKey = (anthropic_api_key: string) =>
  mutate<LlmStatus>("/api/llm", "POST", { mode: "byok", anthropic_api_key });
export const llmSetSubscription = (claude_oauth_token: string) =>
  mutate<LlmStatus>("/api/llm", "POST", { mode: "byok", claude_oauth_token });
export const cloudEnvs = () => getJSON<CloudEnv[]>("/api/infra/envs");
export const cloudEnvDetail = (client: string) => getJSON<CloudEnv>(`/api/infra/envs/${client}`);
export const cloudEnvSpawn = (client: string, tier: string, owner_email: string) =>
  mutate<CloudEnv>("/api/infra/envs", "POST", { client, tier, owner_email });
export const cloudEnvDestroy = (client: string) =>
  mutate<{ client: string; status: string }>(`/api/infra/envs/${client}`, "DELETE");

// flotte du client (managé) — connecteur backend/fleet.py → portail app.sokkan.ch
export interface FleetProduct { sku: string; category: string; label: string; desc: string; price_chf: number; }
export interface FleetResource { id: number; sku: string; name: string; status: string; created_at: number; }
export interface FleetView {
  tenant: string; plan: string | null; catalog: FleetProduct[];
  resources: FleetResource[]; infra_status: string | null;
}
export const fleetView = () => getJSON<FleetView | null>("/api/fleet");
export const fleetRequest = (sku: string, name = "") =>
  mutate<{ ok: boolean; sku: string; invoice: string | null; status: string }>("/api/fleet/request", "POST", { sku, name });

// mémoire / KB
export const memoryStats = () => getJSON<MemStats>("/api/memory/stats");
export const memoryNotes = () => getJSON<MemNote[]>("/api/memory/notes");
export const memorySearch = (q: string, k = 8) =>
  getJSON<MemSearchResult[]>(`/api/memory/search?q=${encodeURIComponent(q)}&k=${k}`);
export const memoryNote = (name: string) =>
  getJSON<{ name: string; body: string }>(`/api/memory/note/${encodeURIComponent(name)}`);

// preview
export const fetchPreviewRepos = () => getJSON<PreviewRepo[]>("/api/preview/repos");
export const fetchDiff = (repo: string) =>
  getJSON<DiffData>(`/api/preview/diff?repo=${encodeURIComponent(repo)}`);
export const shotUrl = (url: string, w = 1440, h = 900) =>
  `/api/preview/shot?url=${encodeURIComponent(url)}&w=${w}&h=${h}`;
export const fetchEnvs = () => getJSON<PreviewEnv[]>("/api/preview/envs");
export const startEnv = (name: string) =>
  mutate<{ running: boolean; url: string }>(`/api/preview/envs/${name}/start`, "POST");
export const stopEnv = (name: string) =>
  mutate<{ running: boolean }>(`/api/preview/envs/${name}/stop`, "POST");

export const spawnCard = (id: number) =>
  mutate<{ session_id: string; window: string; card_id: number }>(
    `/api/board/card/${id}/spawn`, "POST"
  );
