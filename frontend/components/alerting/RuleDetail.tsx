"use client";
// 3.5 — one rule: what it says (plain sentence), how it behaved (backtest of the range with the REAL
// firings marked on top), its active alerts, the timeline of its state changes, and its actions
// (edit, turn off, send a test, silence, delete).
import { useEffect, useRef, useState } from "react";
import { createSilence, deleteRule, getRule, preview, ruleHistory, setRuleEnabled, testNotify } from "@/lib/alerting";
import {
  RANGES, durS, fmtValue, groupText, ruleState, since, toRuleIn, type Alert, type AlertSource, type Channel, type PreviewResult,
  type Rule, type Transition,
} from "@/lib/alertingModel";
import Chart from "./Chart";
import { ChannelBadge } from "./Settings";
import { AlertRow } from "./AlertRow";
import { Banner, Seg, SeverityChip, StateChip, btn, fmtTime } from "./bits";

export default function RuleDetail({ id, sources, channels, alerts, canWrite, onEdit, onBack, onChanged, onOpenIncident }: {
  id: number; sources: AlertSource[]; channels: Channel[]; alerts: Alert[]; canWrite: boolean;
  onEdit: (r: Rule) => void; onBack: () => void; onChanged: () => void; onOpenIncident?: (id: number) => void;
}) {
  const [r, setR] = useState<Rule | null>(null);
  const [err, setErr] = useState("");
  const [range, setRange] = useState<"1h" | "6h" | "24h" | "7d">("24h");
  const [pv, setPv] = useState<PreviewResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [hist, setHist] = useState<Transition[]>([]);
  const [msg, setMsg] = useState<{ tone: "ok" | "error" | "info"; text: string } | null>(null);
  const seq = useRef(0);

  const load = () => {
    getRule(id).then(setR).catch((e) => setErr(String(e.message || e)));
    ruleHistory(id, 200).then(setHist).catch(() => setHist([]));
  };
  useEffect(load, [id]);
  useEffect(() => {
    if (!r) return;
    const my = ++seq.current; setLoading(true);
    preview(toRuleIn(r), range).then((x) => { if (my === seq.current) setPv(x); })
      .catch((e) => { if (my === seq.current) setPv({ kind: "metric", series: [], fired_intervals: [], fires: 0, error: String(e.message || e) }); })
      .finally(() => { if (my === seq.current) setLoading(false); });
  }, [r, range]);

  if (err) return <div className="mx-auto max-w-5xl space-y-2"><button type="button" onClick={onBack} className={btn.small}>← Alerts</button><Banner tone="error">{err}</Banner></div>;
  if (!r) return <div className="p-4 text-[12px] text-mut">Loading…</div>;

  const src = sources.find((s) => s.id === r.source_id);
  const st = ruleState(r);
  const mine = alerts.filter((a) => a.rule_id === r.id);
  const unit = r.query.builder?.ratio_of ? "%" : null;
  const act = async (f: () => Promise<unknown>, ok: string) => {
    setMsg(null);
    try { await f(); setMsg({ tone: "ok", text: ok }); load(); onChanged(); } catch (e) { setMsg({ tone: "error", text: String((e as Error).message || e) }); }
  };
  const test = async () => {
    setMsg({ tone: "info", text: "Sending a test message…" });
    try {
      const res = await testNotify(r.id);
      const parts = Object.entries(res.channels).map(([cid, v]) => `${channels.find((c) => String(c.id) === cid)?.name || `#${cid}`}: ${v}`);
      const bad = Object.values(res.channels).some((v) => v !== "ok");
      setMsg({ tone: bad ? "error" : "ok", text: parts.length ? `${bad ? "Some channels failed" : "Sent"} — ${parts.join(" · ")}` : "No channel on this rule." });
    } catch (e) { setMsg({ tone: "error", text: String((e as Error).message || e) }); }
  };

  return (
    <div className="mx-auto max-w-5xl space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={onBack} className={btn.small}>← Alerts</button>
        <h2 className="min-w-0 truncate text-[15px] font-semibold text-slate-100">{r.name}</h2>
        <StateChip s={st} n={r.state?.firing} />
        <SeverityChip s={r.severity} />
        {canWrite && (
          <span className="ml-auto flex flex-wrap gap-1.5">
            <button type="button" className={btn.small} onClick={() => onEdit(r)}>Edit</button>
            <button type="button" className={btn.small} onClick={test}>Send a test</button>
            <button type="button" className={btn.small} onClick={() => act(() => createSilence({ rule_id: r.id, matchers: {}, for: "1h", reason: "silenced from the rule page" }), "Silenced for 1 h.")}>Silence 1 h</button>
            <button type="button" className={btn.small} onClick={() => act(() => setRuleEnabled(r.id, !r.enabled), r.enabled ? "Turned off." : "Turned on.")}>{r.enabled ? "Turn off" : "Turn on"}</button>
            <button type="button" className={btn.danger} onClick={() => confirm(`Delete the rule « ${r.name} »? Its history goes with it.`) && act(async () => { await deleteRule(r.id); onBack(); }, "Deleted.")}>Delete</button>
          </span>
        )}
      </div>
      <p className="text-[13.5px] leading-snug text-slate-200">{r.sentence || ""}</p>
      {r.description && <p className="whitespace-pre-wrap text-[12px] text-mut">{r.description}</p>}
      {r.state?.error && <Banner tone="error"><b>The source answered with an error at the last check</b> — {r.state.error}</Banner>}
      {msg && <Banner tone={msg.tone}>{msg.text}</Banner>}

      <div className="rounded-xl border border-line bg-panel/50 p-3">
        <div className="mb-2 flex flex-wrap items-center gap-2 text-[11.5px] text-mut">
          <span>last check {r.state?.last_eval ? `${since(r.state.last_eval)} ago` : "—"}</span>
          {r.state?.last_value !== undefined && r.state?.last_value !== null && <span>· value {fmtValue(r.state.last_value, unit)}</span>}
          <span>· every {r.every}{durS(r.for) ? `, holds ${r.for}` : ""}</span>
          <span className="flex items-center gap-1">· ▼ <span className="text-red-300">real firings</span> marked on top</span>
          <span className="ml-auto"><Seg label="time range" size="sm" value={range} onChange={setRange} opts={RANGES.map((x) => ({ id: x.id, label: x.label }))} /></span>
        </div>
        <Chart result={pv} loading={loading} unit={unit} transitions={hist} rangeLabel={RANGES.find((x) => x.id === range)?.label}
          threshold={pv?.threshold ?? null} forS={durS(r.for)} height={240} />
      </div>

      {mine.length > 0 && (
        <section aria-label="active alerts of this rule" className="space-y-1.5">
          <h3 className="text-[12px] font-semibold text-slate-200">Active now ({mine.length})</h3>
          {mine.map((a) => <AlertRow key={a.id} a={a} canWrite={canWrite} onChanged={() => { load(); onChanged(); }} onOpenIncident={onOpenIncident} hideRule />)}
        </section>
      )}

      <div className="grid gap-3 md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
        <section aria-label="history" className="rounded-xl border border-line bg-panel/50 p-3">
          <h3 className="mb-2 text-[12px] font-semibold text-slate-200">History</h3>
          {hist.length === 0 ? <p className="text-[12px] text-mut">No state change yet — it has always been OK since it was created.</p> : (
            <ol className="relative space-y-2 border-l border-line pl-3">
              {hist.slice(0, 40).map((h, i) => (
                <li key={i} className="relative text-[12px]">
                  <span aria-hidden className={`absolute -left-[17px] top-1 h-2 w-2 rounded-full ${h.to === "firing" ? "bg-red-400" : h.to === "pending" ? "bg-amber-300" : h.to === "silenced" ? "bg-slate-400" : "bg-emerald-400"}`} />
                  <div className="flex flex-wrap items-center gap-x-2">
                    <span className="text-mut">{fmtTime(h.ts, true)}</span>
                    <span className="text-slate-200">{h.from} → <b className={h.to === "firing" ? "text-red-300" : h.to === "ok" || h.to === "resolved" ? "text-emerald-300" : "text-slate-100"}>{h.to}</b></span>
                    {h.value !== null && h.value !== undefined && <span className="tabular-nums text-slate-300">{fmtValue(h.value, unit)}{h.threshold !== null && h.threshold !== undefined ? ` / ${fmtValue(h.threshold, unit)}` : ""}</span>}
                    {groupText(h.group, h.group_key) && <span className="font-mono text-[10.5px] text-mut">{groupText(h.group, h.group_key)}</span>}
                  </div>
                  {h.note && <div className="text-[11px] text-mut">{h.note}</div>}
                </li>
              ))}
            </ol>
          )}
        </section>
        <section aria-label="configuration" className="space-y-2 rounded-xl border border-line bg-panel/50 p-3 text-[12px]">
          <h3 className="text-[12px] font-semibold text-slate-200">Set up</h3>
          <Row k="Source">{src ? src.name : `#${r.source_id}`}</Row>
          <Row k="Tells">{r.channels.length ? <span className="flex flex-wrap gap-2">{r.channels.map((cid) => { const c = channels.find((x) => x.id === cid); return c ? <ChannelBadge key={cid} c={c} /> : <span key={cid}>#{cid}</span>; })}</span> : <span className="text-mut">nobody (incident only)</span>}</Row>
          <Row k="Remind every">{r.realert}</Row>
          {r.quiet_hours?.enabled && <Row k="Quiet hours">{r.quiet_hours.start}–{r.quiet_hours.end}</Row>}
          <Row k="Then">{[r.actions.incident && "open an incident", r.actions.diag_session && "start a diagnosis session", r.actions.agent_id && `propose agent #${r.actions.agent_id}`, r.actions.runbook && `runbook ${r.actions.runbook}`].filter(Boolean).join(" · ") || <span className="text-mut">notify only</span>}</Row>
          {r.group_by.length > 0 && <Row k="One alert per">{r.group_by.join(" + ")}</Row>}
          <Row k="Created">{r.created_by || "—"}{r.created_at ? `, ${fmtTime(r.created_at, true)}` : ""}</Row>
          {r.query_compiled && (
            <details><summary className="ui-focus cursor-pointer text-[11px] text-mut hover:text-slate-300">query</summary>
              <code className="mt-1 block whitespace-pre-wrap break-all rounded bg-ink/70 p-2 font-mono text-[11px] text-slate-300">{r.query_compiled}</code></details>
          )}
        </section>
      </div>
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-2"><span className="text-mut">{k}</span><span className="min-w-0 text-slate-200">{children}</span></div>;
}
