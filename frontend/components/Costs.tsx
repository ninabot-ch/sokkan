"use client";
import { useEffect, useMemo, useState } from "react";
import { budgetSet, fetchUsage, instanceInfo } from "@/lib/api";
import type { InstanceInfo } from "@/lib/api";
import type { ProjectBudget, UsageSummary } from "@/lib/types";
import { ago } from "@/lib/fmt";

const usd = (v: number) =>
  v >= 100 ? `$${Math.round(v)}` : v >= 10 ? `$${v.toFixed(1)}` : `$${v.toFixed(2)}`;
const ktok = (v: number) =>
  v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)}M` : v >= 1000 ? `${Math.round(v / 1000)}k` : `${v}`;

function Tile({ label, cost, sub, budget }: { label: string; cost: number; sub: string; budget?: number }) {
  const over = !!budget && cost >= budget;
  const warn = !!budget && !over && cost >= 0.8 * budget;
  return (
    <div className={`flex-1 rounded-xl border bg-panel p-4 ${over ? "border-red-500/50" : warn ? "border-amber-500/50" : "border-line"}`}>
      <div className="text-[11px] uppercase tracking-wide text-mut">{label}</div>
      <div className={`mt-1 text-[26px] font-semibold tabular-nums ${over ? "text-red-300" : warn ? "text-amber-200" : "text-slate-100"}`}>{usd(cost)}</div>
      <div className="text-[11px] text-mut">{sub}{budget ? ` · budget ${usd(budget)}/day` : ""}</div>
    </div>
  );
}

// 3.2 lot 4 — the selected project's day / month ceiling: spend, state, and the form a
// project admin uses to change it (the API refuses it to anyone else).
function BudgetPanel({ b, onSaved }: { b: ProjectBudget; onSaved: (b: ProjectBudget) => void }) {
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

export default function Costs() {
  const [data, setData] = useState<UsageSummary | null>(null);
  const [inst, setInst] = useState<InstanceInfo | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    instanceInfo().then(setInst).catch(() => {});
    let alive = true;
    const load = () => fetchUsage(30).then((d) => alive && setData(d)).catch(() => alive && setErr(true));
    load();
    const iv = setInterval(load, 60_000);
    return () => { alive = false; clearInterval(iv); };
  }, []);

  // série 30 jours continue (jours sans activité = 0)
  const series = useMemo(() => {
    if (!data) return [];
    const byDay = Object.fromEntries(data.days.map((d) => [d.day, d]));
    const out: { day: string; cost: number; turns: number }[] = [];
    for (let i = 29; i >= 0; i--) {
      const d = new Date(Date.now() - i * 86400_000);
      const key = d.toISOString().slice(0, 10);
      const row = byDay[key];
      out.push({ day: key, cost: row?.cost ?? 0, turns: row?.turns ?? 0 });
    }
    return out;
  }, [data]);

  const max = Math.max(1e-9, ...series.map((d) => d.cost));
  const maxIdx = series.findIndex((d) => d.cost === max);

  if (err) return <div className="mt-10 text-center text-[12.5px] text-mut">costs not accessible (dev role required)</div>;
  if (!data) return <div className="mt-10 text-center text-[12.5px] text-mut">aggregating transcripts…</div>;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mx-auto max-w-5xl space-y-5">
        <div className="flex items-baseline gap-2">
          <h2 className="text-[15px] font-semibold text-slate-100">
            Costs &amp; usage{data.project ? <> — project <span className="font-mono">{data.project}</span></> : null}
          </h2>
          <span className="text-[11px] text-mut">— {data.note}</span>
        </div>

        {/* tuiles */}
        <div className="flex flex-wrap gap-3">
          <Tile label="today" cost={data.totals.today.cost}
            sub={`${data.totals.today.turns} turns · ${ktok(data.totals.today.out_tokens)} tok out`}
            budget={inst?.budget_day_usd || 0} />
          <Tile label="7 days" cost={data.totals["7d"].cost}
            sub={`${data.totals["7d"].turns} turns · ${ktok(data.totals["7d"].out_tokens)} tok out`} />
          <Tile label="30 days" cost={data.totals["30d"].cost}
            sub={`${data.totals["30d"].turns} turns · ${ktok(data.totals["30d"].out_tokens)} tok out`} />
          <Tile label="total (transcripts)" cost={data.totals.all.cost}
            sub={`${data.totals.all.turns} turns · ${ktok(data.totals.all.out_tokens)} tok out`} />
        </div>

        {data.project_budget && (
          <BudgetPanel b={data.project_budget}
            onSaved={(b) => setData((d) => (d ? { ...d, project_budget: b } : d))} />
        )}

        {/* barres quotidiennes — série unique (pas de légende), labels en encre neutre */}
        <div className="rounded-xl border border-line bg-panel p-4">
          <div className="mb-3 flex items-baseline justify-between">
            <span className="text-[12.5px] font-medium text-slate-200">Estimated cost per day — last 30 days</span>
            <span className="text-[11px] tabular-nums text-mut">max {usd(max)}</span>
          </div>
          <div className="flex h-40 items-end gap-[2px]">
            {series.map((d, i) => (
              <div key={d.day} className="group relative flex h-full flex-1 items-end">
                <div
                  className="w-full rounded-t bg-sea transition-colors group-hover:bg-sky-400"
                  style={{ height: `${Math.max(d.cost > 0 ? 2 : 0, (d.cost / max) * 100)}%` }}
                />
                {/* label direct sélectif : le max uniquement */}
                {i === maxIdx && d.cost > 0 && (
                  <span className="absolute -top-4 left-1/2 -translate-x-1/2 text-[10px] tabular-nums text-slate-300">
                    {usd(d.cost)}
                  </span>
                )}
                <div className="pointer-events-none invisible absolute bottom-full left-1/2 z-10 mb-5 -translate-x-1/2 whitespace-nowrap rounded-md border border-line bg-panel2 px-2 py-1 text-[11px] shadow-xl group-hover:visible">
                  <span className="text-slate-200">{new Date(`${d.day}T12:00:00`).toLocaleDateString("en-CH", { day: "2-digit", month: "2-digit" })}</span>
                  <span className="ml-2 tabular-nums text-slate-100">{usd(d.cost)}</span>
                  <span className="ml-2 text-mut">{d.turns} turns</span>
                </div>
              </div>
            ))}
          </div>
          <div className="mt-1.5 flex justify-between text-[10px] text-mut">
            {series.filter((_, i) => i % 5 === 0).map((d) => (
              <span key={d.day}>{new Date(`${d.day}T12:00:00`).toLocaleDateString("en-CH", { day: "2-digit", month: "2-digit" })}</span>
            ))}
          </div>
        </div>

        {/* top sessions */}
        <div className="rounded-xl border border-line bg-panel">
          <div className="border-b border-line px-4 py-2.5 text-[12.5px] font-medium text-slate-200">
            Most expensive sessions — last 30 days
          </div>
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-[11px] text-mut">
                <th className="px-4 pb-1 pt-2 font-medium">session</th>
                <th className="px-2 pb-1 pt-2 font-medium">tag</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">turns</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">tok out</th>
                <th className="px-2 pb-1 pt-2 text-right font-medium">est. cost</th>
                <th className="px-4 pb-1 pt-2 text-right font-medium">last</th>
              </tr>
            </thead>
            <tbody>
              {data.sessions.slice(0, 15).map((s) => (
                <tr key={s.session_id} className="border-t border-line/50 hover:bg-panel2/40">
                  <td className="max-w-md truncate px-4 py-1.5 text-slate-200" title={s.title}>{s.title}</td>
                  <td className="px-2 py-1.5">
                    {s.tag && <span className="rounded bg-brass/15 px-1.5 text-[10px] text-brass">{s.tag}</span>}
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-mut">{s.turns}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-mut">{ktok(s.out_tokens)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-slate-100">{usd(s.cost)}</td>
                  <td className="px-4 py-1.5 text-right text-mut">{s.last_ts ? ago(s.last_ts) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
