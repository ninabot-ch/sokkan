"use client";
// 3.5 — the alert wizard: four short steps on one page — What to watch · When to alert · Who to tell
// · Then — with the backtest chart beside the form the whole time (« it would have fired 3 times
// yesterday »), the threshold draggable on the chart, and the rule said in one plain sentence.
// Free navigation between steps; « Save » lists what is missing, with a link to the step.
import { useEffect, useMemo, useRef, useState } from "react";
import { createRule, preview, testChannel, updateRule } from "@/lib/alerting";
import {
  valueUnit,
  RANGES, defaultChannels, durS, hasQuery, isMetricSource, namesFrom, ruleSentence, suggestName, switchSource, switchType, typesFor,
  validateRule, type AlertSource, type Channel, type ChannelKindDef, type Param, type PreviewResult, type Rule, type RuleIn,
  type RuleType, type Severity,
} from "@/lib/alertingModel";
import { FieldPicker, QueryBuilder } from "./Builders";
import Chart from "./Chart";
import { ChannelBadge, ChannelEditor, StatusDot } from "./Settings";
import { Banner, DurInput, Field, Seg, SeverityChip, Toggle, TypeArt, btn, inputCls } from "./bits";

const STEPS = [
  { n: 1, label: "What to watch" }, { n: 2, label: "When to alert" }, { n: 3, label: "Who to tell" }, { n: 4, label: "Then" },
];

/** Which parameter the threshold line on the chart stands for, per type (drag = edit it). */
const LINE: Partial<Record<RuleType, { key: string; op?: string }>> = {
  threshold: { key: "value" }, frequency: { key: "count", op: ">=" }, flatline: { key: "count", op: "<" }, cardinality: { key: "value" },
};

export interface WizardProps {
  start: RuleIn;
  /** editing an existing rule */
  ruleId?: number;
  sources: AlertSource[];
  channels: Channel[];
  kinds: ChannelKindDef[];
  agents: { id: number; name: string }[];
  runbooks: { name: string; description?: string }[];
  canManage: boolean;
  fromTemplate?: string | null;
  onChannelsChanged: () => void;
  onSaved: (r: Rule) => void;
  onCancel: () => void;
}

export default function Wizard(p: WizardProps) {
  // a new rule tells the project's channel when there is exactly one: the 09.10 journey saved its
  // second rule with nobody to tell because the only channel was not ticked
  const [r, setR] = useState<RuleIn>(() =>
    // 3.5.1: every channel of the project, not only when there is exactly one (2 channels in prod → none ticked)
    !p.start.channels.length && !p.ruleId && !p.start.no_notification ? { ...p.start, channels: defaultChannels(p.channels) } : p.start);
  // channels that arrive after the wizard opened are ticked too (only while nothing was chosen)
  useEffect(() => {
    if (!p.ruleId && p.channels.length) setR((x) => (x.channels.length || x.no_notification ? x : { ...x, channels: defaultChannels(p.channels) }));
  }, [p.channels, p.ruleId]);
  const [step, setStep] = useState(1);
  const [range, setRange] = useState<"1h" | "6h" | "24h" | "7d">("24h");
  const [pv, setPv] = useState<PreviewResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [tried, setTried] = useState(false);
  const [nameTouched, setNameTouched] = useState(!!p.start.name);
  const src = p.sources.find((s) => s.id === r.source_id);
  const kind = src?.kind;
  const unit = pv?.unit || valueUnit(r.query);
  const problems = validateRule(r, kind);
  // the server's sentence resolves host names (« except rpi1 »); the local one speaks while it computes
  const sentence = (!loading && pv?.sentence) ? `${pv.sentence.replace(/\.$/, "")}.` : ruleSentence(r, kind, unit, namesFrom(pv?.series));

  // propose a name as long as the person did not write one
  useEffect(() => { if (!nameTouched) setR((x) => ({ ...x, name: suggestName(x) })); }, [nameTouched, r.query, r.type]);

  // live backtest, debounced; the latest answer wins
  const seq = useRef(0);
  const key = useMemo(() => JSON.stringify([r.source_id, r.query, r.type, r.params, r.for, r.group_by, r.every, range]), [r, range]);
  useEffect(() => {
    if (!hasQuery(r, kind) || r.source_id === null || kind === "external") { setPv(null); return; }
    const my = ++seq.current;
    setLoading(true);
    const t = setTimeout(() => {
      preview(r, range).then((x) => { if (my === seq.current) setPv(x); })
        .catch((e) => { if (my === seq.current) setPv({ kind: "metric", series: [], fired_intervals: [], fires: 0, error: String(e.message || e) }); })
        .finally(() => { if (my === seq.current) setLoading(false); });
    }, 550);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const setParam = (k: string, v: Param) => setR((x) => ({ ...x, params: { ...x.params, [k]: v } }));
  const line = LINE[r.type];
  const th = line && typeof r.params[line.key] === "number" ? { op: line.op ?? String(r.params.op || ">"), value: r.params[line.key] as number } : null;

  const save = async () => {
    setTried(true);
    if (problems.length) { setStep(problems[0].step); return; }
    setSaving(true); setErr("");
    try { p.onSaved(p.ruleId ? await updateRule(p.ruleId, r) : await createRule(r)); }
    catch (e) { setErr(String((e as Error).message || e)); } finally { setSaving(false); }
  };

  const stepProblems = (n: number) => problems.filter((x) => x.step === n);

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" onClick={p.onCancel} className={btn.small}>← Alerts</button>
        <h2 className="text-[14px] font-semibold text-slate-100">{p.ruleId ? `Edit « ${p.start.name} »` : "New alert rule"}</h2>
        {p.fromTemplate && <span className="text-[11px] text-mut">from the template « {p.fromTemplate} »</span>}
      </div>

      {/* stepper */}
      <ol className="mb-3 grid grid-cols-2 gap-1.5 sm:grid-cols-4" aria-label="steps">
        {STEPS.map((s) => {
          const bad = tried && stepProblems(s.n).length > 0;
          const cur = s.n === step;
          return (
            <li key={s.n}>
              <button type="button" onClick={() => setStep(s.n)} aria-current={cur ? "step" : undefined}
                className={`ui-focus flex w-full items-center gap-2 rounded-lg border px-2 py-1.5 text-left ${cur ? "border-sea bg-sea/10" : "border-line hover:border-sea/40"}`}>
                <span className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${bad ? "bg-red-500/80 text-white" : cur ? "bg-sea text-white" : s.n < step ? "bg-emerald-500/70 text-white" : "bg-panel2 text-mut ring-1 ring-line"}`}>
                  {bad ? "!" : s.n < step ? "✓" : s.n}
                </span>
                <span className={`truncate text-[12px] ${cur ? "text-slate-100" : "text-mut"}`}>{s.label}</span>
              </button>
            </li>
          );
        })}
      </ol>

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
        {/* the form of the step */}
        <section aria-label={STEPS[step - 1].label} className="min-w-0 space-y-3 rounded-xl border border-line bg-panel/50 p-3">
          {step === 1 && <StepWhat r={r} setR={setR} sources={p.sources} />}
          {step === 2 && <StepWhen r={r} setR={setR} src={src} setParam={setParam} />}
          {step === 3 && <StepWho r={r} setR={setR} channels={p.channels} kinds={p.kinds} canManage={p.canManage} onChannelsChanged={p.onChannelsChanged} />}
          {step === 4 && <StepThen r={r} setR={setR} agents={p.agents} runbooks={p.runbooks} onName={() => setNameTouched(true)} />}
          {tried && stepProblems(step).length > 0 && (
            <Banner tone="error"><ul className="list-inside list-disc">{stepProblems(step).map((x) => <li key={x.msg}>{x.msg}</li>)}</ul></Banner>
          )}
          <div className="flex items-center gap-2 border-t border-line pt-3">
            {step > 1 && <button type="button" className={btn.ghost} onClick={() => setStep(step - 1)}>Back</button>}
            {step < 4 ? (
              <button type="button" className={`${btn.primary} ml-auto`} onClick={() => setStep(step + 1)}>Next: {STEPS[step].label}</button>
            ) : (
              <button type="button" className={`${btn.primary} ml-auto`} onClick={save} disabled={saving}>
                {saving ? "Saving…" : p.ruleId ? "Save the rule" : r.enabled ? "Save and turn on" : "Save (off)"}
              </button>
            )}
          </div>
          {err && <Banner tone="error">{err}</Banner>}
        </section>

        {/* the chart + the sentence, always in view */}
        <aside className="min-w-0 space-y-2 lg:sticky lg:top-2 lg:self-start" aria-label="preview">
          <div className="rounded-xl border border-line bg-panel/50 p-3">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-[12px] font-semibold text-slate-200">Backtest</span>
              {src && <span className="flex items-center gap-1 text-[11px] text-mut"><StatusDot ok={src.status?.ok} />{src.name}</span>}
              <span className="ml-auto"><Seg label="time range" size="sm" value={range} onChange={setRange} opts={RANGES.map((x) => ({ id: x.id, label: x.label }))} /></span>
            </div>
            <Chart result={pv} loading={loading} unit={unit} forS={durS(r.for)} rangeLabel={RANGES.find((x) => x.id === range)?.label}
              threshold={th} onThreshold={line ? (v) => setParam(line.key, line.key === "count" ? Math.max(1, Math.round(v)) : v) : undefined}
              idle={kind === "external" ? "Alerts sent by another tool have no backtest: each one that arrives fires." : step === 1 ? "Pick a metric or describe the logs: the data of the last 24 h shows here." : undefined} />
          </div>
          <div className="rounded-xl border border-line bg-panel2/40 p-3">
            <div className="mb-1 flex items-center gap-2 text-[11px] uppercase tracking-wide text-mut">In plain words <SeverityChip s={r.severity} /></div>
            <p className="text-[13.5px] leading-snug text-slate-100">{sentence}</p>
            {(pv?.query_compiled || (r.query.mode === "raw" && r.query.raw)) && (
              <details className="mt-1.5">
                <summary className="ui-focus cursor-pointer text-[11px] text-mut hover:text-slate-300">query sent to {src?.name || "the source"}</summary>
                <code className="mt-1 block whitespace-pre-wrap break-all rounded bg-ink/70 p-2 font-mono text-[11px] text-slate-300">{pv?.query_compiled || r.query.raw}</code>
              </details>
            )}
            {(pv?.sample_events || []).length > 0 && (
              <details className="mt-1.5" open={r.type === "any" || r.type === "new_term" || r.type === "change"}>
                <summary className="ui-focus cursor-pointer text-[11px] text-mut hover:text-slate-300">matching events ({pv?.sample_events?.length})</summary>
                <ul className="mt-1 space-y-0.5">
                  {pv?.sample_events?.slice(0, 5).map((e, i) => (
                    <li key={i} className="truncate rounded bg-ink/60 px-2 py-0.5 font-mono text-[10.5px] text-slate-300" title={e.text}>
                      <span className="text-mut">{new Date(e.ts * 1000).toLocaleTimeString()}</span> {e.text}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- step 1 — what to watch

const KIND_WORD: Record<string, string> = {
  prometheus: "metrics", loki: "logs", elasticsearch: "logs & documents", opensearch: "logs & documents", sokkan: "cockpit figures", external: "alerts from other tools",
};

function StepWhat({ r, setR, sources }: { r: RuleIn; setR: (f: (x: RuleIn) => RuleIn) => void; sources: AlertSource[] }) {
  const src = sources.find((s) => s.id === r.source_id);
  return (
    <>
      <Field label="Where the data comes from">
        {/* 3.5.1: native radios — the button + role="radio" cards were read « not checked » when checked */}
        <div className="grid gap-1.5 sm:grid-cols-2" role="radiogroup" aria-label="data source">
          {sources.map((s) => {
            const on = s.id === r.source_id;
            return (
              <label key={s.id}
                className={`flex cursor-pointer items-center gap-2 rounded-lg border px-2.5 py-2 text-left has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-blue-500 ${on ? "border-sea bg-sea/10" : "border-line hover:border-sea/40"}`}>
                <input type="radio" name="w-source" value={s.id} checked={on} onChange={() => setR((x) => switchSource(x, s))} className="sr-only" />
                <StatusDot ok={s.status?.ok} title={s.status?.error || undefined} />
                <span className="min-w-0">
                  <span className="block truncate text-[12.5px] text-slate-100">{s.name}</span>
                  <span className="block truncate text-[10.5px] text-mut">{KIND_WORD[s.kind] || s.kind}{s.status?.ok === false ? " · unreachable" : ""}</span>
                </span>
              </label>
            );
          })}
        </div>
      </Field>
      {src?.status?.ok === false && <Banner tone="warn">This source does not answer right now{src.status.error ? ` (${src.status.error})` : ""}. You can still write the rule; it will show « source error » until it answers.</Banner>}
      {src && (
        <div className="border-t border-line pt-3">
          <QueryBuilder src={src} q={r.query} onChange={(q) => setR((x) => ({
            ...x, query: q,
            // one alert per label: the builder's « by » IS the rule's grouping on a metric source
            ...(isMetricSource(src.kind) && q.mode === "builder" ? { group_by: [...(q.builder.by || [])] } : {}),
          }))} />
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------- step 2 — when to alert

function StepWhen({ r, setR, src, setParam }: {
  r: RuleIn; setR: (f: (x: RuleIn) => RuleIn) => void; src: AlertSource | undefined; setParam: (k: string, v: Param) => void;
}) {
  const types = typesFor(src?.kind);
  const p = r.params;
  const num = (k: string, label: string, opts: { min?: number; step?: number; suffix?: string; hint?: string } = {}) => (
    <Field label={label} htmlFor={`p-${k}`} hint={opts.hint}>
      <div className="flex items-center gap-1.5">
        <input id={`p-${k}`} type="number" inputMode="decimal"
          // Chrome changes a focused number field on the mouse wheel: scrolling the page with the cursor
          // over it moved the threshold 85 → 42 in the 09.10 journey without anyone noticing
          onWheel={(e) => e.currentTarget.blur()} min={opts.min} step={opts.step ?? "any"} value={p[k] === null || p[k] === undefined ? "" : String(p[k])}
          onChange={(e) => setParam(k, e.target.value === "" ? null : Number(e.target.value))} className={`${inputCls} w-28 text-right tabular-nums`} />
        {opts.suffix && <span className="text-[12px] text-mut">{opts.suffix}</span>}
      </div>
    </Field>
  );
  const dur = (k: string, label: string, hint?: string) => (
    <Field label={label} htmlFor={`p-${k}`} hint={hint}><DurInput id={`p-${k}`} label={label} value={String(p[k] ?? "5m")} onChange={(v) => setParam(k, v)} /></Field>
  );
  const unit = valueUnit(r.query) ?? undefined;
  const logs = !!src && !isMetricSource(src.kind);
  return (
    <>
      <Field label="Alert when…">
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2" role="radiogroup" aria-label="rule type">
          {types.map((t) => {
            const on = t.id === r.type;
            return (
              <label key={t.id}
                className={`flex cursor-pointer items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-blue-500 ${on ? "border-sea bg-sea/10" : "border-line hover:border-sea/40"}`}>
                <input type="radio" name="w-type" value={t.id} checked={on} onChange={() => setR((x) => switchType(x, t.id))} className="sr-only" />
                <TypeArt t={t.id} active={on} />
                <span className="min-w-0">
                  <span className={`block text-[12.5px] ${on ? "text-slate-100" : "text-slate-200"}`}>{t.label}</span>
                  <span className="block text-[10.5px] leading-snug text-mut">{t.hint}</span>
                </span>
              </label>
            );
          })}
        </div>
      </Field>

      <div className="grid gap-3 border-t border-line pt-3 sm:grid-cols-2">
        {r.type === "threshold" && (<>
          <Field label="Is" htmlFor="p-op">
            <select id="p-op" value={String(p.op || ">")} onChange={(e) => setParam("op", e.target.value)} className={inputCls}>
              <option value=">">above</option><option value=">=">at least</option><option value="<">below</option><option value="<=">at most</option><option value="==">exactly</option>
            </select>
          </Field>
          {num("value", "Threshold", { suffix: unit, hint: "or drag the line on the chart" })}
          {logs && dur("window", "Events counted per", "the number of matching lines in each window is compared")}
          {!logs && r.query.mode === "raw" && (
            <Field label="Using the" htmlFor="p-reduce" hint="how the window's values become one number">
              <select id="p-reduce" value={String(p.reduce || "last")} onChange={(e) => setParam("reduce", e.target.value)} className={inputCls}>
                <option value="last">latest value</option><option value="avg">average</option><option value="max">highest</option><option value="min">lowest</option><option value="sum">sum</option>
              </select>
            </Field>
          )}
        </>)}
        {r.type === "frequency" && (<>{num("count", "At least", { min: 1, step: 1, suffix: "events", hint: "or drag the line on the chart" })}{dur("window", "Within")}</>)}
        {r.type === "flatline" && (<>{num("count", "Fewer than", { min: 1, step: 1, suffix: "events" })}{dur("window", "Within", "e.g. no log line for 10 min = the app or the shipper stopped")}</>)}
        {r.type === "spike" && (<>
          {num("ratio", "Factor", { min: 1, step: 0.5, suffix: "×", hint: "×3 = three times the usual rate" })}
          <Field label="Direction"><Seg label="spike direction" size="sm" value={String(p.direction || "up") as "up" | "down" | "both"} onChange={(v) => setParam("direction", v)}
            opts={[{ id: "up", label: "↑ up" }, { id: "down", label: "↓ down" }, { id: "both", label: "↕ both" }]} /></Field>
          {dur("window", "Recent window")}{dur("reference", "Compared with the last", "the usual level")}
          {num("min_count", "Ignore under", { min: 0, step: 1, suffix: "events", hint: "no alert for 1 → 4 errors at night" })}
        </>)}
        {r.type === "absence" && dur("window", "No data for", "a scrape target down, an exporter gone, a job that stopped reporting")}
        {r.type === "new_term" && (<>
          <Field label="Field" htmlFor="p-field"><FieldPicker id="p-field" src={src} label="field" value={String(p.field || "")} onChange={(v) => setParam("field", v)} /></Field>
          {dur("lookback", "Not seen in the last", "values seen in this period are « known »")}
        </>)}
        {r.type === "change" && (<>
          <Field label="Field that changes" htmlFor="p-field"><FieldPicker id="p-field" src={src} label="field" value={String(p.field || "")} onChange={(v) => setParam("field", v)} /></Field>
          <Field label="For the same" htmlFor="p-key" hint="e.g. status of the same host"><FieldPicker id="p-key" src={src} label="key field" value={String(p.key_field || "")} onChange={(v) => setParam("key_field", v)} /></Field>
        </>)}
        {r.type === "cardinality" && (<>
          <Field label="Distinct values of" htmlFor="p-field"><FieldPicker id="p-field" src={src} label="field" value={String(p.field || "")} onChange={(v) => setParam("field", v)} /></Field>
          <Field label="Is"><Seg label="more or fewer" size="sm" value={String(p.op || ">") as ">" | "<"} onChange={(v) => setParam("op", v)} opts={[{ id: ">", label: "more than" }, { id: "<", label: "fewer than" }]} /></Field>
          {num("value", "Count", { min: 0, step: 1 })}{dur("window", "Within")}
        </>)}
        {r.type === "anomaly" && (<>
          {num("z", "Sensitivity", { min: 1, step: 0.5, suffix: "σ", hint: "3 = rare (about 3 in 1000 points); 2 = more alerts" })}
          {dur("lookback", "Usual level of the last")}
        </>)}
        {r.type === "any" && <p className="text-[12px] text-mut sm:col-span-2">Every matching event alerts — keep the query narrow (a text, a level, a service). A reminder pause (step 3) keeps a burst to one message.</p>}
      </div>

      <details className="rounded-lg border border-line px-2.5 py-2" open={durS(r.for) > 0 && r.type === "threshold"}>
        <summary className="ui-focus cursor-pointer text-[12px] text-slate-300">Timing{logs ? " and grouping" : ""} <span className="text-mut">— check every {r.every}, hold {r.for || "0s"}</span></summary>
        <div className="mt-2.5 grid gap-3 sm:grid-cols-2">
          <Field label="Check every" htmlFor="p-every"><DurInput id="p-every" label="check every" min={10} value={r.every} onChange={(v) => setR((x) => ({ ...x, every: v }))} /></Field>
          {r.type !== "any" && r.type !== "new_term" && r.type !== "change" && (
            <Field label="Only if it lasts" htmlFor="p-for" hint="0 = alert at the first check; 5 min avoids alerts on a blip">
              <DurInput id="p-for" label="hold for" value={r.for || "0s"} onChange={(v) => setR((x) => ({ ...x, for: v }))} />
            </Field>
          )}
          {logs && (
            <Field label="One alert per field" htmlFor="p-group" hint="e.g. host — one alert per host instead of one for all" className="sm:col-span-2">
              <FieldPicker id="p-group" src={src} label="group by field" value={r.group_by[0] || ""} onChange={(v) => setR((x) => ({ ...x, group_by: v.trim() ? [v.trim()] : [] }))} />
            </Field>
          )}
        </div>
      </details>
    </>
  );
}

// ---------------------------------------------------------------- step 3 — who to tell

function StepWho({ r, setR, channels, kinds, canManage, onChannelsChanged }: {
  r: RuleIn; setR: (f: (x: RuleIn) => RuleIn) => void; channels: Channel[]; kinds: ChannelKindDef[]; canManage: boolean; onChannelsChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [tests, setTests] = useState<Record<number, string>>({});
  const toggle = (id: number) => setR((x) => ({ ...x, channels: x.channels.includes(id) ? x.channels.filter((c) => c !== id) : [...x.channels, id] }));
  return (
    <>
      <Field label="How serious">
        <Seg label="severity" value={r.severity} onChange={(v: Severity) => setR((x) => ({ ...x, severity: v }))}
          opts={[{ id: "info", label: <><span aria-hidden className="text-sky-300">●</span> Info</> }, { id: "warning", label: <><span aria-hidden className="text-amber-300">▲</span> Warning</> },
            { id: "critical", label: <><span aria-hidden className="text-red-300">◆</span> Critical</> }]} />
      </Field>
      <Field label="Tell" hint={channels.length ? "the message says what fired, the value, and links back here" : undefined}>
        <div className="space-y-1.5">
          {channels.filter((c) => c.enabled).map((c) => {
            const on = r.channels.includes(c.id);
            return (
              <div key={c.id} className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 ${on ? "border-sea bg-sea/10" : "border-line"}`}>
                <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-[12.5px] text-slate-100">
                  <input type="checkbox" className="ui-focus accent-sky-500" checked={on} onChange={() => { toggle(c.id); setR((x) => ({ ...x, no_notification: false })); }} />
                  <ChannelBadge c={c} />
                </label>
                {tests[c.id] && <span className={`text-[10.5px] ${tests[c.id].startsWith("✓") ? "text-emerald-300" : tests[c.id] === "…" ? "text-mut" : "text-red-300"}`}>{tests[c.id] === "…" ? "sending…" : tests[c.id]}</span>}
                <button type="button" className={btn.small} onClick={() => { setTests((t) => ({ ...t, [c.id]: "…" })); testChannel(c.id).then((x) => setTests((t) => ({ ...t, [c.id]: x.ok ? "✓ sent" : x.timed_out ? "⏱ no answer yet" : `✕ ${x.detail || "failed"}` }))).catch((e) => setTests((t) => ({ ...t, [c.id]: `✕ ${e.message || e}` }))); }}>test</button>
              </div>
            );
          })}
          {channels.length === 0 && !adding && <div className="rounded-lg border border-dashed border-line p-3 text-[12px] text-mut">No channel yet{canManage ? " — add one now, it takes a minute." : " — ask a maintainer to add one (Settings › Channels), or tick « no notification » below."}</div>}
          <label className="flex items-center gap-2 pt-1 text-[12px] text-mut">
            <input type="checkbox" className="ui-focus accent-sky-500" checked={!!r.no_notification}
              onChange={(e) => setR((x) => ({ ...x, no_notification: e.target.checked, channels: e.target.checked ? [] : x.channels }))} />
            no notification — this rule only shows in the cockpit
          </label>
          {canManage && (adding
            ? <div className="rounded-lg border border-sea/40 p-2.5"><ChannelEditor kinds={kinds} onDone={() => { setAdding(false); onChannelsChanged(); }} onCreated={(c) => setR((x) => ({ ...x, channels: [...x.channels, c.id] }))} /></div>
            : <button type="button" className={btn.small} onClick={() => setAdding(true)}>+ Add a channel</button>)}
        </div>
      </Field>
      <div className="grid gap-3 border-t border-line pt-3 sm:grid-cols-2">
        <Field label="Remind every" htmlFor="w-realert" hint="while it keeps firing — no spam, no forgetting"><DurInput id="w-realert" label="remind every" value={r.realert} onChange={(v) => setR((x) => ({ ...x, realert: v }))} /></Field>
        <div className="space-y-2">
          <Toggle on={r.quiet_hours.enabled} onChange={(v) => setR((x) => ({ ...x, quiet_hours: { ...x.quiet_hours, enabled: v } }))} label="Quiet hours"
            hint="still evaluated and shown here, but nobody is pinged in this window" />
          {r.quiet_hours.enabled && (
            <div className="flex items-center gap-1.5 pl-10 text-[12px] text-mut">
              <input aria-label="quiet from" type="time" value={r.quiet_hours.start} onChange={(e) => setR((x) => ({ ...x, quiet_hours: { ...x.quiet_hours, start: e.target.value } }))} className={`${inputCls} w-auto`} />
              to
              <input aria-label="quiet until" type="time" value={r.quiet_hours.end} onChange={(e) => setR((x) => ({ ...x, quiet_hours: { ...x.quiet_hours, end: e.target.value } }))} className={`${inputCls} w-auto`} />
            </div>
          )}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- step 4 — then

function StepThen({ r, setR, agents, runbooks, onName }: {
  r: RuleIn; setR: (f: (x: RuleIn) => RuleIn) => void; agents: { id: number; name: string }[]; runbooks: { name: string; description?: string }[]; onName: () => void;
}) {
  const a = r.actions;
  const setA = (p: Partial<RuleIn["actions"]>) => setR((x) => ({ ...x, actions: { ...x.actions, ...p } }));
  return (
    <>
      <div className="space-y-2.5">
        <div className="text-[11.5px] font-medium text-slate-300">When it fires</div>
        <Toggle on={a.incident} onChange={(v) => setA({ incident: v })} label="Open an incident" hint="in Operate › Incidents, with the value, the samples and a link to this rule" />
        <Toggle on={a.diag_session} onChange={(v) => setA({ diag_session: v })} label="Start a diagnosis session"
          hint="an agent reads the alert with the project's memory and proposes what to do — nothing runs without your go-ahead" />
        <Field label="Propose a Crew agent run" htmlFor="w-agent" hint="the run waits for an approval (four eyes if your project asks for it)">
          <select id="w-agent" value={a.agent_id ?? ""} onChange={(e) => setA({ agent_id: e.target.value ? Number(e.target.value) : null })} className={inputCls}>
            <option value="">— no agent —</option>
            {agents.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Field>
        <Field label="Point to a runbook" htmlFor="w-runbook" hint="shown with the alert, replayable as a guided session">
          <select id="w-runbook" value={a.runbook ?? ""} onChange={(e) => setA({ runbook: e.target.value || null })} className={inputCls}>
            <option value="">— none —</option>
            {runbooks.map((b) => <option key={b.name} value={b.name}>{b.name.replace(/^runbook-/, "")}</option>)}
          </select>
        </Field>
      </div>
      <div className="grid gap-3 border-t border-line pt-3">
        <Field label="Name" htmlFor="w-name"><input id="w-name" value={r.name} onChange={(e) => { onName(); setR((x) => ({ ...x, name: e.target.value })); }} placeholder="API — too many 5xx" className={inputCls} /></Field>
        <Field label="Note for whoever gets paged" htmlFor="w-desc" hint="what it means, where to look first — it goes in the message">
          <textarea id="w-desc" rows={2} value={r.description} onChange={(e) => setR((x) => ({ ...x, description: e.target.value }))} className={`${inputCls} resize-y`} />
        </Field>
        <Toggle on={r.enabled} onChange={(v) => setR((x) => ({ ...x, enabled: v }))} label="Turn it on now" hint="off = saved, not evaluated — turn it on from the list later" />
      </div>
    </>
  );
}

