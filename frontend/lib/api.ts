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

export interface Playbook { id: string; label: string; description: string; tag: string; subject_optional: boolean }
export const fetchPlaybooks = () => getJSON<Playbook[]>("/api/playbooks");

export const spawnSession = (tag: string, prompt = "", title = "", kind: "sdk" | "tmux" = "sdk", playbook = "") =>
  mutate<{ session_id: string; tag: string; window: string; title: string; kind?: string }>(
    "/api/spawn", "POST", { tag, prompt, title, kind, playbook }
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

export interface TestRun { repo: string; cmd: string; code: number; passed: boolean; output: string }
export const runPreviewTests = (repo: string) => mutate<TestRun>(`/api/preview/test/${repo}`, "POST");

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
  model: string | null; base_url?: string | null; operator_managed: boolean;
}
export interface LlmUsage {
  client: string; day: string; used_today: number; daily_quota_tokens: number;
  used_month: number; monthly_quota_tokens: number;
  balance_centimes?: number; // wallet prépayé (inférence gérée)
  spent_today_centimes?: number; spent_month_centimes?: number; // dépenses réelles (ledger)
  // jauge d'escalade : tokens servis en tier supérieur, facturés au tier demandé
  // jusqu'à la franchise mensuelle (engagement public anti-escalade-artificielle)
  escalated_month_tokens?: number; escalation_franchise_tokens?: number;
  per_user?: { user: string; input_tokens: number; output_tokens: number; requests: number }[];
  rates_chf_per_mtok?: { up_to_input: number; input: number; output: number }[];
  coding_tiers_chf_per_mtok?: { id: string; label: string; description: string;
                                chf_per_mtok_in: number; chf_per_mtok_out: number }[];
}
export const llmCredit = (pack: number) =>
  mutate<{ ok: boolean; checkout_url: string }>("/api/llm/credit", "POST", { pack });
export interface InstanceInfo {
  org_name: string; tier: string; public_url: string; owner_email: string;
  budget_session_usd?: number; budget_day_usd?: number; // 0 = off
  update?: { local_version: string; latest: string | null; update_available: boolean };
}
export const instanceInfo = () => getJSON<InstanceInfo>("/api/instance");
export const instanceRename = (org_name: string) => mutate<InstanceInfo>("/api/instance", "POST", { org_name });
export const instanceBudgets = (budget_session_usd: number, budget_day_usd: number) =>
  mutate<InstanceInfo>("/api/instance", "POST", { budget_session_usd, budget_day_usd });
export const llmStatus = () => getJSON<LlmStatus>("/api/llm");
export const llmUsage = () => getJSON<LlmUsage | null>("/api/llm/usage");
export const llmSetApiKey = (anthropic_api_key: string) =>
  mutate<LlmStatus>("/api/llm", "POST", { mode: "byok", anthropic_api_key });
export const llmSetSubscription = (claude_oauth_token: string) =>
  mutate<LlmStatus>("/api/llm", "POST", { mode: "byok", claude_oauth_token });
export const llmSetCustom = (base_url: string, auth_token: string, model: string, small_model = "") =>
  mutate<LlmStatus>("/api/llm", "POST", { mode: "custom", base_url, auth_token, model, small_model });
export interface LlmTier { id: string; label: string; description: string; chf_per_mtok_in: number; chf_per_mtok_out: number; }
export const llmTiers = () => getJSON<{ tiers: LlmTier[]; current: string | null }>("/api/llm/tiers");
export const llmSetTier = (tier: string) => mutate<{ ok: boolean; tier: string }>("/api/llm/tier", "POST", { tier });
export const cloudEnvs = () => getJSON<CloudEnv[]>("/api/infra/envs");
export const cloudEnvDetail = (client: string) => getJSON<CloudEnv>(`/api/infra/envs/${client}`);
export const cloudEnvSpawn = (client: string, tier: string, owner_email: string) =>
  mutate<CloudEnv>("/api/infra/envs", "POST", { client, tier, owner_email });
export const cloudEnvDestroy = (client: string) =>
  mutate<{ client: string; status: string }>(`/api/infra/envs/${client}`, "DELETE");

// flotte du client (managé) — connecteur backend/fleet.py → portail app.sokkan.ch
export interface FleetProduct { sku: string; category: string; label: string; desc: string; price_chf: number; }
export interface FleetResource {
  id: number; sku: string; name: string; status: string; created_at: number;
  fleet_host?: string; private_ip?: string; uri?: string; // adressage réel (une fois provisionnée)
}
export interface FleetRoute {
  id: number; kind: "subdomain" | "custom"; hostname: string;
  target: string; port: number; created_at: number;
}
export interface FleetView {
  tenant: string; plan: string | null; catalog: FleetProduct[];
  resources: FleetResource[]; infra_status: string | null; cockpit_ip?: string | null;
  can_term?: boolean; // droit terminal maintenance (admin ou grant explicite)
  routes?: FleetRoute[]; // exposition web (managé)
  edge_host?: string;    // cible CNAME des domaines custom (edge-<tenant>.sokkan.ch)
  route_suffix?: string; // suffixe des sous-domaines (-<tenant>.sokkan.ch)
  deploys?: Record<string, string[]>; // historique d'images déployées par worker
}
export const fleetView = () => getJSON<FleetView | null>("/api/fleet");
export const fleetRequest = (sku: string, name = "") =>
  mutate<{ ok: boolean; sku: string; invoice: string | null; status: string }>("/api/fleet/request", "POST", { sku, name });
export const fleetRemove = (rid: number) =>
  mutate<{ ok: boolean; status: string }>(`/api/fleet/resource/${rid}`, "DELETE");
export const fleetRouteAdd = (kind: string, name: string, hostname: string, target: string, port: number) =>
  mutate<{ ok: boolean; id: number; hostname: string; edge_host: string }>(
    "/api/fleet/routes", "POST", { kind, name, hostname, target, port });
export const fleetRouteRemove = (rid: number) =>
  mutate<{ ok: boolean }>(`/api/fleet/routes/${rid}`, "DELETE");
export const fleetUpgrade = () =>
  mutate<{ client: string; status: string }>("/api/fleet/upgrade", "POST");
export const fleetDeploy = (addon: string, image: string) =>
  mutate<{ addon: string; image: string; status: string }>("/api/fleet/deploy", "POST", { addon, image });
export const fleetRollback = (addon: string) =>
  mutate<{ addon: string; status: string }>("/api/fleet/rollback", "POST", { addon });

// notifications (HITL push + alertes prod)
export interface NotifyStatus { telegram: boolean; webhook: boolean; hitl_enabled: boolean; hitl_delay_s: number; }
export const notifyStatus = () => getJSON<NotifyStatus>("/api/notify");
export const notifySet = (cfg: Partial<{ telegram_bot_token: string; telegram_chat_id: string; webhook_url: string; hitl_enabled: boolean }>) =>
  mutate<NotifyStatus>("/api/notify", "POST", cfg);
export const notifyTest = () => mutate<{ sent: Record<string, string> }>("/api/notify/test", "POST");

// observability / operate
export interface Incident { id: number; ts: number; title: string; summary: string; severity: string; status: string; session_id: string; }
export interface ObsStatus { enabled: boolean; prometheus: boolean; loki: boolean; grafana: boolean; grafana_public_url: string | null; incidents: Incident[]; }
export interface Dashboard { title: string; uid: string; url?: string; }
export const obsStatus = () => getJSON<ObsStatus>("/api/observability");
export const obsDashboards = () => getJSON<Dashboard[]>("/api/observability/dashboards");
export const obsIncidentSet = (rid: number, status: string) =>
  mutate<{ ok: boolean }>(`/api/observability/incident/${rid}`, "POST", { status });

// vault (secrets injected into sessions as env vars — values never returned)
export const vaultList = () => getJSON<{ names: string[] }>("/api/vault");
export const vaultSet = (name: string, value: string) =>
  mutate<{ names: string[] }>("/api/vault", "POST", { name, value });
export const vaultDelete = (name: string) =>
  mutate<{ names: string[] }>(`/api/vault/${encodeURIComponent(name)}`, "DELETE");

// runbooks (memory notes named runbook-*, replayable as a guided session)
export interface Runbook { name: string; description: string; mtime: number; }
export const runbooksList = () => getJSON<Runbook[]>("/api/runbooks");
export const runbookRun = (name: string) =>
  mutate<{ session_id: string }>(`/api/runbooks/${encodeURIComponent(name)}/run`, "POST");
export const fleetGrants = () => getJSON<{ grants: string[] }>("/api/fleet/grants");
export const fleetGrantsSet = (emails: string[]) =>
  mutate<{ grants: string[] }>("/api/fleet/grants", "POST", { emails });

// mémoire / KB
export const memoryStats = () => getJSON<MemStats>("/api/memory/stats");
export const memoryNotes = () => getJSON<MemNote[]>("/api/memory/notes");
export const memorySearch = (q: string, k = 8) =>
  getJSON<MemSearchResult[]>(`/api/memory/search?q=${encodeURIComponent(q)}&k=${k}`);
export const memoryDigest = () =>
  mutate<{ session_id: string }>("/api/memory/digest", "POST");
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

// magnitude — profile the host hardware, bench & run local models (llama.cpp),
// connect the local Anthropic-compatible shim to the session router
export interface MagnitudeGpu {
  vendor: string; name: string; backend: string;
  // null when the GPU was detected via vulkaninfo/lspci (no VRAM figures)
  vram_total_gb: number | null; vram_free_gb: number | null;
  driver: string | null; power_limit_w: number | null;
}
export interface MagnitudeProfile {
  schema: string; os: string; arch: string;
  hostname?: string;
  gpu: MagnitudeGpu | null;
  cpu: string; cores: number; ram_gb: number | null;
  class: "XL" | "L" | "M" | "S" | "CPU" | "unsupported";
}
export interface MagnitudeStatus {
  phase: "idle" | "benching" | "downloading" | "starting" | "serving" | "error";
  model?: string | null; pct?: number | null; detail?: string | null;
}
export interface MagnitudeBench {
  gen_tok_s: number | null; prefill_tok_s: number | null;
  power_avg_w: number | null; eur_per_mtok_gen: number | null;
  wall_s: number; at: number;
}
export interface MagnitudeServing { model: string; shim_url: string; since: number; }
export interface MagnitudeModel {
  id: string; label: string; params: string; moe: boolean;
  weights_gb: number; note: string;
  fits: boolean; fit: "comfortable" | "tight" | "no" | "unknown";
}
export interface MagnitudeNode {
  id: string; name: string; online: boolean; last_seen: number | null;
  shim_url: string; // URL du shim de ce node vue des sessions (défaut ou explicite)
  profile: MagnitudeProfile | null;
  status: MagnitudeStatus;
  bench: Record<string, MagnitudeBench>;
  serving: MagnitudeServing | null;
  connected: boolean;
  catalog: MagnitudeModel[];
}
export interface MagnitudeState {
  paired: boolean; shim_default: string; nodes: MagnitudeNode[];
}
export const magnitudeState = () => getJSON<MagnitudeState>("/api/magnitude");
export const magnitudePair = () =>
  mutate<{ node: string; token: string; command: string }>("/api/magnitude/pair", "POST");
export const magnitudeUnpair = (node: string) =>
  mutate<MagnitudeState>(`/api/magnitude/node/${encodeURIComponent(node)}`, "DELETE");
export const magnitudeNodeConfig = (node: string, cfg: { name?: string; shim_url?: string }) =>
  mutate<MagnitudeState>(`/api/magnitude/node/${encodeURIComponent(node)}`, "POST", cfg);
export const magnitudeCmd = (node: string, action: "bench" | "run" | "stop", model?: string) =>
  mutate<MagnitudeState>("/api/magnitude/cmd", "POST", model ? { node, action, model } : { node, action });
export const magnitudeConnect = (node: string) =>
  mutate<MagnitudeState>("/api/magnitude/connect", "POST", { node });

// comme mutate, mais l'erreur porte le `detail` du backend (affiché tel quel dans l'UI)
async function mutateD<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" && detail ? detail : `${method} ${url} → ${r.status}`);
  }
  return r.json();
}

// mémoire (CortHeXis) — banc de recall sur les notes du client
export interface BenchMetrics { n: number; weight: number; h1: number | null; h5: number | null; mrr: number | null; ndcg10: number | null; }
export interface BenchRun {
  id: number; generation_id: number | null; embed_identity: string | null; profile: string | null;
  trigger: string; status: "running" | "done" | "failed"; started_at: string; finished_at: string | null;
  n_questions: number; error: string | null;
  metrics: { overall?: BenchMetrics; by_source?: Record<string, BenchMetrics>; stale?: number;
             errors?: number; search_ms_p50?: number | null; search_ms_p95?: number | null };
  regression: BenchComparison | null;
}
export interface BenchExample { id: number; question: string | null; expected: string[]; rank_before: number | null; rank_after: number | null; top_after: string[]; }
export interface BenchComparison {
  common: number; enough: boolean; regressed: boolean; max_drop?: number; reason?: string;
  base?: BenchMetrics; new?: BenchMetrics; delta?: Record<string, number | null>;
  lost?: number; gained?: number; worse?: number;
  lost_examples?: BenchExample[]; worse_examples?: BenchExample[]; gained_examples?: BenchExample[];
}
export interface BenchFinding { check: string; severity: "critical" | "warning" | "info"; title: string; detail: string; remedy: string; notes: string[]; }
export interface BenchCounts { active: number; disabled: number; }
export interface BenchOverview {
  available: boolean; reason?: string;
  questions?: { transcript: BenchCounts; client: BenchCounts; generated: BenchCounts; total_active: number };
  runs?: BenchRun[]; last?: BenchRun | null; active_generation?: number | null;
  findings?: BenchFinding[]; busy?: string[]; llm?: boolean; max_drop?: number;
  weights?: Record<string, number>;
}
export interface BenchQuestion {
  id: number; question: string; expected: string[]; source: "transcript" | "client" | "generated";
  weight: number; status: "active" | "disabled"; author: string | null; seen: number;
  created_at: string | null; measured: boolean; rank: number | null;
}
export const benchOverview = () => getJSON<BenchOverview>("/api/memory/eval");
export const benchQuestions = () => getJSON<BenchQuestion[]>("/api/memory/eval/questions");
export const benchAdd = (question: string, notes: string[]) =>
  mutateD<BenchQuestion>("/api/memory/eval/questions", "POST", { question, notes });
export const benchSetStatus = (id: number, status: "active" | "disabled") =>
  mutateD<{ ok: boolean }>(`/api/memory/eval/questions/${id}/status`, "POST", { status });
export const benchDelete = (id: number) =>
  mutateD<{ ok: boolean }>(`/api/memory/eval/questions/${id}`, "DELETE");
export const benchRun = () => mutateD<{ started: boolean }>("/api/memory/eval/run", "POST");
export const benchHarvest = () =>
  mutateD<{ files: number; pairs: number; new: number }>("/api/memory/eval/harvest", "POST");
export const benchGenerate = () => mutateD<{ started: boolean }>("/api/memory/eval/generate", "POST");

// carte « Mémoire » de Magnitude — profil, changement surveillé par le banc, licence
export interface MemProfileCosts {
  ram_gb: number; vram_gb: number; download_mb: number; query_ms_p50: number;
  rerank_ms_top10: number | null; index_chunks_per_s: number; reindex_250k_h: number;
  mrr: number; mrr_reranked?: number; mrr_fallback: number;
}
export interface MemProfile {
  id: "leger" | "standard" | "gpu"; label: string; embed: string; embed_device: string;
  reranker: string | null; rerank_policy: string; fits: boolean; costs: MemProfileCosts;
  min: Record<string, number>;
}
export interface MemoryView {
  current: string | null; recommended: string;
  local: { recommended: string; reason: string; warnings: string[] };
  nodes: { id: string; name: string; online: boolean; recommended: string; reason: string }[];
  profiles: MemProfile[];
  engine: { profile?: string; identity?: string; model?: string; label?: string; licence?: string;
            urls?: string[]; rerank_url?: string | null; error?: string };
  models: { active: { embed?: string | null; reason?: string } | null; installed: Record<string, boolean>;
            licence: { decision: string | null; terms_version: string | null; at: string | null;
                       by: string | null; via: string | null; current: boolean };
            terms_version: string; terms_url: string; policy_url: string };
}
export interface SwitchTarget { profile: string; model: string | null; urls: string[]; rerank_url: string | null; rebuild?: boolean; }
export interface SwitchJob {
  id: number; kind: "switch" | "rollback";
  status: "building" | "evaluating" | "switched" | "blocked" | "failed" | "cancelled" | "rolled_back";
  phase: string | null; progress: number; detail: string | null;
  from_generation: number | null; to_generation: number | null; from_target: SwitchTarget | null;
  to_target: SwitchTarget; built: boolean; comparison: BenchComparison | null;
  requested_by: string | null; decided_by: string | null; started_at: string; finished_at: string | null;
}
export interface MemGeneration {
  id: number; status: "building" | "active" | "retired"; identity: string; dim: number; chunks: number;
  created_at: string; activated_at: string | null; retired_at: string | null;
  rollback_until: string | null; profile: string | null;
}
export interface SwitchState {
  available: boolean; active_generation?: number | null; identity?: string | null;
  current?: SwitchTarget; job?: SwitchJob | null; history?: SwitchJob[]; generations?: MemGeneration[];
  max_drop?: number; min_questions?: number; retention_days?: number;
  rollback?: { switch: number; to_generation: number; to_target: SwitchTarget; since: string; until: string } | null;
}
export const memoryView = () => getJSON<MemoryView>("/api/magnitude/memory");
export const memorySwitchState = () => getJSON<SwitchState>("/api/magnitude/memory/switch");
export const memorySwitch = (t: { profile: string; model?: string | null; urls?: string[]; rebuild?: boolean }) =>
  mutateD<SwitchJob>("/api/magnitude/memory/switch", "POST", t);
export const memorySwitchDecide = (id: number, action: "approve" | "cancel") =>
  mutateD<SwitchJob>(`/api/magnitude/memory/switch/${id}/${action}`, "POST");
export const memoryRollback = () => mutateD<SwitchJob>("/api/magnitude/memory/rollback", "POST");
export const memoryLicence = (decision: "accepted" | "declined") =>
  mutateD<{ licence: MemoryView["models"]["licence"]; downloading: boolean; installed: boolean }>(
    "/api/magnitude/memory/licence", "POST", { decision });

// agents — « Crew » (3.1) : un agent = une carte du deck (docs/AGENTS.md)
export type DeckState = "idle" | "armed" | "running" | "error" | "archived";
export interface AgentRunLite { id: number; status: string; started_at: number | null; ended_at: number | null; cost_usd: number }
export interface Agent {
  id: number; name: string; owner: string; model: string; purpose: string; deliverable: string;
  done_criteria: string; playbook: string; trigger: "manual" | "once" | "cron" | "event";
  schedule: string; timezone: string; once_at: number | null; event: string;
  tools: string[]; mcp: string[]; auto_approve: string[]; secrets: string[];
  budget_usd: number; max_minutes: number; outputs: string[]; notify_on: string[];
  status: "draft" | "pending" | "active" | "paused" | "archived";
  pending_change: Partial<Agent> | null; created_by: string; approved_by: string;
  approved_at: number | null; next_run_at: number | null; last_run_at: number | null;
  stats?: { runs: number; cost_usd: number; live: number; waiting: number };
  last_run?: AgentRunLite | null;
  deck: DeckState; needs_approval: boolean; waiting_for_human: boolean;
}
export interface AgentRun {
  id: number; agent_id: number; trigger: string; scheduled_for: number | null; status: string;
  waiting_approval: boolean; session_id: string; cost_usd: number; tokens_in: number;
  tokens_out: number; num_turns: number; deliverable: string; outputs: Record<string, unknown>;
  error: string; context: Record<string, unknown>; requested_by: string; created_at: number;
  started_at: number | null; ended_at: number | null; agent_name?: string;
}
export interface AgentsMeta {
  secrets: string[]; tools: string[]; default_tools: string[]; mcp: string[]; outputs: string[];
  notify_on: string[]; models: string[]; triggers: string[]; playbooks: Playbook[]; timezone: string;
}
export interface AgentsList { agents: Agent[]; pending: { agents: Agent[]; runs: (AgentRun & { agent_name: string })[] } }

async function mutateDetail<T>(url: string, method: string, body?: unknown): Promise<T> {
  // comme mutate, mais remonte le message de validation de l'API (detail)
  const r = await fetch(url, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}
export const agentsList = (archived = false) => getJSON<AgentsList>(`/api/agents${archived ? "?archived=1" : ""}`);
export const agentsMeta = () => getJSON<AgentsMeta>("/api/agents/meta");
export const agentGet = (id: number) => getJSON<Agent>(`/api/agents/${id}`);
export const agentCreate = (fields: Partial<Agent> & { activate?: boolean }) =>
  mutateDetail<Agent>("/api/agents", "POST", fields);
export const agentPatch = (id: number, fields: Partial<Agent>) =>
  mutateDetail<Agent>(`/api/agents/${id}`, "PATCH", fields);
export const agentAction = (id: number, action: "approve" | "reject" | "pause" | "resume" | "archive" | "run") =>
  mutateDetail<{ agent: Agent; run?: AgentRun }>(`/api/agents/${id}/${action}`, "POST");
export const agentRuns = (id: number, limit = 50) => getJSON<AgentRun[]>(`/api/agents/${id}/runs?limit=${limit}`);
export const agentRun = (runId: number) => getJSON<AgentRun>(`/api/agents/runs/${runId}`);
export const agentRunCancel = (runId: number) => mutateDetail<{ ok: boolean }>(`/api/agents/runs/${runId}/cancel`, "POST");
export const agentPropose = (fields: Partial<Agent>) => mutateDetail<Agent>("/api/agents/proposals", "POST", fields);
