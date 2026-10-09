"use client";
// 3.5 — one active alert: what fired, where (group), the value against its threshold, since when,
// who took it — and the three things people do with an alert: take it (ack), mute it for a while,
// open an incident.
import { useState } from "react";
import { ackAlert, openIncident, silenceAlert } from "@/lib/alerting";
import { alertValueText, groupTip, groupTitle, since, type Alert } from "@/lib/alertingModel";
import { SeverityChip, StateChip, btn } from "./bits";

/** « until tomorrow 08:00 » as a duration string for the API */
function untilMorning(): string {
  const now = new Date();
  const t = new Date(now); t.setDate(now.getDate() + (now.getHours() >= 8 ? 1 : 0)); t.setHours(8, 0, 0, 0);
  return `${Math.max(60, Math.round((t.getTime() - now.getTime()) / 60000))}m`;
}

export function AlertRow({ a, canWrite, onChanged, onOpenRule, onOpenIncident, hideRule = false, focus = false }: {
  a: Alert; canWrite: boolean; onChanged: () => void; onOpenRule?: (id: number) => void; onOpenIncident?: (id: number) => void;
  hideRule?: boolean; focus?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [mute, setMute] = useState(false);
  const run = async (f: () => Promise<unknown>) => {
    setBusy(true); setErr("");
    try { await f(); onChanged(); } catch (e) { setErr(String((e as Error).message || e)); } finally { setBusy(false); setMute(false); }
  };
  const g = groupTitle(a);
  const v = alertValueText(a);
  const tone = a.state === "firing" ? (a.severity === "critical" ? "border-red-500/60 bg-red-500/[0.07]" : "border-red-500/35 bg-red-500/[0.04]")
    : a.state === "pending" ? "border-amber-500/35 bg-amber-500/[0.04]" : "border-line bg-panel2/40";
  return (
    <div id={`alert-${a.id}`} className={`rounded-xl border p-2.5 ${tone} ${focus ? "ring-2 ring-sea" : ""}`}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <StateChip s={a.state} />
        <SeverityChip s={a.severity} compact />
        {!hideRule && (onOpenRule
          ? <button type="button" onClick={() => onOpenRule(a.rule_id)} className="ui-focus min-h-6 min-w-0 truncate text-left text-[13px] font-medium text-slate-100 hover:underline">{a.rule_name}</button>
          : <span className="min-w-0 truncate text-[13px] font-medium text-slate-100">{a.rule_name}</span>)}
        {g && <span className="rounded bg-panel2 px-1.5 text-[11.5px] font-medium text-slate-200" title={groupTip(a)}>{g}</span>}
        <span className="ml-auto whitespace-nowrap text-[11px] text-mut" title={new Date(a.started_at * 1000).toLocaleString()}>
          {a.state === "pending" ? "pending for" : "since"} {since(a.fired_at || a.started_at)}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
        {v ? (
          <span className={`tabular-nums ${a.state === "resolved" ? "text-slate-300" : a.state === "pending" ? "text-amber-100" : "text-red-100"}`}>
            <b className="font-semibold">{v.value}</b>{v.rest && <span className="text-mut"> {v.rest}</span>}
          </span>
        ) : a.summary ? <span className="min-w-0 text-mut">{a.summary}</span> : null}
        {a.acked_by && <span className="text-[11px] text-emerald-300">✓ taken by {a.acked_by}</span>}
        {a.silenced_until && a.silenced_until > Date.now() / 1000 && <span className="text-[11px] text-slate-300">‖ silenced {since(Date.now() / 1000, a.silenced_until)} more</span>}
      </div>
      {(a.sample_events || []).length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {a.sample_events!.slice(0, 2).map((e, i) => <li key={i} className="truncate rounded bg-ink/50 px-2 py-0.5 font-mono text-[10.5px] text-slate-300" title={e.text}>{e.text}</li>)}
        </ul>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {canWrite && !a.acked_by && a.state !== "resolved" && <button type="button" disabled={busy} className={btn.small} onClick={() => run(() => ackAlert(a.id))} title="tell the others you are on it">✓ I'm on it</button>}
        {canWrite && a.state !== "resolved" && (
          <span className="relative" onKeyDown={(e) => { if (e.key === "Escape") setMute(false); }}
            onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setMute(false); }}>
            <button type="button" disabled={busy} className={btn.small} aria-expanded={mute} aria-haspopup="menu" onClick={() => setMute((v) => !v)}>‖ Silence…</button>
            {mute && (
              <span role="menu" className="absolute left-0 top-full z-20 mt-1 flex w-44 flex-col rounded-lg border border-line bg-panel p-1 shadow-xl shadow-black/40">
                {[["1h", "for 1 hour"], ["4h", "for 4 hours"], [untilMorning(), "until tomorrow 08:00"], ["7d", "for a week"]].map(([d, l]) => (
                  <button key={l} type="button" role="menuitem" className="ui-focus rounded px-2 py-1 text-left text-[12px] text-slate-200 hover:bg-sea/15" onClick={() => run(() => silenceAlert(a.id, d))}>{l}</button>
                ))}
              </span>
            )}
          </span>
        )}
        {a.incident_id ? (onOpenIncident
          ? <button type="button" className={btn.small} onClick={() => onOpenIncident(a.incident_id as number)}>incident #{a.incident_id} →</button>
          : <span className="text-[11px] text-slate-300">incident #{a.incident_id} opened — the ops team follows it in Operate › Incidents</span>
        ) : canWrite && a.state === "firing" ? (
          <button type="button" disabled={busy} className={btn.small} onClick={() => run(async () => { const r = await openIncident(a.id); onOpenIncident?.(r.incident_id); })}>Open an incident</button>
        ) : null}
        {err && <span role="alert" className="text-[11px] text-red-300">{err}</span>}
      </div>
    </div>
  );
}
