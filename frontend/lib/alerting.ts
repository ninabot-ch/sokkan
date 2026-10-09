// 3.5 — Operate › Alerts: the typed client of /api/alerting (contract v1). The project goes in
// the x-sokkan-project header like every /api call (lib/project.ts). Errors carry the API's
// readable `detail` so the UI can say it as is.
import type {
  Alert, AlertCounts, AlertingStatus, AlertSource, Channel, ChannelKindDef, PreviewResult, Rule, RuleCounts, RuleIn,
  Silence, SourceIn, Template, TestResult, Transition,
} from "./alertingModel";

export class ApiError extends Error {
  status: number;
  constructor(status: number, msg: string) { super(msg); this.status = status; }
}

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method, cache: "no-store",
    headers: body !== undefined ? { "content-type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = `${method} ${url} → ${r.status}`;
    try {
      const j = await r.json();
      if (typeof j?.detail === "string") msg = j.detail;
      else if (Array.isArray(j?.detail)) msg = j.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join(" · ") || msg;
    } catch { /* not json */ }
    throw new ApiError(r.status, msg);
  }
  return r.json();
}

const B = "/api/alerting";
const q = (o: Record<string, string | number | undefined | null>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== "") s.set(k, String(v));
  const t = s.toString();
  return t ? `?${t}` : "";
};

export const alertingStatus = () => call<AlertingStatus>(`${B}/status`);

// sources
export const listSources = () => call<{ sources: AlertSource[] }>(`${B}/sources`).then((r) => r.sources);
export const createSource = (s: SourceIn) => call<AlertSource>(`${B}/sources`, "POST", s);
export const updateSource = (id: number, s: SourceIn) => call<AlertSource>(`${B}/sources/${id}`, "PUT", s);
export const deleteSource = (id: number) => call<{ ok: boolean }>(`${B}/sources/${id}`, "DELETE");
export const testSource = (id: number) => call<TestResult>(`${B}/sources/${id}/test`, "POST");
export const testSourceDraft = (s: SourceIn) => call<TestResult>(`${B}/sources/test`, "POST", s);
export const suggest = (id: number, kind: "metrics" | "labels" | "values" | "fields",
  o: { metric?: string; label?: string; field?: string; q?: string } = {}) =>
  call<{ items: string[] }>(`${B}/sources/${id}/suggest${q({ kind, ...o })}`).then((r) => r.items);

// templates
export const listTemplates = () => call<{ templates: Template[] }>(`${B}/templates`).then((r) => r.templates);

// rules
export const listRules = () => call<{ rules: Rule[]; counts: RuleCounts }>(`${B}/rules`);
export const getRule = (id: number) => call<Rule>(`${B}/rules/${id}`);
export const createRule = (r: RuleIn) => call<Rule>(`${B}/rules`, "POST", r);
export const updateRule = (id: number, r: RuleIn) => call<Rule>(`${B}/rules/${id}`, "PUT", r);
export const deleteRule = (id: number) => call<{ ok: boolean }>(`${B}/rules/${id}`, "DELETE");
export const setRuleEnabled = (id: number, on: boolean) => call<Rule>(`${B}/rules/${id}/${on ? "enable" : "disable"}`, "POST");
export const testNotify = (id: number) => call<{ channels: Record<string, string> }>(`${B}/rules/${id}/test-notify`, "POST");
export const ruleHistory = (id: number, limit = 100) =>
  call<{ transitions: Transition[] }>(`${B}/rules/${id}/history${q({ limit })}`).then((r) => r.transitions);

// preview / backtest
export const preview = (rule: RuleIn, range: "1h" | "6h" | "24h" | "7d") =>
  call<PreviewResult>(`${B}/preview`, "POST", { rule, range });

// alerts
export const listAlerts = (state: "firing" | "pending" | "resolved" | "active" = "active", limit = 100) =>
  call<{ alerts: Alert[]; counts: AlertCounts }>(`${B}/alerts${q({ state, limit })}`);
export const ackAlert = (id: number) => call<Alert>(`${B}/alerts/${id}/ack`, "POST");
export const silenceAlert = (id: number, dur: string, reason = "") =>
  call<Alert>(`${B}/alerts/${id}/silence`, "POST", { for: dur, reason });
export const openIncident = (id: number) => call<{ alert: Alert; incident_id: number }>(`${B}/alerts/${id}/open-incident`, "POST");
export const proposeAgent = (id: number, agentId: number) =>
  call<{ proposal: string; status: string }>(`${B}/alerts/${id}/propose-agent`, "POST", { agent_id: agentId });

// silences
export const listSilences = () => call<{ silences: Silence[] }>(`${B}/silences`).then((r) => r.silences);
export const createSilence = (s: { rule_id: number | null; matchers: Record<string, string>; for: string; reason: string }) =>
  call<Silence>(`${B}/silences`, "POST", s);
export const deleteSilence = (id: number) => call<{ ok: boolean }>(`${B}/silences/${id}`, "DELETE");

// channels
export const listChannels = () => call<{ channels: Channel[]; kinds: ChannelKindDef[] }>(`${B}/channels`);
export const createChannel = (c: { name: string; kind: string; config: Record<string, string>; enabled: boolean }) =>
  call<Channel>(`${B}/channels`, "POST", c);
export const updateChannel = (id: number, c: { name: string; kind: string; config: Record<string, string>; enabled: boolean }) =>
  call<Channel>(`${B}/channels/${id}`, "PUT", c);
export const deleteChannel = (id: number) => call<{ ok: boolean }>(`${B}/channels/${id}`, "DELETE");
export const testChannel = (id: number) => call<{ ok: boolean; detail?: string }>(`${B}/channels/${id}/test`, "POST");

/** Crew agents of the project, for « propose an agent » (existing route of 3.1). */
export const listAgentsLite = () =>
  fetch("/api/agents", { cache: "no-store" }).then((r) => (r.ok ? r.json() : [])).then((j) =>
    (Array.isArray(j) ? j : j?.agents || []) as { id: number; name: string; status?: string }[]).catch(() => []);
