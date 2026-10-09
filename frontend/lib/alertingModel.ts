// 3.5 — Operate › Alerts: the pure part of the alerting UI (no React, no import), run by
// tests/nav/alerting_test.ts with node (type stripping). The API's types (contract v1 of
// 09.10.2026), the sentence that says a rule in plain words (shown instantly while the person
// edits — the server's `sentence` is the same idea), the wizard's form ⇄ rule mapping, its
// checks, and the scales of the backtest chart.

// ---------------------------------------------------------------- API types (contract v1)

export type SourceKind = "prometheus" | "loki" | "elasticsearch" | "opensearch" | "sokkan" | "external";
export type Severity = "info" | "warning" | "critical";
export type RuleType =
  | "threshold" | "any" | "frequency" | "spike" | "flatline" | "change"
  | "new_term" | "cardinality" | "absence" | "anomaly";
export type RuleState = "ok" | "pending" | "firing" | "silenced" | "error" | "disabled" | "new";
export type AlertState = "pending" | "firing" | "resolved" | "silenced";
export type ChannelKind = "telegram" | "teams" | "slack" | "email" | "webhook" | "pagerduty" | "instance";
/** "30s" "5m" "1h" "1d" "7d" */
export type Dur = string;
export type TV = [number, number | null];

export interface SourceAuth { kind: "none" | "basic" | "apikey" | "bearer"; user?: string; secret_set?: boolean; secret?: string }
export interface SourceStatus { ok: boolean | null; checked_at?: number | null; latency_ms?: number | null; error?: string | null }
export interface AlertSource {
  id: number;
  name: string;
  kind: SourceKind;
  url?: string | null;
  scope?: "instance" | "project";
  builtin?: boolean;
  auth?: SourceAuth;
  options?: { index?: string; time_field?: string; message_field?: string };
  status?: SourceStatus;
}
export interface SourceIn {
  name: string; kind: SourceKind; url: string;
  auth: { kind: SourceAuth["kind"]; user?: string; secret?: string };
  options: { index?: string; time_field?: string; message_field?: string };
}
export interface TestResult { ok: boolean; latency_ms?: number | null; detail?: string | null; error?: string | null }

export interface Filter { label?: string; field?: string; op: string; value: string }
export interface Builder {
  metric?: string;
  filters?: Filter[];
  agg?: "rate" | "increase" | "avg" | "max" | "min" | "sum" | "count" | "last" | "p95";
  range?: Dur;
  by?: string[];
  /** a denominator → the rule compares a percentage */
  ratio_of?: Builder | null;
  /** logs: text the line contains; level: error/warn… */
  text?: string;
  level?: string;
  /** ES */
  index?: string;
  field?: string;
}
export interface RuleQuery {
  mode: "builder" | "raw"; raw: string; builder: Builder;
  /** what a raw query measures, in words (« memory used (%) ») — set by a template, dropped when the query is edited */
  label?: string;
}

export interface QuietHours { enabled: boolean; start: string; end: string; tz?: string; days?: number[] }
export interface RuleActions { incident: boolean; diag_session: boolean; agent_id: number | null; runbook: string | null }

export interface RuleIn {
  name: string;
  description: string;
  severity: Severity;
  enabled: boolean;
  source_id: number | null;
  query: RuleQuery;
  type: RuleType;
  params: Record<string, Param>;
  every: Dur;
  for: Dur;
  group_by: string[];
  realert: Dur;
  quiet_hours: QuietHours;
  channels: number[];
  actions: RuleActions;
  labels: Record<string, string>;
  runbook_url: string;
  level: string | null;
  /** what the value is measured in (« % », « bytes », « s », « /s », « events », « days ») — the server
   *  infers it from the template / builder / query when not given */
  unit?: string | null;
}
export type Param = number | string | boolean | null;

export interface RuleStateInfo {
  state: RuleState; since?: number | null; last_eval?: number | null; last_value?: number | null;
  firing?: number; pending?: number; error?: string | null; next_eval?: number | null;
}
export interface Rule extends RuleIn {
  id: number;
  project?: string;
  created_by?: string;
  created_at?: number;
  updated_at?: number;
  query_compiled?: string;
  sentence?: string;
  state?: RuleStateInfo;
  spark?: TV[];
  /** "external:<alertname>" = a rule mirrored from an external receiver (Grafana): read-only */
  external?: string | null;
}
export interface RuleCounts { firing: number; pending: number; silenced: number; ok: number; error: number; disabled: number }

export interface TemplateVar { key: string; label: string; default: Param; unit?: string | null }
export interface Template {
  id: string; name: string; category: string; icon?: string; description: string;
  source_kind: SourceKind; available: boolean; missing?: string | null; source_id?: number | null;
  variables?: TemplateVar[];
  rule: Partial<RuleIn>;
}

export interface Series {
  group?: Record<string, string> | null; group_key?: string | null; points: TV[];
  /** 3.5: « rpi1 · node » — addresses resolved to host names (the address stays in the tooltip) */
  group_title?: string | null; group_display?: Record<string, string> | null;
}
export interface Interval { group_key?: string | null; start: number; end: number | null; peak?: number | null }
export interface SampleEvent { ts: number; text: string; fields?: Record<string, string> }
export interface Threshold { op: string; value: number }
export interface PreviewResult {
  kind: "metric" | "count" | "events";
  step_s?: number;
  query_compiled?: string;
  sentence?: string;
  series: Series[];
  threshold?: Threshold | null;
  reference?: Series | { lower: TV[]; upper: TV[] } | null;
  fired_intervals: Interval[];
  fires: number;
  sample_events?: SampleEvent[];
  error?: string | null;
  warnings?: string[];
  unit?: string | null;
}

export interface Alert {
  id: number; rule_id: number; rule_name: string; project?: string; severity: Severity;
  group?: Record<string, string> | null; group_key?: string | null; state: AlertState;
  started_at: number; fired_at?: number | null; resolved_at?: number | null; last_notified_at?: number | null;
  value?: number | null; threshold?: Threshold | null; summary?: string | null;
  acked_by?: string | null; acked_at?: number | null; silenced_until?: number | null; incident_id?: number | null;
  sample_events?: SampleEvent[]; link?: string;
  /** 3.5: what a person reads — « rpi1 · node », « 77.8 % > 50 % », « down » */
  group_title?: string | null; group_display?: Record<string, string> | null; value_text?: string | null;
  unit?: string | null;
}
export interface AlertCounts { firing: number; pending: number; silenced: number; acked: number }

export interface Transition {
  ts: number; group?: Record<string, string> | null; group_key?: string | null; group_title?: string | null; value_text?: string | null;
  from: string; to: string; value?: number | null; threshold?: number | null; note?: string | null;
}

export interface Silence {
  id: number; rule_id: number | null; matchers: Record<string, string>; starts_at: number; ends_at: number;
  reason?: string | null; created_by?: string | null; active: boolean;
}

export interface ChannelField { key: string; label: string; secret?: boolean }
export interface ChannelKindDef { kind: ChannelKind; label: string; fields: ChannelField[]; unavailable?: string | null }
export interface Channel {
  id: number; name: string; kind: ChannelKind; scope?: "instance" | "project"; enabled: boolean; builtin?: boolean;
  config: Record<string, string | boolean | null>;
  last_test?: { ok: boolean; at: number; detail?: string | null } | null;
}

export interface AlertingStatus {
  enabled: boolean;
  evaluator?: { leader?: boolean; tick_s?: number; last_tick?: number | null; rules?: number; errors?: number };
  defaults?: { prometheus?: boolean; loki?: boolean };
}

// ---------------------------------------------------------------- durations

const DUR = /^(\d+(?:\.\d+)?)\s*(s|m|h|d|w)$/;
const UNIT_S: Record<string, number> = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 };

/** "5m" → 300; "" or garbage → 0 */
export function durS(d: Dur | null | undefined): number {
  const m = DUR.exec(String(d ?? "").trim());
  return m ? Math.round(parseFloat(m[1]) * UNIT_S[m[2]]) : 0;
}
/** 300 → "5m", 3600 → "1h", 90 → "90s", 86400 → "1d" */
export function sDur(s: number): Dur {
  const v = Math.max(0, Math.round(s));
  if (v && v % 86400 === 0) return `${v / 86400}d`;
  if (v && v % 3600 === 0) return `${v / 3600}h`;
  if (v && v % 60 === 0) return `${v / 60}m`;
  return `${v}s`;
}
/** "5m" → "5 min", "1h" → "1 h", "1d" → "1 day", "7d" → "7 days" */
export function fmtDur(d: Dur | number | null | undefined): string {
  const v = typeof d === "number" ? d : durS(d);
  if (v <= 0) return "0 s";
  if (v % 86400 === 0) { const n = v / 86400; return `${n} day${n > 1 ? "s" : ""}`; }
  if (v % 3600 === 0) return `${v / 3600} h`;
  if (v % 60 === 0) return `${v / 60} min`;
  return `${v} s`;
}

// ---------------------------------------------------------------- numbers

/** Compact number for axes and sentences: 0.0213 → "0.0213", 12345 → "12.3k", 2.5e6 → "2.5M". */
export function fmtValue(v: number | null | undefined, unit?: string | null): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (unit === "bytes") {
    for (const [k, suf] of [[2 ** 40, "TB"], [2 ** 30, "GB"], [2 ** 20, "MB"], [2 ** 10, "KB"]] as [number, string][])
      if (a >= k) return `${trim(v / k)} ${suf}`;
    return `${trim(v)} B`;
  }
  if (unit === "s" || unit === "seconds") {
    if (a > 0 && a < 1) return `${trim(v * 1000)} ms`;
    if (a >= 7200) return `${trim(v / 3600)} h`;
    if (a >= 120) return `${trim(v / 60)} min`;
    return `${trim(v)} s`;
  }
  if (unit === "/s") return `${trim(v)}/s`;
  if (unit === "events") return a >= 1e4 ? `${trim(v / 1e3)}k` : String(Math.round(v));
  if (unit === "days") return `${trim(v)} d`;
  const u = unit === "%" || unit === "percent" ? " %" : "";
  if (a >= 1e9) return `${trim(v / 1e9)}G${u}`;
  if (a >= 1e6) return `${trim(v / 1e6)}M${u}`;
  if (a >= 1e4) return `${trim(v / 1e3)}k${u}`;
  return `${trim(v)}${u}`;
}
function trim(x: number): string {
  const a = Math.abs(x);
  const d = a === 0 ? 0 : a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : Math.min(6, 2 - Math.floor(Math.log10(a)));
  return String(Number(x.toFixed(d)));
}

// ---------------------------------------------------------------- rule types

export interface TypeDef {
  id: RuleType; label: string; hint: string;
  /** metric = a value over time; events = log lines / documents */
  data: "metric" | "events" | "both";
}
export const RULE_TYPES: TypeDef[] = [
  { id: "threshold", label: "Threshold", hint: "a value goes above or below a line", data: "both" },
  { id: "frequency", label: "Too many", hint: "at least N matching events in a window", data: "events" },
  { id: "any", label: "Any match", hint: "every matching event alerts", data: "events" },
  { id: "spike", label: "Spike", hint: "jumps (or drops) vs the usual rate", data: "both" },
  { id: "flatline", label: "Went quiet", hint: "fewer than N events — the logs stopped", data: "events" },
  { id: "absence", label: "Gone", hint: "no data any more — a target is down", data: "metric" },
  { id: "new_term", label: "New value", hint: "a value never seen before (new error, new IP)", data: "events" },
  { id: "change", label: "Changed", hint: "a field changes for the same key", data: "events" },
  { id: "cardinality", label: "Distinct count", hint: "too many (or too few) distinct values", data: "events" },
  { id: "anomaly", label: "Unusual", hint: "far from its usual level", data: "metric" },
];
export const typeDef = (t: RuleType): TypeDef => RULE_TYPES.find((d) => d.id === t) ?? RULE_TYPES[0];

/** Is the source a value-over-time one (Prometheus, SOKKAN figures) or an event one (logs, ES)? */
export const isMetricSource = (k: SourceKind | undefined) => k === "prometheus" || k === "sokkan";

/** The types a source can feed, in card order. */
export function typesFor(k: SourceKind | undefined): TypeDef[] {
  if (!k || k === "external") return RULE_TYPES;
  const metric = isMetricSource(k);
  return RULE_TYPES.filter((d) => d.data === "both" || (metric ? d.data === "metric" : d.data === "events"));
}

/** Default parameters of a type (contract names) — the wizard starts from these. */
export function defaultParams(t: RuleType): Record<string, Param> {
  switch (t) {
    case "threshold": return { op: ">", value: 0, reduce: "avg" };
    case "frequency": return { count: 10, window: "5m" };
    case "any": return {};
    case "spike": return { ratio: 3, direction: "up", window: "10m", reference: "1h", min_count: 10 };
    case "flatline": return { count: 1, window: "10m" };
    case "absence": return { window: "5m" };
    case "new_term": return { field: "", lookback: "7d" };
    case "change": return { field: "", key_field: "" };
    case "cardinality": return { field: "", op: ">", value: 100, window: "1h" };
    case "anomaly": return { z: 3, lookback: "24h" };
  }
}

// ---------------------------------------------------------------- the sentence

const OP_WORD: Record<string, string> = {
  ">": "goes above", ">=": "reaches", "<": "drops below", "<=": "falls to", "==": "equals",
};
const REDUCE_WORD: Record<string, string> = {
  avg: "the average of ", max: "the highest ", min: "the lowest ", sum: "the total of ", last: "",
};
const AGG_WORD: Record<string, string> = {
  rate: "the rate of ", increase: "the increase of ", avg: "the average of ", max: "the highest ",
  min: "the lowest ", sum: "the total of ", count: "the number of ", last: "", p95: "the 95th percentile of ",
};

const filt = (fs: Filter[] | undefined) =>
  (fs || []).filter((f) => (f.label || f.field || "").trim())
    .map((f) => `${(f.label || f.field || "").trim()} ${f.op} ${f.value}`).join(", ");

const KNOWN_RAW: [RegExp, string, string][] = [
  [/node_memory_MemAvailable_bytes\s*\/\s*node_memory_MemTotal_bytes/, "memory used", "%"],
  [/node_cpu_seconds_total\{[^}]*mode="idle"/, "CPU used", "%"],
  [/node_filesystem_(avail|free)_bytes[\s\S]*node_filesystem_size_bytes/, "disk used", "%"],
  [/probe_ssl_earliest_cert_expiry/, "days before the certificate expires", "days"],
  [/node_load(1|5|15)\b/, "load average", ""],
  [/^\s*up\s*(\{|$|[=!<>])/, "target up", ""],
];
const NOT_METRIC = new Set(["sum", "avg", "max", "min", "count", "rate", "irate", "increase", "by", "without", "on",
  "ignoring", "group_left", "group_right", "time", "vector", "scalar", "abs", "histogram_quantile", "quantile", "topk",
  "bottomk", "and", "or", "unless", "bool", "offset", "label_replace", "clamp_min", "clamp_max", "round", "delta",
  "deriv", "avg_over_time", "max_over_time", "min_over_time", "sum_over_time", "absent"]);

/** What a raw PromQL measures, in words, and its unit — never the query itself (same table as the
 *  server's humanize.describe_raw). Nothing recognisable → ["", ""]. */
export function describeRaw(promql: string): [string, string] {
  const q = promql || "";
  for (const [rx, label, unit] of KNOWN_RAW) if (rx.test(q)) return [label, unit];
  const rx = /\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{|\[|\)|$|\s)/g;
  let m: RegExpExecArray | null;
  while ((m = rx.exec(q))) {
    const name = m[1];
    if (NOT_METRIC.has(name) || !name.includes("_")) continue;
    const rate = /\b(i?rate|increase)\(/.test(q);
    let unit = /_bytes(_total)?$/.test(name) ? (rate && name.endsWith("_total") ? "/s" : "bytes")
      : /_seconds(_total)?$/.test(name) ? "s" : rate && name.endsWith("_total") ? "/s" : "";
    if (/\*\s*100/.test(q) && q.includes("/")) unit = "%";
    return [name.replace(/_(total|count|sum|bucket)$/, "").replace(/_/g, " ").trim(), unit];
  }
  return ["", ""];
}

/** What the rule watches, in words. */
export function subjectOf(q: RuleQuery, kind?: SourceKind): string {
  if (q.mode === "raw") {
    const label = (q.label || (kind === "prometheus" || !kind ? describeRaw(q.raw)[0] : "")).trim();
    // « memory used (%) is above 85 % » says « % » twice
    if (label) return label.replace(/\s*\(%\)\s*$/, "");
    return "the query";
  }
  const b = q.builder || {};
  if (isMetricSource(kind) || b.metric) {
    const m = (b.metric || "").trim() || "the metric";
    const f = filt(b.filters);
    const base = `${AGG_WORD[b.agg || "last"] ?? ""}${m}${f ? ` (${f})` : ""}`;
    if (b.ratio_of) return `the share of ${m}${f ? ` (${f})` : ""} in ${(b.ratio_of.metric || m)}${filt(b.ratio_of.filters) ? ` (${filt(b.ratio_of.filters)})` : ""}`;
    return base;
  }
  const parts: string[] = [];
  if (b.level) parts.push(`${b.level} `);
  parts.push("log lines");
  if (b.text) parts.push(` containing « ${b.text} »`);
  const f = filt(b.filters);
  if (f) parts.push(` (${f})`);
  if (b.index) parts.push(` in ${b.index}`);
  return parts.join("");
}

/** `up < 1` / `up == 0`: « a target is down », not « up drops below 1 ». */
export function isTargetDown(r: Pick<RuleIn, "type" | "params" | "query">): boolean {
  if (r.type !== "threshold") return false;
  const q = r.query;
  const up = (q.mode === "builder" && (q.builder?.metric || "").trim() === "up") || (q.mode === "raw" && describeRaw(q.raw)[0] === "target up");
  const op = String(r.params?.op ?? ""); const v = Number(r.params?.value);
  return up && (((op === "<" || op === "<=") && v === 1) || (op === "==" && v === 0));
}

/** The rule in one plain sentence — under the type cards, in the summary, in the list.
 *  « Alert when the rate of http_requests_total (status =~ 5..) goes above 2 for 5 min, per job. » */
export function ruleSentence(r: Pick<RuleIn, "type" | "params" | "for" | "query" | "group_by">, kind?: SourceKind,
  unit?: string | null): string {
  const p = r.params || {};
  const s = subjectOf(r.query, kind);
  const w = fmtDur(String(p.window ?? ""));
  const per = r.group_by?.length ? `, per ${r.group_by.join(" + ")}` : "";
  const hold = durS(r.for) > 0 ? ` for ${fmtDur(r.for)}` : "";
  const n = (k: string) => Number(p[k]) || 0;
  switch (r.type) {
    case "threshold": {
      if (isTargetDown(r)) return `Alert when a target is down${hold}${per}.`;
      const red = isMetricSource(kind) && r.query.mode === "builder" ? "" : (REDUCE_WORD[String(p.reduce || "last")] ?? "");
      const op = OP_WORD[String(p.op || ">")] ?? String(p.op);
      const pct = r.query.builder?.ratio_of ? "%" : unit;
      return `Alert when ${red}${s} ${op} ${fmtValue(Number(p.value), pct)}${hold}${per}.`;
    }
    case "frequency": return `Alert when at least ${n("count")} ${s} arrive within ${w}${per}.`;
    case "any": return `Alert on every one of the ${s}${per}.`;
    case "spike": {
      const k = n("ratio");
      const ref = fmtDur(String(p.reference ?? ""));
      const dir = p.direction === "down" ? `falls under 1/${k} of` : p.direction === "both" ? `moves ×${k} away from` : `jumps ×${k} above`;
      const floor = n("min_count") > 0 ? ` (ignored under ${n("min_count")})` : "";
      return `Alert when ${s} over ${w} ${dir} its level of the last ${ref}${floor}${per}.`;
    }
    case "flatline": return `Alert when fewer than ${n("count")} ${s} arrive within ${w}${per} — the source went quiet.`;
    case "absence": return `Alert when ${s} has no data for ${w}${per} — the target is gone.`;
    case "new_term": return `Alert when ${p.field || "a field"} of ${s} takes a value not seen in the last ${fmtDur(String(p.lookback ?? ""))}${per}.`;
    case "change": return `Alert when ${p.field || "a field"} of ${s} changes for the same ${p.key_field || "key"}.`;
    case "cardinality": {
      const word = p.op === "<" ? "fewer than" : "more than";
      return `Alert when ${s} has ${word} ${n("value")} distinct ${p.field || "values"} within ${w}${per}.`;
    }
    case "anomaly": return `Alert when ${s} is more than ${n("z")} standard deviations from its level of the last ${fmtDur(String(p.lookback ?? ""))}${hold}${per}.`;
  }
}

// ---------------------------------------------------------------- the wizard's form

export function emptyRule(src?: Pick<AlertSource, "id" | "kind"> | null): RuleIn {
  const metric = isMetricSource(src?.kind);
  const type: RuleType = metric ? "threshold" : "frequency";
  return {
    name: "", description: "", severity: "warning", enabled: true,
    source_id: src?.id ?? null,
    query: { mode: "builder", raw: "", builder: metric ? { metric: "", filters: [], agg: "rate", range: "5m", by: [] } : { filters: [], text: "" } },
    type, params: defaultParams(type),
    every: "1m", for: metric ? "5m" : "0s", group_by: [], realert: "1h",
    quiet_hours: { enabled: false, start: "22:00", end: "07:00", tz: "Europe/Zurich", days: [0, 1, 2, 3, 4, 5, 6] },
    channels: [], actions: { incident: true, diag_session: false, agent_id: null, runbook: null },
    labels: {}, runbook_url: "", level: null,
  };
}

/** A template's rule (possibly partial) completed into an editable rule. */
export function fromTemplate(t: Template, srcs: AlertSource[]): RuleIn {
  const src = srcs.find((s) => s.id === t.source_id) ?? srcs.find((s) => s.kind === t.source_kind) ?? null;
  const base = emptyRule(src);
  const r = t.rule || {};
  // a template groups with `group_by` alone; on a metric source the form's « One alert per » is the
  // builder's `by` (kept in sync with group_by on every edit) — fill it so the form says what the
  // sentence says (« per job + instance ») instead of an empty field
  const tb = r.query?.builder;
  const fillBy = isMetricSource(t.source_kind) && (r.query?.mode ?? "builder") === "builder"
    && !(tb?.by || []).length && (r.group_by || []).length;
  const by = fillBy ? [...(r.group_by || [])] : tb?.by;
  return {
    ...base, ...r,
    name: r.name ?? t.name,
    description: r.description ?? t.description,
    source_id: r.source_id ?? src?.id ?? null,
    query: { ...base.query, ...(r.query || {}), builder: { ...base.query.builder, ...(r.query?.builder || {}), ...(by ? { by } : {}) } },
    params: { ...defaultParams(r.type ?? base.type), ...(r.params || {}) },
    quiet_hours: { ...base.quiet_hours, ...(r.quiet_hours || {}) },
    actions: { ...base.actions, ...(r.actions || {}) },
    labels: { ...(r.labels || {}) },
    group_by: [...(r.group_by || [])],
    channels: [...(r.channels || [])],
  };
}

/** A saved rule back into the editable shape (server-only fields dropped). */
export function toRuleIn(r: Rule | RuleIn): RuleIn {
  const base = emptyRule(null);
  return {
    name: r.name, description: r.description ?? "", severity: r.severity, enabled: r.enabled,
    source_id: r.source_id, query: { ...base.query, ...r.query, builder: { ...(r.query?.builder || {}) } },
    type: r.type, params: { ...defaultParams(r.type), ...(r.params || {}) },
    every: r.every || "1m", for: r.for ?? "0s", group_by: [...(r.group_by || [])], realert: r.realert || "1h",
    quiet_hours: { ...base.quiet_hours, ...(r.quiet_hours || {}) },
    channels: [...(r.channels || [])], actions: { ...base.actions, ...(r.actions || {}) },
    labels: { ...(r.labels || {}) }, runbook_url: r.runbook_url ?? "", level: r.level ?? null,
  };
}

/** Changing the type keeps the shared parameters (window, field…) and fills the new ones. */
export function switchType(r: RuleIn, t: RuleType): RuleIn {
  const d = defaultParams(t);
  const keep: Record<string, Param> = {};
  for (const k of Object.keys(d)) if (k in r.params && r.params[k] !== null && r.params[k] !== "") keep[k] = r.params[k];
  return { ...r, type: t, params: { ...d, ...keep } };
}

/** Changing the source: a metric source and an event source do not share a builder. */
export function switchSource(r: RuleIn, src: AlertSource): RuleIn {
  const was = r.query.builder?.metric !== undefined;
  const now = isMetricSource(src.kind);
  let out: RuleIn = { ...r, source_id: src.id };
  if (was !== now) {
    const e = emptyRule(src);
    out = { ...out, query: e.query, for: e.for };
  }
  if (!typesFor(src.kind).some((d) => d.id === out.type)) out = switchType(out, typesFor(src.kind)[0].id);
  return out;
}

/** Does the query part say something? (builder: a metric, or a filter / text for logs) */
export function hasQuery(r: RuleIn, kind?: SourceKind): boolean {
  if (r.query.mode === "raw") return !!r.query.raw.trim();
  const b = r.query.builder || {};
  if (isMetricSource(kind)) return !!(b.metric || "").trim();
  return !!((b.text || "").trim() || (b.filters || []).some((f) => (f.label || f.field || "").trim()) || (b.index || "").trim() || kind === "external");
}

/** Problems that block « Activate », in words, per step (1 what · 2 condition · 3 who · 4 then). */
export function validateRule(r: RuleIn, kind?: SourceKind): { step: number; msg: string }[] {
  const out: { step: number; msg: string }[] = [];
  if (r.source_id === null || r.source_id === undefined) out.push({ step: 1, msg: "Pick where the data comes from." });
  if (!hasQuery(r, kind)) out.push({ step: 1, msg: r.query.mode === "raw" ? "Write the query." : isMetricSource(kind) ? "Pick a metric." : "Say which lines to look at: a text or a filter." });
  const p = r.params;
  const num = (k: string) => typeof p[k] === "number" && Number.isFinite(p[k] as number);
  if (r.type === "threshold" && !num("value")) out.push({ step: 2, msg: "Set the threshold value." });
  if ((r.type === "frequency" || r.type === "flatline") && !(num("count") && (p.count as number) >= 1)) out.push({ step: 2, msg: "The number of events must be 1 or more." });
  if (r.type === "spike" && !(num("ratio") && (p.ratio as number) > 1)) out.push({ step: 2, msg: "A spike factor must be above 1 (×2, ×3…)." });
  if ((r.type === "new_term" || r.type === "cardinality" || r.type === "change") && !String(p.field || "").trim()) out.push({ step: 2, msg: "Pick the field to watch." });
  if (r.type === "change" && !String(p.key_field || "").trim()) out.push({ step: 2, msg: "Pick the key the field belongs to (host, user…)." });
  if (r.type === "cardinality" && !num("value")) out.push({ step: 2, msg: "Set the number of distinct values." });
  // a threshold on a metric keeps window "0s" (= the raw value, what the API stores): editing such a
  // rule was blocked by « the window must be a duration » about a field the form does not show
  const zeroOk = (k: string) => k === "window" && r.type === "threshold" && DUR.test(String(p[k] ?? "").trim());
  for (const k of ["window", "reference", "lookback"]) if (k in p && !durS(String(p[k])) && !zeroOk(k)) out.push({ step: 2, msg: `The ${k === "window" ? "window" : k === "reference" ? "comparison period" : "look-back"} must be a duration (5m, 1h, 7d).` });
  if (durS(r.every) < 10) out.push({ step: 2, msg: "Check at most every 10 seconds." });
  if (!r.channels.length && !r.actions.incident) out.push({ step: 3, msg: "Nobody would know: pick a channel, or open an incident." });
  if (!r.name.trim()) out.push({ step: 4, msg: "Give the rule a name." });
  return out;
}

/** A name proposed from the rule — the person can keep it or change it. */
export function suggestName(r: RuleIn): string {
  const b = r.query.builder || {};
  const subj = (b.metric || b.text || b.index || "").trim();
  if (!subj) return "";
  return `${subj} — ${typeDef(r.type).label.toLowerCase()}`.slice(0, 80);
}

// ---------------------------------------------------------------- the chart's scales

export interface Scale { (v: number): number; domain: [number, number]; range: [number, number]; invert(px: number): number }

export function linear(domain: [number, number], range: [number, number]): Scale {
  const [d0, d1] = domain; const [r0, r1] = range;
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  const f = ((v: number) => (k === 0 ? (r0 + r1) / 2 : r0 + (v - d0) * k)) as Scale;
  f.domain = domain; f.range = range;
  f.invert = (px: number) => (k === 0 ? d0 : d0 + (px - r0) / k);
  return f;
}

/** « Nice » ticks: 1, 2, 2.5, 5 × 10^n steps covering [lo, hi], about `count` of them. */
export function niceTicks(lo: number, hi: number, count = 4): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  if (hi < lo) [lo, hi] = [hi, lo];
  if (hi === lo) hi = lo + 1;
  const raw = (hi - lo) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw * 0.999) ?? 10 * mag;
  const start = Math.floor(lo / step + 1e-9) * step;
  const out: number[] = [];
  for (let i = 0; i < 50; i++) {
    const v = Number((start + i * step).toFixed(10));
    out.push(v);
    if (v >= hi - step * 1e-9) break;
  }
  return out;
}

const vals = (pts: TV[]) => pts.map((p) => p[1]).filter((v): v is number => v !== null && Number.isFinite(v));

/** y domain that keeps every series AND the threshold in view, from 0 when all is ≥ 0, on nice ticks. */
export function yDomain(series: TV[][], ...lines: (number | null | undefined)[]): [number, number] {
  const vs = series.flatMap(vals);
  for (const l of lines) if (l !== null && l !== undefined && Number.isFinite(l)) vs.push(l);
  if (!vs.length) return [0, 1];
  let lo = Math.min(...vs); const hi0 = Math.max(...vs);
  if (lo >= 0) lo = 0;
  const hi = hi0 === lo ? lo + (Math.abs(lo) || 1) : hi0 + (hi0 - lo) * 0.08;
  const t = niceTicks(lo, hi, 4);
  return [Math.min(lo, t[0]), Math.max(hi, t[t.length - 1])];
}

/** Time ticks for a 1 h … 7 d window, aligned on round hours / days (UTC epoch, shown local). */
export function timeTicks(t0: number, t1: number): number[] {
  const span = t1 - t0;
  const step = span <= 2 * 3600 ? 900 : span <= 8 * 3600 ? 3600 : span <= 36 * 3600 ? 4 * 3600 : span <= 3 * 86400 ? 12 * 3600 : 86400;
  const first = Math.ceil(t0 / step) * step;
  const out: number[] = [];
  for (let t = first; t <= t1; t += step) out.push(t);
  return out;
}

/** SVG path of a line with gaps where the value is null (no data must show as a hole). */
export function linePath(pts: TV[], x: Scale, y: Scale): string {
  let d = ""; let pen = false;
  for (const [t, v] of pts) {
    if (v === null || !Number.isFinite(v)) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  }
  return d;
}

/** The same line closed down to y(base) — a soft area under the series. */
export function areaPath(pts: TV[], x: Scale, y: Scale, base = 0): string {
  const segs: TV[][] = []; let cur: TV[] = [];
  for (const p of pts) { if (p[1] === null || !Number.isFinite(p[1])) { if (cur.length) segs.push(cur); cur = []; } else cur.push(p); }
  if (cur.length) segs.push(cur);
  return segs.map((s) => `M${x(s[0][0]).toFixed(1)},${y(base).toFixed(1)}` +
    s.map(([t, v]) => `L${x(t).toFixed(1)},${y(v as number).toFixed(1)}`).join("") +
    `L${x(s[s.length - 1][0]).toFixed(1)},${y(base).toFixed(1)}Z`).join("");
}

const hits = (op: string, v: number, th: number) =>
  op === ">" ? v > th : op === ">=" ? v >= th : op === "<" ? v < th : op === "<=" ? v <= th : v === th;

/** Where a threshold rule would have fired while the person DRAGS the line: computed in the browser
 *  so the chart answers at once; the server's backtest (it knows « for », cooldown and groups)
 *  replaces it as soon as it answers. `forS` = how long the condition must hold. */
export function localFires(series: Series[], op: string, th: number, forS = 0): Interval[] {
  const out: Interval[] = [];
  for (const s of series) {
    let start: number | null = null; let last = 0; let peak: number | null = null;
    const close = () => {
      if (start !== null && last - start >= forS) out.push({ group_key: s.group_key ?? null, start: start + forS, end: last, peak });
      start = null; peak = null;
    };
    for (const [t, v] of s.points) {
      if (v !== null && Number.isFinite(v) && hits(op, v, th)) {
        if (start === null) start = t;
        last = t;
        peak = peak === null ? v : op.startsWith("<") ? Math.min(peak, v) : Math.max(peak, v);
      } else close();
    }
    close();
  }
  return out.sort((a, b) => a.start - b.start);
}

/** Merged fire intervals (several groups firing at once show as one band, counted per group). */
export function mergeIntervals(iv: Interval[], now?: number): { start: number; end: number; groups: number }[] {
  // end null = still firing at the end of the range → the band runs to `now` (was drawn as a 3 px tick)
  const s = [...iv].map((i) => ({ start: i.start, end: i.end ?? now ?? i.start })).sort((a, b) => a.start - b.start);
  const out: { start: number; end: number; groups: number }[] = [];
  for (const i of s) {
    const l = out[out.length - 1];
    if (l && i.start <= l.end) { l.end = Math.max(l.end, i.end); l.groups++; }
    else out.push({ ...i, groups: 1 });
  }
  return out;
}

/** Nearest point to a time — the hover layer's crosshair. */
export function nearest(pts: TV[], t: number): TV | null {
  let best: TV | null = null; let bd = Infinity;
  for (const p of pts) {
    if (p[1] === null) continue;
    const d = Math.abs(p[0] - t);
    if (d < bd) { bd = d; best = p; }
  }
  return best;
}

// ---------------------------------------------------------------- states, order, time

export const STATE_WORD: Record<RuleState | AlertState, { label: string; icon: string }> = {
  firing: { label: "Firing", icon: "▲" },
  pending: { label: "Pending", icon: "◔" },
  ok: { label: "OK", icon: "✓" },
  resolved: { label: "Resolved", icon: "✓" },
  silenced: { label: "Silenced", icon: "⏸" },
  error: { label: "Source error", icon: "!" },
  disabled: { label: "Off", icon: "○" },
  new: { label: "First check…", icon: "◌" },
};

const RANK: Record<RuleState, number> = { firing: 0, error: 1, pending: 2, silenced: 3, new: 4, ok: 4, disabled: 5 };
const SEV: Record<Severity, number> = { critical: 0, warning: 1, info: 2 };

/** A rule just saved has not been checked yet: « OK » would claim what nobody looked at (09.10 journey:
 *  « OK » next to a target that was down). */
export const ruleState = (r: Rule): RuleState => {
  const st = r.state?.state ?? (r.enabled ? "ok" : "disabled");
  return st === "ok" && r.enabled && !r.state?.last_eval ? "new" : st;
};

/** Templates to suggest first when a project has few rules: the ones every ops team wants, in this order. */
export const TEMPLATE_PRIORITY = ["target-down", "host-memory", "host-disk", "http-5xx-rate", "latency-p95", "host-cpu",
  "log-errors", "logs-flatline", "tls-expiry", "agent-run-failed", "project-budget"];
export function suggestTemplates<T extends { id: string; name: string; available: boolean }>(ts: T[], usedNames: string[], n = 3): T[] {
  const used = new Set(usedNames.map((x) => x.trim().toLowerCase()));
  const rank = (id: string) => { const i = TEMPLATE_PRIORITY.indexOf(id); return i < 0 ? 99 : i; };
  return ts.filter((t) => t.available && !used.has(t.name.trim().toLowerCase()))
    .sort((a, b) => rank(a.id) - rank(b.id)).slice(0, n);
}

/** Order of the rule list: what needs eyes first, then severity, then name. */
export function sortRules(rs: Rule[]): Rule[] {
  return [...rs].sort((a, b) => RANK[ruleState(a)] - RANK[ruleState(b)] || SEV[a.severity] - SEV[b.severity]
    || a.name.localeCompare(b.name));
}
/** Active alerts: firing before pending, critical first, oldest first. */
export function sortAlerts(as: Alert[]): Alert[] {
  const st: Record<AlertState, number> = { firing: 0, pending: 1, silenced: 2, resolved: 3 };
  return [...as].sort((a, b) => st[a.state] - st[b.state] || SEV[a.severity] - SEV[b.severity] || a.started_at - b.started_at);
}

/** « 12 min », « 3 h 5 min », « 2 d » */
/** « 3 min ago » / « just now » (not « just now ago »). */
export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  const s = since(ts, now);
  return s === "just now" || s === "—" ? s : `${s} ago`;
}

export function since(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "—";
  const s = Math.max(0, now - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) { const m = Math.floor((s % 3600) / 60); return `${Math.floor(s / 3600)} h${m ? ` ${m} min` : ""}`; }
  return `${Math.floor(s / 86400)} d`;
}

/** group {job: "api", host: "a"} → "job=api · host=a" */
export const groupText = (g: Record<string, string> | null | undefined, key?: string | null): string =>
  g && Object.keys(g).length ? Object.entries(g).map(([k, v]) => `${k}=${v}`).join(" · ") : (key || "");

/** "1h" of the preview range buttons → seconds, and the label */
export const RANGES: { id: "1h" | "6h" | "24h" | "7d"; label: string }[] = [
  { id: "1h", label: "1 h" }, { id: "6h", label: "6 h" }, { id: "24h", label: "24 h" }, { id: "7d", label: "7 days" },
];

/** Series to draw first: the groups that fired (else a target down all day — value 0 — was among the
 *  « +14 more groups (not drawn) » while the verdict said it fired), then the rest in server order. */
export function firedFirst(series: Series[], fires: Interval[]): Series[] {
  const hot = new Set(fires.map((f) => f.group_key ?? ""));
  return [...series].map((x, i) => ({ x, i })).sort((a, b) =>
    (hot.has(b.x.group_key ?? "") ? 1 : 0) - (hot.has(a.x.group_key ?? "") ? 1 : 0) || a.i - b.i).map((o) => o.x);
}

/** A group key short enough for a legend: the label values (« node · 100.76.30.90:9100 »), the full
 *  key stays in the tooltip. */
export function shortGroup(key: string | null | undefined): string {
  if (!key) return "series";
  const vals = key.split(",").map((kv) => kv.split("=").slice(1).join("=")).filter(Boolean);
  return vals.length ? vals.join(" · ") : key;
}

/** The unit of the watched value: % for a share (« as a percentage of ») or a labelled query
 *  ending in « (%) » (memory, CPU, disk templates) — so the line says « > 85 % », not « > 85 ». */
export function valueUnit(q: RuleQuery, unit?: string | null): string | null {
  if (unit) return unit;
  if (q.mode === "builder" && q.builder?.ratio_of) return "%";
  if (/\(%\)\s*$/.test(q.label || "")) return "%";
  if (q.mode === "raw" && !q.label) return describeRaw(q.raw)[1] || null;
  if (q.mode === "builder" && q.builder?.metric) {
    const m = q.builder.metric; const rate = q.builder.agg === "rate";
    if (/_bytes(_total)?$/.test(m)) return rate && m.endsWith("_total") ? "/s" : "bytes";
    if (/_seconds(_total)?$/.test(m)) return "s";
    if (rate && m.endsWith("_total")) return "/s";
  }
  return null;
}

const OP_SIGN: Record<string, string> = { ">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "=" };

/** The value line of an alert, split for emphasis: {value: "77.8 %", rest: "> 50 %"} · {value: "down"}.
 *  The server's `value_text` when it has one, else built from value / threshold / unit. */
export function alertValueText(a: Pick<Alert, "value" | "threshold" | "value_text" | "unit">): { value: string; rest: string } | null {
  const t = (a.value_text || "").trim();
  if (t) {
    const m = /^(.+?)\s+([<>≤≥=]\s.+|\(.+\)|in\s.+)$/.exec(t);
    return m ? { value: m[1], rest: m[2] } : { value: t, rest: "" };
  }
  if (a.value === null || a.value === undefined) return null;
  const u = a.unit || null;
  const th = a.threshold;
  if (th && (th.op === "<" || th.op === "<=") && th.value === 1 && !u && a.value === 0) return { value: "down", rest: "" };
  return { value: fmtValue(a.value, u), rest: th && typeof th.value === "number" && th.op ? `${OP_SIGN[th.op] ?? th.op} ${fmtValue(th.value, u)}` : "" };
}

/** The value column of a rule row: « 77.8 % », « down » (a target rule at 0), « no data » (absence). */
export function ruleValueText(r: Pick<Rule, "type" | "params" | "state">, unit?: string | null): string | null {
  const v = r.state?.last_value;
  if (r.type === "absence" && (r.state?.firing ?? 0) > 0) return "no data";
  if (v === null || v === undefined) return null;
  const op = String(r.params?.op ?? ""); const th = Number(r.params?.value);
  if ((op === "<" || op === "<=") && th === 1 && !unit && v === 0) {
    const n = r.state?.firing ?? 0;
    return n > 1 ? `${n} down` : "down";
  }
  return fmtValue(v, unit);
}

/** The name of a group for a legend / an alert row: the server's « rpi1 · node » when it has one. */
export function groupTitle(x: { group_title?: string | null; group_key?: string | null } | null | undefined): string {
  return (x?.group_title || "").trim() || shortGroup(x?.group_key);
}

/** The tooltip of a group: every label with its raw value (« instance=100.76.30.90:9100 · job=node »). */
export function groupTip(x: { group?: Record<string, string> | null; group_key?: string | null } | null | undefined): string {
  return groupText(x?.group, x?.group_key);
}
