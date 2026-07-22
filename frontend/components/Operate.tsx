"use client";
import { useEffect, useState } from "react";
import { obsStatus, obsDashboards, obsIncidentSet, type ObsStatus, type Dashboard, type Incident } from "@/lib/api";

const SEV: Record<string, string> = {
  critical: "text-red-400 border-red-500/40 bg-red-500/10",
  warning: "text-amber-300 border-amber-500/40 bg-amber-500/10",
  info: "text-sky-300 border-sky-500/40 bg-sky-500/10",
};
const ago = (ts: number) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

// « Operate » — opérer la prod depuis le cockpit : les dashboards Grafana de ta
// flotte, et le fil d'incidents. Chaque alerte a spawné une session de
// diagnostic (l'agent a la mémoire du projet) que tu peux ouvrir en un clic.
export default function Operate({ onOpenSession }: { onOpenSession?: (sid: string) => void }) {
  const [st, setSt] = useState<ObsStatus | null>(null);
  const [dashes, setDashes] = useState<Dashboard[]>([]);
  const reload = () => {
    obsStatus().then(setSt).catch(() => setSt(null));
    obsDashboards().then(setDashes).catch(() => {});
  };
  useEffect(() => { reload(); const iv = setInterval(reload, 10000); return () => clearInterval(iv); }, []);

  if (!st) return <div className="p-4 text-[12px] text-mut">Loading…</div>;
  if (!st.enabled)
    return (
      <div className="p-4 text-[12px] text-mut">
        No observability stack connected. On managed cloud, add the{" "}
        <span className="text-slate-300">Observability</span> resource in{" "}
        <span className="text-slate-300">Infra → My fleet</span> (Prometheus + Grafana + Loki in your
        private network). Self-hosted: set <span className="font-mono text-slate-300">SOKKAN_PROM</span> /{" "}
        <span className="font-mono text-slate-300">SOKKAN_GRAFANA_URL</span>.
      </div>
    );

  const incidents = st.incidents || [];
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-line bg-panel/60 px-3 py-1.5 text-[11px] text-mut">
        <span className={`h-2 w-2 rounded-full ${st.prometheus ? "bg-emerald-500" : "bg-slate-600"}`} /> Prometheus
        <span className={`ml-2 h-2 w-2 rounded-full ${st.grafana ? "bg-emerald-500" : "bg-slate-600"}`} /> Grafana
        <span className={`ml-2 h-2 w-2 rounded-full ${st.loki ? "bg-emerald-500" : "bg-slate-600"}`} /> Loki
        {st.grafana_public_url && (
          <a href={st.grafana_public_url} target="_blank" rel="noreferrer" className="ml-auto text-sea hover:underline">open Grafana ↗</a>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {/* Incidents — le fil vivant */}
        <div className="mb-4 max-w-4xl">
          <div className="mb-1.5 text-[12px] font-semibold text-slate-200">Incidents</div>
          <div className="space-y-1.5">
            {incidents.length === 0 && <div className="text-[12px] text-mut">No incidents. Alerts fired in production land here, each with a diagnosis session already started.</div>}
            {incidents.map((i: Incident) => (
              <div key={i.id} className={`rounded-lg border p-2 text-[12px] ${SEV[i.severity] || "border-line bg-panel2/50"}`}>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-100">{i.title}</span>
                  <span className="rounded border border-line px-1.5 py-px text-[10px] text-mut">{i.severity}</span>
                  <span className="text-[10.5px] text-mut">{ago(i.ts)}</span>
                  <span className={`ml-auto text-[11px] ${i.status === "resolved" ? "text-emerald-400" : "text-amber-300"}`}>{i.status}</span>
                </div>
                {i.summary && <div className="mt-0.5 text-[11px] text-mut">{i.summary}</div>}
                <div className="mt-1 flex items-center gap-2">
                  {i.session_id && onOpenSession && (
                    <button onClick={() => onOpenSession(i.session_id)}
                      className="rounded border border-sea/40 bg-sea/10 px-2 py-0.5 text-[10.5px] text-sea hover:border-sea">open diagnosis session →</button>
                  )}
                  {i.status !== "resolved" && (
                    <button onClick={() => obsIncidentSet(i.id, "resolved").then(reload)}
                      className="rounded border border-line px-2 py-0.5 text-[10.5px] text-mut hover:text-emerald-300">mark resolved</button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Dashboards Grafana */}
        <div className="mb-2 max-w-4xl">
          <div className="mb-1.5 text-[12px] font-semibold text-slate-200">Dashboards
            <span className="ml-2 text-[10.5px] font-normal text-mut">ask a session: « build a dashboard for my API latency and errors »</span>
          </div>
          <div className="grid gap-1.5 [grid-template-columns:repeat(auto-fill,minmax(220px,1fr))]">
            {dashes.map((d) => (
              <a key={d.uid} href={(st.grafana_public_url || "") + (d.url || "")} target="_blank" rel="noreferrer"
                className="truncate rounded-lg border border-line bg-panel2/50 px-2.5 py-2 text-[12px] text-slate-200 hover:border-sea/50">📊 {d.title}</a>
            ))}
            {dashes.length === 0 && <div className="text-[12px] text-mut">No dashboards yet.</div>}
          </div>
        </div>

        {/* Grafana embed */}
        {st.grafana_public_url && (
          <div className="mt-3 max-w-6xl overflow-hidden rounded-lg border border-line">
            <iframe src={st.grafana_public_url} title="Grafana" className="h-[70vh] w-full bg-white" />
          </div>
        )}
      </div>
    </div>
  );
}
