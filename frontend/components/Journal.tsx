"use client";
import { useEffect, useState } from "react";
import { fetchAudit } from "@/lib/api";
import type { AuditEvent } from "@/lib/types";
import { ago, stamp } from "@/lib/fmt";

// famille d'action → teinte du badge
function tone(action: string): string {
  if (action.includes("delete") || action.includes("close")) return "bg-red-500/15 text-red-300";
  if (action.includes("spawn") || action.includes("create")) return "bg-emerald-500/15 text-emerald-300";
  if (action.startsWith("session.")) return "bg-sky-500/15 text-sky-300";
  if (action.startsWith("preview.")) return "bg-violet-500/15 text-violet-300";
  if (action.startsWith("iam.")) return "bg-amber-500/15 text-amber-300";
  return "bg-panel2 text-slate-300";
}

export default function Journal() {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [q, setQ] = useState("");
  const [err, setErr] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetchAudit(300, q)
        .then((e) => { if (alive) { setEvents(e); setErr(false); } })
        .catch(() => alive && setErr(true));
    load();
    const iv = setInterval(load, 8000);
    return () => { alive = false; clearInterval(iv); };
  }, [q]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-line bg-panel/60 px-3 py-2">
        <span className="text-[12.5px] font-semibold text-slate-200">Action log</span>
        <span className="text-[11px] text-mut">— who did what, when (not session content)</span>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="filter (user, action, resource)…"
          className="ml-auto w-64 rounded border border-line bg-panel2 px-2 py-1 text-[12px] text-slate-200 outline-none focus:border-sea/50" />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {err ? (
          <div className="mt-10 text-center text-[12.5px] text-mut">log not accessible (dev role required)</div>
        ) : !events ? (
          <div className="mt-10 text-center text-[12.5px] text-mut">loading…</div>
        ) : !events.length ? (
          <div className="mt-10 text-center text-[12.5px] text-mut">no logged actions{q && " for this filter"}</div>
        ) : (
          <table className="w-full border-separate border-spacing-0 text-[12px]">
            <thead>
              <tr className="text-left text-[11px] text-mut">
                <th className="border-b border-line px-2 pb-1.5 font-medium">when</th>
                <th className="border-b border-line px-2 pb-1.5 font-medium">who</th>
                <th className="border-b border-line px-2 pb-1.5 font-medium">action</th>
                <th className="border-b border-line px-2 pb-1.5 font-medium">resource</th>
                <th className="border-b border-line px-2 pb-1.5 font-medium">detail</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev, i) => (
                <tr key={i} className="hover:bg-panel2/40">
                  <td className="whitespace-nowrap px-2 py-1.5 tabular-nums text-mut" title={stamp(ev.ts)}>{ago(ev.ts)}</td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-slate-300">{ev.user.split("@")[0] || "—"}</td>
                  <td className="whitespace-nowrap px-2 py-1.5">
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${tone(ev.action)}`}>{ev.action}</span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-slate-300">{ev.resource}</td>
                  <td className="max-w-md truncate px-2 py-1.5 text-mut" title={ev.detail}>{ev.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
