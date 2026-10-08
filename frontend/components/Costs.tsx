"use client";
import { useEffect, useMemo, useState } from "react";
import { budgetSet, fetchUsage, instanceInfo } from "@/lib/api";
import type { InstanceInfo } from "@/lib/api";
import type { BillingBasis, ProjectBudget, UsageSummary } from "@/lib/types";
import { ago } from "@/lib/fmt";

const usd = (v: number) =>
  v >= 1000 ? `$${Math.round(v).toLocaleString("en-US")}` : v >= 100 ? `$${Math.round(v)}` : v >= 10 ? `$${v.toFixed(1)}` : `$${v.toFixed(2)}`;
const ktok = (v: number) =>
  v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)}M` : v >= 1000 ? `${Math.round(v / 1000)}k` : `${v}`;

const BASIS_TONE: Record<BillingBasis, string> = {
  api: "bg-sky-400/15 text-sky-200",
  subscription: "bg-emerald-400/15 text-emerald-200",
  gateway: "bg-brass/15 text-brass",
  local: "bg-violet-400/15 text-violet-200",
  custom: "bg-slate-400/15 text-slate-200",
};
const BASIS_SHORT: Record<BillingBasis, string> = {
  api: "API key", subscription: "subscription", gateway: "SOKKAN Inference", local: "local", custom: "other endpoint",
};
function BasisChip({ b }: { b: BillingBasis }) {
  return <span className={`whitespace-nowrap rounded px-1.5 py-px text-[10.5px] ${BASIS_TONE[b]}`}>{BASIS_SHORT[b]}</span>;
}

/** One period: what is billed, and — when it differs — the same tokens at the API price. */
function Tile({ label, billed, apiEquiv, sub, budget }: { label: string; billed: number; apiEquiv: number; sub: string; budget?: number }) {
  const over = !!budget && billed >= budget;
  const warn = !!budget && !over && billed >= 0.8 * budget;
  const differs = Math.abs(apiEquiv - billed) > 0.005;
  return (
    <div className={`min-w-[150px] flex-1 rounded-xl border bg-panel p-4 ${over ? "border-red-500/50" : warn ? "border-amber-500/50" : "border-line"}`}>
      <div className="text-[11px] uppercase tracking-wide text-mut">{label}</div>
      <div className={`mt-1 text-[26px] font-semibold tabular-nums ${over ? "text-red-300" : warn ? "text-amber-200" : "text-slate-100"}`}>{usd(billed)}</div>
      <div className="text-[11px] text-mut">billed{budget ? ` · budget ${usd(budget)}/day` : ""}</div>
      {differs && (
        <div className="mt-1 text-[11.5px] tabular-nums text-slate-300" title="the same tokens at the public Claude API price — not billed on this basis">
          {usd(apiEquiv)} <span className="text-mut">API-equivalent</span>
        </div>
      )}
      <div className="mt-1 text-[11px] text-mut">{sub}</div>
    </div>
  );
}

// 3.2 lot 4 — the selected project's day / month ceiling: spend, state, and the form a
// project admin uses to change it (the API refuses it to anyone else).
function BudgetPanel({ b, onSaved, subscription }: { b: ProjectBudget; onSaved: (b: ProjectBudget) => void; subscription?: boolean }) {
  const [day, setDay] = useState(String(b.day || ""));
  const [month, setMonth] = useState(String(b.month || ""));
  const [cur, setCur] = useState<string>(b.currency);
  const [err, setErr] = useState("");
  const tone = b.state === "stop" ? "border-red-500/50" : b.state === "warn" ? "border-amber-500/50" : "border-line";
  const row = (label: string, spent: number, cap: number) => (
    <div className="flex items-baseline gap-2">
      <span className="w-14 text-[11px] uppercase tracking-wide text-mut">{label}</span>
      <span className="tabular-nums text-slate-100">{spent.toFixed(2)} {b.currency}</span>
      <span className="text-[11px] text-mut">{cap ? `of ${cap.toFixed(2)} (${Math.round((100 * spent) / cap)}%)` : "no ceiling"}</span>
    </div>
  );
  const save = () => {
    setErr("");
    budgetSet({ currency: cur, day: Number(day || 0), month: Number(month || 0) })
      .then(onSaved).catch((e) => setErr(String(e)));
  };
  return (
    <div className={`rounded-xl border bg-panel p-4 text-[12.5px] ${tone}`}>
      <div className="mb-2 flex items-baseline justify-between">
        <span className="font-medium text-slate-200">Project budget — <span className="font-mono">{b.project}</span></span>
        <span className="text-[11px] text-mut">warning at 80 %, new turns and agent runs stop at 100 %</span>
      </div>
      {subscription && (
        <div className="mb-2 text-[11px] text-mut">On a subscription nothing is billed per token: the budget counts the API-equivalent.</div>
      )}
      {row("today", b.spent_day, b.day)}
      {row("month", b.spent_month, b.month)}
      {b.message && <div className={`mt-2 text-[11.5px] ${b.state === "stop" ? "text-red-300" : "text-amber-200"}`}>{b.message}</div>}
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <input value={day} onChange={(e) => setDay(e.target.value)} placeholder="per day" inputMode="decimal"
          className="w-24 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
        <input value={month} onChange={(e) => setMonth(e.target.value)} placeholder="per month" inputMode="decimal"
          className="w-24 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
        <select aria-label="Budget currency" value={cur} onChange={(e) => setCur(e.target.value)}
          className="rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100">
          <option>USD</option><option>CHF</option>
        </select>
        <button onClick={save} className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">save</button>
        <span className="text-[11px] text-mut">0 = no ceiling · project admins only</span>
      </div>
      {err && <div className="mt-1 text-[11px] text-red-400">{err}</div>}
    </div>
  );
}

const PERIODS = [7, 30, 90];

export default function Costs() {
  const [data, setData] = useState<UsageSummary | null>(null);
  const [inst, setInst] = useState<InstanceInfo | null>(null);
  const [err, setErr] = useState(false);
  const [days, setDays] = useState(30);

  useEffect(() => {
    instanceInfo().then(setInst).catch(() => {});
  }, []);
  useEffect(() => {
    let alive = true;
    const load = () => fetchUsage(days).then((d) => alive && setData(d)).catch(() => alive && setErr(true));
    load();
    const iv = setInterval(load, 60_000);
    return () => { alive = false; clearInterval(iv); };
  }, [days]);

  // continuous series over the period (days without activity = 0)
  const series = useMemo(() => {
    if (!data) return [];
    const byDay = Object.fromEntries(data.days.map((d) => [d.day, d]));
    const n = data.period?.days ?? days;
    const out: { day: string; billed: number; api: number; turns: number }[] = [];
    for (let i = n - 1; i >= 0; i--) {
      const d = new Date(Date.now() - i * 86400_000);
      const key = d.toLocaleDateString("en-CA", { timeZone: "Europe/Zurich" });
      const row = byDay[key];
      out.push({ day: key, billed: row?.cost ?? 0, api: row?.api_equiv ?? 0, turns: row?.turns ?? 0 });
    }
    return out;
  }, [data, days]);

  if (err) return <div className="mt-10 text-center text-[12.5px] text-mut">costs not accessible (dev role required)</div>;
  if (!data) return <div className="mt-10 text-center text-[12.5px] text-mut">aggregating transcripts…</div>;

  const max = Math.max(1e-9, ...series.map((d) => Math.max(d.api, d.billed)));
  const maxIdx = series.findIndex((d) => Math.max(d.api, d.billed) === max);
  const anyBilled = series.some((d) => d.billed > 0);
  const t = data.totals;
  const ext = data.sources?.external;
  const step = Math.max(1, Math.round(series.length / 6));
  const dlabel = (day: string) => new Date(`${day}T12:00:00`).toLocaleDateString("en-CH", { day: "2-digit", month: "2-digit" });

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto max-w-5xl space-y-5">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-[15px] font-semibold text-slate-100">
            Costs &amp; usage{data.project ? <> — project <span className="font-mono">{data.project}</span></> : null}
          </h2>
          <div className="ml-auto flex overflow-hidden rounded-lg border border-line text-[12px]" role="group" aria-label="Period">
            {PERIODS.map((n) => (
              <button key={n} onClick={() => setDays(n)} aria-pressed={days === n}
                className={`px-2.5 py-1 ${days === n ? "bg-sea/30 text-slate-100" : "text-mut hover:bg-panel2"}`}>{n} days</button>
            ))}
          </div>
        </div>

        {/* how the figures are computed — always visible, never a guess */}
        <div className="rounded-xl border border-line bg-panel2/40 px-4 py-3 text-[12px] leading-relaxed text-slate-300">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-slate-100">How this instance pays for Claude:</span>
            <BasisChip b={data.billing.claude} />
            <span className="text-mut">{data.billing.why}</span>
          </div>
          <div className="mt-1 text-mut">
            {data.billing.claude_method}. Prices: public Claude API table of <span className="text-slate-300">{data.pricing.version}</span>
            {data.pricing.override ? " (+ operator overrides)" : ""}, per model and per token kind (input, cache write, cache read, output).
            Figures cover the sessions started by SOKKAN{data.include_external ? " and the other transcripts of the workspace" : ""}.
          </div>
        </div>

        <div className="flex flex-wrap gap-3">
          <Tile label="today" billed={t.today.cost} apiEquiv={t.today.api_equiv}
            sub={`${t.today.turns} turns · ${ktok(t.today.out_tokens)} tok out`} budget={inst?.budget_day_usd || 0} />
          <Tile label="7 days" billed={t["7d"].cost} apiEquiv={t["7d"].api_equiv}
            sub={`${t["7d"].turns} turns · ${ktok(t["7d"].out_tokens)} tok out`} />
          <Tile label={`${data.period.days} days`} billed={t.period.cost} apiEquiv={t.period.api_equiv}
            sub={`${t.period.turns} turns · ${ktok(t.period.out_tokens)} tok out`} />
          <Tile label="all time" billed={t.all.cost} apiEquiv={t.all.api_equiv}
            sub={`${t.all.turns} turns · ${ktok(t.all.out_tokens)} tok out`} />
        </div>

        {ext && ext.transcripts > 0 && !ext.counted && (
          <div className="rounded-xl border border-line bg-panel px-4 py-2.5 text-[12px] text-mut">
            <span className="text-slate-300">{ext.transcripts} other transcript{ext.transcripts > 1 ? "s" : ""}</span> in this
            workspace were not started by SOKKAN (Claude Code used directly in the same folder): {ext.turns.toLocaleString("en-CH")} turns,
            {" "}{usd(ext.api_equiv)} API-equivalent — <span className="text-slate-300">not counted</span> above.
            Set <code className="font-mono text-slate-300">SOKKAN_USAGE_EXTERNAL=include</code> to count them.
          </div>
        )}

        {data.unpriced_models.length > 0 && (
          <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 px-4 py-2.5 text-[12px] text-amber-200">
            No price in the table for {data.unpriced_models.join(", ")} — counted in tokens only.
            Add it to <code className="font-mono">SOKKAN_CLAUDE_PRICES</code> (Claude) or <code className="font-mono">SOKKAN_MODEL_PRICES</code>.
          </div>
        )}

        {data.project_budget && (
          <BudgetPanel b={data.project_budget} subscription={data.billing.claude === "subscription"}
            onSaved={(b) => setData((d) => (d ? { ...d, project_budget: b } : d))} />
        )}

        {/* by basis */}
        {data.by_basis.length > 0 && (
          <div className="overflow-x-auto rounded-xl border border-line bg-panel">
            <div className="border-b border-line px-4 py-2.5 text-[12.5px] font-medium text-slate-200">By billing basis — last {data.period.days} days</div>
            <table className="w-full min-w-[640px] text-[12px]">
              <thead><tr className="text-left text-[11px] text-mut">
                <th className="px-4 pb-1 pt-2 font-medium">basis</th>
                <th className="px-2 pb-1 pt-2 font-medium">method</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">turns</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">billed</th>
                <th className="whitespace-nowrap px-4 pb-1 pt-2 text-right font-medium">API-equivalent</th>
              </tr></thead>
              <tbody>
                {data.by_basis.map((b) => (
                  <tr key={b.basis} className="border-t border-line/50 align-top">
                    <td className="px-4 py-1.5"><BasisChip b={b.basis} /></td>
                    <td className="px-2 py-1.5 text-mut">{b.method}{b.unpriced_turns ? ` · ${b.unpriced_turns} unpriced turns` : ""}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-mut">{b.turns.toLocaleString("en-CH")}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-100">{usd(b.billed)}</td>
                    <td className="px-4 py-1.5 text-right tabular-nums text-slate-300">{usd(b.api_equiv)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.gateway && (
              <div className="border-t border-line/50 px-4 py-2 text-[11.5px] text-mut">
                SOKKAN Inference ledger (whole tenant): today {((data.gateway.spent_today_centimes ?? 0) / 100).toFixed(2)} CHF ·
                this month {((data.gateway.spent_month_centimes ?? 0) / 100).toFixed(2)} CHF
                {data.gateway.balance_centimes != null ? ` · balance ${(data.gateway.balance_centimes / 100).toFixed(2)} CHF` : ""}
              </div>
            )}
          </div>
        )}

        {/* daily bars: API-equivalent (light) with the billed part (solid) */}
        <div className="rounded-xl border border-line bg-panel p-4">
          <div className="mb-3 flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <span className="text-[12.5px] font-medium text-slate-200">Per day — last {data.period.days} days</span>
            <span className="flex items-center gap-1.5 text-[11px] text-mut"><span className="h-2 w-3 rounded-sm bg-sea" />billed</span>
            <span className="flex items-center gap-1.5 text-[11px] text-mut"><span className="h-2 w-3 rounded-sm bg-sea/45" />API-equivalent</span>
            <span className="ml-auto text-[11px] tabular-nums text-mut">max {usd(max)}</span>
          </div>
          <div className="flex h-40 items-end gap-[2px]">
            {series.map((d, i) => (
              <div key={d.day} className="group relative flex h-full flex-1 items-end">
                <div className="relative w-full rounded-t bg-sea/45" style={{ height: `${Math.max(d.api > 0 ? 2 : 0, (Math.max(d.api, d.billed) / max) * 100)}%` }}>
                  {d.billed > 0 && (
                    <div className="absolute bottom-0 w-full rounded-t bg-sea" style={{ height: `${Math.min(100, (d.billed / Math.max(d.api, d.billed)) * 100)}%` }} />
                  )}
                </div>
                {i === maxIdx && max > 1e-6 && (
                  <span className="absolute -top-4 left-1/2 -translate-x-1/2 text-[10px] tabular-nums text-slate-300">
                    {usd(anyBilled ? Math.max(d.billed, d.api) : d.api)}
                  </span>
                )}
                <div className="pointer-events-none invisible absolute bottom-full left-1/2 z-10 mb-5 -translate-x-1/2 whitespace-nowrap rounded-md border border-line bg-panel2 px-2 py-1 text-[11px] shadow-xl group-hover:visible">
                  <span className="text-slate-200">{dlabel(d.day)}</span>
                  <span className="ml-2 tabular-nums text-slate-100">{usd(d.billed)} billed</span>
                  <span className="ml-2 tabular-nums text-slate-300">{usd(d.api)} API-eq.</span>
                  <span className="ml-2 text-mut">{d.turns} turns</span>
                </div>
              </div>
            ))}
          </div>
          {/* one slot per bar: the labels sit under their bar, the last day always labelled */}
          <div className="mt-1.5 flex gap-[2px] text-[10px] text-mut">
            {series.map((d, i) => (
              <div key={d.day} className="relative h-3 flex-1">
                {(i % step === 0 && series.length - 1 - i >= step / 2) || i === series.length - 1 ? (
                  <span className={`absolute top-0 whitespace-nowrap ${i === series.length - 1 ? "right-0" : "left-0"}`}>{dlabel(d.day)}</span>
                ) : null}
              </div>
            ))}
          </div>
        </div>

        {/* by model: tokens and dollars per kind */}
        {data.by_model.length > 0 && (
          <div className="overflow-x-auto rounded-xl border border-line bg-panel">
            <div className="border-b border-line px-4 py-2.5 text-[12.5px] font-medium text-slate-200">
              By model — tokens and API-equivalent cost per kind
            </div>
            <table className="w-full min-w-[760px] text-[12px]">
              <thead><tr className="text-left text-[11px] text-mut">
                <th className="px-4 pb-1 pt-2 font-medium">model</th>
                <th className="px-2 pb-1 pt-2 font-medium">basis</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">input</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">cache write</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">cache read</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">output</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">billed</th>
                <th className="px-4 pb-1 pt-2 text-right font-medium">API-eq.</th>
              </tr></thead>
              <tbody>
                {data.by_model.map((m) => {
                  const cell = (tok: number, c: number) => (
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      <div className="text-slate-200">{ktok(tok)}</div>
                      {m.priced_as && <div className="text-[10.5px] text-mut">{usd(c)}</div>}
                    </td>
                  );
                  return (
                    <tr key={m.model} className="border-t border-line/50">
                      <td className="px-4 py-1.5">
                        <div className="font-mono text-[11.5px] text-slate-200">{m.model}</div>
                        <div className="text-[10.5px] text-mut">
                          {m.price ? `$${m.price.input}/$${m.price.output} per M · cache read $${m.price.cache_read}` : "no price — tokens only"}
                          {" · "}{m.turns.toLocaleString("en-CH")} turn{m.turns === 1 ? "" : "s"}
                        </div>
                      </td>
                      <td className="px-2 py-1.5"><BasisChip b={m.basis} /></td>
                      {cell(m.in_tokens, m.cost_in)}
                      {cell(m.cache_write, m.cost_cw)}
                      {cell(m.cache_read, m.cost_cr)}
                      {cell(m.out_tokens, m.cost_out)}
                      <td className="px-2 py-1.5 text-right tabular-nums text-slate-100">{usd(m.billed)}</td>
                      <td className="px-4 py-1.5 text-right tabular-nums text-slate-300">{m.priced_as ? usd(m.api_equiv) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* by project (instance view, or several projects) */}
        {data.by_project.length > 1 && (
          <div className="rounded-xl border border-line bg-panel">
            <div className="border-b border-line px-4 py-2.5 text-[12.5px] font-medium text-slate-200">By project</div>
            <table className="w-full text-[12px]">
              <tbody>
                {data.by_project.map((p) => (
                  <tr key={p.project} className="border-t border-line/50 first:border-t-0">
                    <td className="px-4 py-1.5 font-mono text-slate-200">{p.project}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-mut">{p.turns} turns</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-100">{usd(p.billed)} billed</td>
                    <td className="px-4 py-1.5 text-right tabular-nums text-slate-300">{usd(p.api_equiv)} API-eq.</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* sessions */}
        <div className="overflow-x-auto rounded-xl border border-line bg-panel">
          <div className="border-b border-line px-4 py-2.5 text-[12.5px] font-medium text-slate-200">
            Sessions — last {data.period.days} days, by API-equivalent
          </div>
          {data.sessions.length === 0 ? (
            <div className="px-4 py-6 text-center text-[12px] text-mut">No SOKKAN session used a model in this period.</div>
          ) : (
            <table className="w-full min-w-[680px] text-[12px]">
              <thead>
                <tr className="text-left text-[11px] text-mut">
                  <th className="px-4 pb-1 pt-2 font-medium">session</th>
                  <th className="px-2 pb-1 pt-2 font-medium">tag</th>
                  <th className="px-2 pb-1 pt-2 text-right font-medium">turns</th>
                  <th className="px-2 pb-1 pt-2 text-right font-medium">tok out</th>
                  <th className="px-2 pb-1 pt-2 text-right font-medium">billed</th>
                  <th className="px-2 pb-1 pt-2 text-right font-medium">API-eq.</th>
                  <th className="px-4 pb-1 pt-2 text-right font-medium">last</th>
                </tr>
              </thead>
              <tbody>
                {data.sessions.slice(0, 15).map((s) => (
                  <tr key={s.session_id} className="border-t border-line/50 hover:bg-panel2/40">
                    <td className="max-w-md truncate px-4 py-1.5 text-slate-200" title={`${s.title} — ${s.models}`}>
                      {s.title}{s.subagents ? <span className="ml-1.5 text-[10.5px] text-mut">+{s.subagents} sub-agent{s.subagents > 1 ? "s" : ""}</span> : null}
                    </td>
                    <td className="px-2 py-1.5">
                      {s.tag && <span className="rounded bg-brass/15 px-1.5 text-[10px] text-brass">{s.tag}</span>}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-mut">{s.turns}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-mut">{ktok(s.out_tokens)}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-100">{usd(s.cost)}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">{usd(s.api_equiv)}</td>
                    <td className="px-4 py-1.5 text-right text-mut">{s.last_ts ? ago(s.last_ts) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
