"use client";
// 3.5 — the visual query builders of the alert wizard: nobody has to write PromQL, LogQL or Lucene.
// Prometheus: metric + label filters + what to compute (+ « as a share of all »);
// Loki: label filters + text + level; Elasticsearch / OpenSearch: index + field filters + text;
// SOKKAN: one of the cockpit's own figures. The raw editor stays one click away for experts.
import { useCallback, useState } from "react";
import { suggest } from "@/lib/alerting";
import type { AlertSource, Builder, Filter, RuleQuery } from "@/lib/alertingModel";
import { Combo, DurInput, Field, Seg, btn, inputCls } from "./bits";

const AGGS: { id: NonNullable<Builder["agg"]>; label: string; title: string }[] = [
  { id: "rate", label: "per second", title: "rate(): how fast a counter grows, per second" },
  { id: "increase", label: "count in window", title: "increase(): how much a counter grew over the window" },
  { id: "last", label: "current value", title: "the value as it is (gauges: CPU %, disk, temperature…)" },
  { id: "avg", label: "average", title: "average over the window" },
  { id: "max", label: "max", title: "highest over the window" },
  { id: "sum", label: "sum", title: "sum over the window" },
];

const PROM_OPS = ["=", "!=", "=~", "!~"];
const ES_OPS = ["=", "!=", "contains", "exists"];

export function QueryBuilder({ src, q, onChange }: { src: AlertSource | undefined; q: RuleQuery; onChange: (q: RuleQuery) => void }) {
  const kind = src?.kind;
  const b = q.builder || {};
  const setB = (p: Partial<Builder>) => onChange({ ...q, builder: { ...b, ...p } });
  const sid = src?.id;
  const sug = useCallback((k: "metrics" | "labels" | "values" | "fields", o: { metric?: string; label?: string; field?: string } = {}) =>
    (qq: string) => (sid === undefined ? Promise.resolve([]) : suggest(sid, k, { ...o, q: qq })), [sid]);

  if (!src) return <div className="text-[12px] text-mut">Pick a source first.</div>;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Seg label="query editor" size="sm" value={q.mode}
          onChange={(m) => onChange({ ...q, mode: m, raw: m === "raw" && !q.raw ? "" : q.raw })}
          opts={[{ id: "builder", label: "Visual" }, { id: "raw", label: kind === "prometheus" ? "PromQL" : kind === "loki" ? "LogQL" : kind === "sokkan" ? "Filter" : "Query" }]} />
        {q.mode === "raw" && <span className="text-[10.5px] text-mut">for experts — the visual builder covers most rules</span>}
      </div>

      {q.mode === "raw" ? (
        <Field label={kind === "prometheus" ? "PromQL expression" : kind === "loki" ? "LogQL query" : kind === "sokkan" ? "SOKKAN filter" : "Lucene query or JSON DSL"} htmlFor="raw-q"
          hint={kind === "prometheus" ? <>e.g. <code className="font-mono">sum(rate(http_requests_total{"{"}status=~&quot;5..&quot;{"}"}[5m]))</code></>
            : kind === "loki" ? <>e.g. <code className="font-mono">{"{"}app=&quot;api&quot;{"}"} |= &quot;ERROR&quot;</code></>
            : kind === "sokkan" ? <>e.g. <code className="font-mono">audit action=agent.run.failed</code>, <code className="font-mono">cost.day</code>, <code className="font-mono">sessions.waiting</code></>
            : <>e.g. <code className="font-mono">level:error AND service:api</code> — or a JSON query starting with <code>{"{"}</code></>}>
          <textarea id="raw-q" value={q.raw} onChange={(e) => onChange({ ...q, raw: e.target.value })} rows={4} spellCheck={false}
            className={`${inputCls} resize-y font-mono text-[12px]`} />
        </Field>
      ) : kind === "prometheus" ? (
        <PromBuilder b={b} setB={setB} sug={sug} />
      ) : kind === "loki" ? (
        <LogsBuilder b={b} setB={setB} sug={sug} />
      ) : kind === "elasticsearch" || kind === "opensearch" ? (
        <EsBuilder b={b} setB={setB} sug={sug} defIndex={src.options?.index} />
      ) : kind === "sokkan" ? (
        <Field label="Figure to watch" htmlFor="sk-m" hint="the cockpit's own figures: cost of the day, failed agent runs, sessions waiting for a human, audit events…">
          <Combo id="sk-m" label="figure" value={b.metric || ""} onChange={(v) => setB({ metric: v })} fetcher={sug("metrics")} placeholder="cost.day" />
        </Field>
      ) : (
        <div className="text-[12px] text-mut">This source receives alerts sent by another tool (Grafana, a CI hook…): no query — every alert it sends matches.</div>
      )}
    </div>
  );
}

type Sug = (k: "metrics" | "labels" | "values" | "fields", o?: { metric?: string; label?: string; field?: string }) => (q: string) => Promise<string[]>;

function Filters({ fs, onChange, keyName, ops, sugKey, sugVal, keyLabel }: {
  fs: Filter[]; onChange: (f: Filter[]) => void; keyName: "label" | "field"; ops: string[];
  sugKey: (q: string) => Promise<string[]>; sugVal: (key: string) => (q: string) => Promise<string[]>; keyLabel: string;
}) {
  const set = (i: number, p: Partial<Filter>) => onChange(fs.map((f, j) => (j === i ? { ...f, ...p } : f)));
  return (
    <div className="space-y-1.5">
      {fs.map((f, i) => {
        const k = (f[keyName] || "") as string;
        return (
          <div key={i} className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto] items-center gap-1.5">
            <Combo label={`${keyLabel} ${i + 1}`} value={k} onChange={(v) => set(i, { [keyName]: v } as Partial<Filter>)} fetcher={sugKey} placeholder={keyLabel} />
            <select aria-label={`operator ${i + 1}`} value={f.op} onChange={(e) => set(i, { op: e.target.value })} className={`${inputCls} w-auto font-mono`}>
              {ops.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            {f.op === "exists" ? <span className="text-[11px] text-mut">has any value</span> : (
              <Combo label={`value ${i + 1}`} value={f.value} onChange={(v) => set(i, { value: v })} fetcher={k ? sugVal(k) : undefined} placeholder="value" />
            )}
            <button type="button" onClick={() => onChange(fs.filter((_, j) => j !== i))} aria-label={`remove filter ${i + 1}`}
              className="ui-focus rounded px-1.5 text-[14px] leading-none text-mut hover:text-red-300">×</button>
          </div>
        );
      })}
      <button type="button" className={btn.small} onClick={() => onChange([...fs, { [keyName]: "", op: ops[0], value: "" } as Filter])}>+ filter</button>
    </div>
  );
}

function Chips({ items, onChange, fetcher, label, placeholder }: { items: string[]; onChange: (v: string[]) => void; fetcher: (q: string) => Promise<string[]>; label: string; placeholder: string }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {items.map((it) => (
        <span key={it} className="inline-flex items-center gap-1 rounded-full border border-line bg-panel2 px-2 py-0.5 font-mono text-[11px] text-slate-200">
          {it}<button type="button" aria-label={`remove ${it}`} onClick={() => onChange(items.filter((x) => x !== it))} className="ui-focus text-mut hover:text-red-300">×</button>
        </span>
      ))}
      <div className="w-40">
        <AddCombo label={label} placeholder={placeholder} fetcher={fetcher} onAdd={(v) => !items.includes(v) && onChange([...items, v])} />
      </div>
    </div>
  );
}

function AddCombo({ label, placeholder, fetcher, onAdd }: { label: string; placeholder: string; fetcher: (q: string) => Promise<string[]>; onAdd: (v: string) => void }) {
  const [v, setV] = useState("");
  return <Combo label={label} value={v} placeholder={placeholder} fetcher={fetcher} onChange={setV}
    onPick={(x) => { if (x.trim()) onAdd(x.trim()); setTimeout(() => setV(""), 0); }} />;
}

function PromBuilder({ b, setB, sug }: { b: Builder; setB: (p: Partial<Builder>) => void; sug: Sug }) {
  const m = b.metric || "";
  const counter = /_(total|count|sum|bucket)$/.test(m);
  const pct = !!b.ratio_of;
  return (
    <div className="space-y-3">
      <Field label="Metric" htmlFor="pm-metric" hint={m && counter && b.agg !== "rate" && b.agg !== "increase" ? "This looks like a counter (it only grows): « per second » or « count in window » is usually what you want." : "start typing — the list comes from your Prometheus"}>
        <Combo id="pm-metric" label="metric" value={m} onChange={(v) => setB({ metric: v, ...(b.ratio_of ? { ratio_of: { ...b.ratio_of, metric: v } } : {}) })} fetcher={sug("metrics")} placeholder="http_requests_total" />
      </Field>
      <Field label="Only where" hint="narrow to a service, a status, a host…">
        <Filters fs={b.filters || []} onChange={(f) => setB({ filters: f })} keyName="label" ops={PROM_OPS} keyLabel="label"
          sugKey={sug("labels", { metric: m })} sugVal={(k) => sug("values", { metric: m, label: k })} />
      </Field>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
        <Field label="Compute">
          <div className="flex flex-wrap gap-1">
            {AGGS.map((a) => (
              <button key={a.id} type="button" title={a.title} aria-pressed={(b.agg || "last") === a.id} onClick={() => setB({ agg: a.id })}
                className={`ui-focus rounded-md border px-2 py-1 text-[11.5px] ${(b.agg || "last") === a.id ? "border-sea bg-sea/15 text-slate-100" : "border-line text-mut hover:text-slate-200"}`}>{a.label}</button>
            ))}
          </div>
        </Field>
        {b.agg && b.agg !== "last" && (
          <Field label="over" htmlFor="pm-range"><DurInput id="pm-range" label="window" value={b.range || "5m"} onChange={(v) => setB({ range: v })} /></Field>
        )}
      </div>
      <label className="flex cursor-pointer items-start gap-2 text-[12px] text-slate-200">
        <input type="checkbox" className="ui-focus mt-0.5 accent-sky-500" checked={pct}
          onChange={(e) => setB({ ratio_of: e.target.checked ? { metric: m, filters: (b.filters || []).filter((f) => !/status|code/.test(f.label || "")), agg: b.agg, range: b.range } : null })} />
        <span>As a percentage of all <span className="font-mono text-[11px]">{m || "requests"}</span>
          <span className="block text-[10.5px] text-mut">e.g. the share of 5xx among all requests — the threshold is then in %</span></span>
      </label>
      {pct && b.ratio_of && (
        <Field label="…out of (denominator filters)" hint="leave empty for « all of them »">
          <Filters fs={b.ratio_of.filters || []} onChange={(f) => setB({ ratio_of: { ...b.ratio_of, filters: f } })} keyName="label" ops={PROM_OPS} keyLabel="label"
            sugKey={sug("labels", { metric: m })} sugVal={(k) => sug("values", { metric: m, label: k })} />
        </Field>
      )}
      <Field label="One alert per" hint="optional — one alert for each service / host instead of one for everything">
        <Chips items={b.by || []} onChange={(v) => setB({ by: v })} fetcher={sug("labels", { metric: m })} label="group by label" placeholder="+ label (job, instance…)" />
      </Field>
    </div>
  );
}

const LEVELS = ["", "error", "warn", "info", "debug", "fatal"];

function LogsBuilder({ b, setB, sug }: { b: Builder; setB: (p: Partial<Builder>) => void; sug: Sug }) {
  return (
    <div className="space-y-3">
      <Field label="Which logs" hint="the streams to read: app, job, container, host…">
        <Filters fs={b.filters || []} onChange={(f) => setB({ filters: f })} keyName="label" ops={PROM_OPS} keyLabel="label"
          sugKey={sug("labels")} sugVal={(k) => sug("values", { label: k })} />
      </Field>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_10rem]">
        <Field label="Line contains" htmlFor="lk-text" hint="plain text — case matters">
          <input id="lk-text" value={b.text || ""} onChange={(e) => setB({ text: e.target.value })} placeholder="ERROR, timeout, panic…" className={`${inputCls} font-mono`} />
        </Field>
        <Field label="Level" htmlFor="lk-level">
          <select id="lk-level" value={b.level || ""} onChange={(e) => setB({ level: e.target.value })} className={inputCls}>
            {LEVELS.map((l) => <option key={l} value={l}>{l || "any level"}</option>)}
          </select>
        </Field>
      </div>
    </div>
  );
}

function EsBuilder({ b, setB, sug, defIndex }: { b: Builder; setB: (p: Partial<Builder>) => void; sug: Sug; defIndex?: string }) {
  return (
    <div className="space-y-3">
      <Field label="Index" htmlFor="es-index" hint={defIndex ? `empty = the source's default (${defIndex})` : "index or pattern, e.g. logs-*"}>
        <input id="es-index" value={b.index || ""} onChange={(e) => setB({ index: e.target.value })} placeholder={defIndex || "logs-*"} className={`${inputCls} font-mono`} />
      </Field>
      <Field label="Only documents where">
        <Filters fs={b.filters || []} onChange={(f) => setB({ filters: f })} keyName="field" ops={ES_OPS} keyLabel="field"
          sugKey={sug("fields")} sugVal={(k) => sug("values", { field: k })} />
      </Field>
      <Field label="Text anywhere" htmlFor="es-text" hint="full-text search in the message">
        <input id="es-text" value={b.text || ""} onChange={(e) => setB({ text: e.target.value })} placeholder="connection refused" className={inputCls} />
      </Field>
    </div>
  );
}

/** The field picker of new_term / change / cardinality (log and ES sources). */
export function FieldPicker({ src, value, onChange, label, id }: { src: AlertSource | undefined; value: string; onChange: (v: string) => void; label: string; id: string }) {
  const sid = src?.id;
  const kind = src?.kind === "loki" ? "labels" : "fields";
  const f = useCallback((q: string) => (sid === undefined ? Promise.resolve([]) : suggest(sid, kind, { q })), [sid, kind]);
  return <Combo id={id} label={label} value={value} onChange={onChange} fetcher={f} placeholder={src?.kind === "loki" ? "label" : "field.name"} />;
}
