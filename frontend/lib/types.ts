export type Role = "user" | "assistant" | "system";

export interface ToolResult {
  text: string;
  is_error: boolean;
  truncated: boolean;
}

export interface Message {
  role: Role;
  kind: "text" | "thinking" | "tool" | "chip" | "note" | "tool_result_orphan";
  text?: string;
  // tool
  tool?: string;
  title?: string;
  input?: Record<string, unknown>;
  id?: string;
  result?: ToolResult | null;
  is_error?: boolean;
  ts?: string;
}

export type LiveStateKind = "dead" | "booting" | "working" | "awaiting" | "idle";

export interface SessionSummary {
  session_id: string;
  tag: string;
  window: string;
  title: string;
  mtime: number;
  age_s: number;
  active: boolean;
  alive: boolean;
  exists: boolean;
  live_state?: LiveStateKind;
  kind?: "sdk" | "tmux";
}

export interface UsageDay {
  day: string;
  turns: number;
  in_tokens: number;
  out_tokens: number;
  cost: number;
}

export interface UsageTotals {
  cost: number;
  out_tokens: number;
  turns: number;
}

export interface UsageSession {
  session_id: string;
  title: string;
  tag: string;
  models: string;
  turns: number;
  in_tokens: number;
  out_tokens: number;
  cache_read: number;
  cost: number;
  last_ts: number;
}

export interface UsageSummary {
  days: UsageDay[];
  totals: Record<"today" | "7d" | "30d" | "all", UsageTotals>;
  sessions: UsageSession[];
  by_model: { model: string; cost: number; out_tokens: number }[];
  note: string;
}

export interface LiveChoice {
  key: string;
  label: string;
}

export interface LiveState {
  session_id: string;
  window: string;
  alive: boolean;
  state: LiveStateKind;
  activity: string;
  tail: string;
  choices: LiveChoice[];
  question: string;
  changing: boolean;
}

export interface TmuxWindow {
  session: string;
  index: string;
  window: string;
  cmd: string;
  activity: string;
}

export interface Binding {
  tmux_session: string;
  window: string;
  session_id: string;
  target: string;
  alive: boolean;
  transcript_exists: boolean;
}

export interface Me {
  email: string;
  /** role IN THE SELECTED PROJECT (3.2), on the instance scale the UI checks */
  role: string;
  name: string;
  known: boolean;
  source: string;
  project?: string;
  project_role?: "viewer" | "dev" | "maintainer" | "admin" | null;
  instance_role?: string;
  /** member of the ops team (or instance admin): the Operate tab */
  ops?: boolean;
  /** B1: sessions receive the whole vault on this instance */
  secrets_warning?: string | null;
}

export interface ProjectRow {
  slug: string; name: string; role: string; access_source: string; shared: boolean;
}

export interface ProjectsList {
  projects: ProjectRow[]; multi: boolean; default: string; ops: boolean; instance_admin: boolean;
}

export interface IamUser {
  email: string;
  role: string;
  name: string;
  created_at: number;
}

export interface InfraNode {
  ip: string;
  name: string;
  role: string;
  monitored: boolean;
  up: boolean | null;
  cpu_pct: number | null;
  cores: number | null;
  mem_total: number | null;
  mem_avail: number | null;
  disk_total: number | null;
  disk_avail: number | null;
  disks: { mount: string; total: number | null; avail: number | null }[];
  load1: number | null;
  uptime_s: number | null;
}

export interface InfraTarget {
  job: string;
  instance: string;
  up: boolean;
}

export interface CloudEnv {
  client: string;
  tier: string;
  owner_email: string;
  status: string;
  public_url: string;
  created_at: number;
  updated_at: number;
  local_token?: string; // renvoyé UNE fois au spawn
  last_log?: string;
}

export interface MemNote {
  name: string;
  description: string;
  type: string;
  mtime: number;
  chunks: number;
  links: string[];
  backlinks: string[];
  priority?: boolean;
}

export interface MemStats {
  notes: number;
  chunks: number;
  model: string | null;
  last_mtime: number | null;
  cortex_url?: string | null;
}

export interface MemSearchResult {
  note_name: string;
  description: string;
  score: number;
  cosine?: number;
  snippet: string;
  path: string;
  priority?: boolean;
}

export interface PreviewRepo {
  name: string;
  path: string;
  branch: string;
  modified: number;
}

export interface PreviewEnv {
  name: string;
  label: string;
  cwd: string;
  port: number;
  url: string;
  preview_url: string;
  running: boolean;
  cwd_exists: boolean;
}

export interface DiffData {
  repo: string;
  path: string;
  branch: string;
  status: string;
  diff: string;
  truncated: boolean;
  files?: { path: string; added: number | null; deleted: number | null }[];
  has_tests?: boolean; // une test_cmd est déclarée pour ce repo (SOKKAN_REPOS)
}

export interface ChecklistItem {
  text: string;
  done: boolean;
}

export interface Card {
  id: number;
  level?: number;          // 3.4 classification rank (2 = project)
  title: string;
  description: string;
  tag: string;
  bucket: string;
  session_id: string | null;
  window: string | null;
  created_at: number;
  sort: number;
  priority: number; // 0 urgente · 1 haute · 2 normale · 3 basse
  due: string; // "YYYY-MM-DD" ou ""
  checklist: ChecklistItem[];
  updated_at: number | null;
  archived: number;
  assignee?: string; // IAM user email, "agent:<name>" or ""
  closed_at?: number | null; // closed = finished, not deleted
  closed_by?: string;
}

// where a card action came from: the SOKKAN session, and the channel
// ("web", "mcp", "agent-run #12") — set by the server, never by a model
export interface CardEvent {
  ts: number;
  user: string;
  action: string;
  detail: string;
  session_id?: string;
  session_tag?: string;
  via?: string;
}

export interface CardComment {
  id: number;
  ts: number;
  author: string;
  session_id: string;
  session_tag: string;
  via: string;
  body: string;
}

export interface CardLink {
  kind: "session" | "agent" | "run" | "incident";
  ref: string;
  label: string;
  status: string;
  href: string;
  ts: number | null;
  created_by: string;
  missing?: boolean;
}

export interface CardDetail extends Card {
  events: CardEvent[];
  comments?: CardComment[];
  links?: CardLink[];
}

export interface BoardData {
  buckets: string[];
  cards: Record<string, Card[]>;
}

export interface AuditEvent {
  ts: number;
  user: string;
  action: string;
  resource: string;
  detail: string;
}

export interface PreviewTrigger {
  env: string;
  path: string;
  session_id: string;
  tag: string;
  window: string;
  user: string;
  ts: number;
  running?: boolean;
  url?: string;
  preview_url?: string;
}

export interface SessionDetail {
  session_id: string;
  title: string;
  tag?: string;
  git_branch: string;
  mtime: number;
  size: number;
  n_messages: number;
  active: boolean;
  alive?: boolean;
  window?: string;
  exists?: boolean;
  starting?: boolean;
  messages: Message[];
}
