"use client";
import { useEffect, useState } from "react";
import { useClassification } from "@/lib/classification";
import LevelBadge from "./LevelBadge";

const inp = "rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50";
const btn = "rounded bg-sea/80 px-2 py-0.5 text-[11px] text-white hover:bg-sea disabled:opacity-40";

interface Channel { channel_id: string; project: string; level: number; name: string }
interface State {
  enabled: boolean; missing: string[]; tenant: string; app_id: string; endpoint: string;
  channels: Channel[]; graph_permissions: { permission: string; type: string; why: string }[];
}

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, { method, credentials: "same-origin",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) { let d = `${r.status}`; try { d = (await r.json()).detail || d; } catch { /* */ } throw new Error(d); }
  return r.json() as Promise<T>;
}

/** 3.4 — Microsoft Teams: configuration state and the channel ↔ project mapping (instance
 *  admins). The level of a channel is its audience: Nina never says more there. */
export default function TeamsAdmin() {
  const info = useClassification();
  const [d, setD] = useState<State | null>(null);
  const [err, setErr] = useState("");
  const [cid, setCid] = useState("");
  const [project, setProject] = useState("");
  const [level, setLevel] = useState("project");
  const load = () => call<State>("/api/admin/teams").then(setD).catch((e) => setErr(String(e.message || e)));
  useEffect(() => { load(); }, []);
  const run = (p: Promise<unknown>) => p.then(() => { setErr(""); load(); }).catch((e) => setErr(String(e.message || e)));
  if (!d) return <div className="text-[12px] text-mut">{err || "…"}</div>;
  return (
    <div className="space-y-3 text-[12.5px]">
      {err && <div className="rounded border border-red-400/40 bg-red-500/10 px-2 py-1 text-[11.5px] text-red-300">{err}</div>}
      <div className="rounded-lg border border-line bg-panel2/40 p-3 text-slate-300">
        <div>Feature: <b>{d.enabled ? "on" : "off"}</b>{d.missing.length > 0 && <span className="text-amber-300"> — missing {d.missing.join(", ")}</span>}</div>
        <div className="mt-1 text-[11.5px] text-mut">Tenant {d.tenant || "—"} · app {d.app_id || "—"} · endpoint <span className="font-mono">{d.endpoint}</span></div>
        <div className="mt-1 text-[11.5px] text-mut">Graph (application, admin consent): {d.graph_permissions.map((p) => p.permission).join(", ")} — see docs/enterprise/TEAMS.md</div>
        <a className="mt-1 inline-block text-[12px] text-sky-300 hover:underline" href="/api/admin/teams/manifest" target="_blank" rel="noreferrer">manifest.json</a>
      </div>
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="mb-1.5 text-[11px] text-mut">Channel or chat → project (channel id: 19:…@thread.tacv2)</div>
        <div className="flex flex-wrap gap-1.5">
          <input aria-label="Channel id" placeholder="19:…@thread.tacv2" value={cid} onChange={(e) => setCid(e.target.value)} className={`${inp} flex-1`} />
          <input aria-label="Project" placeholder="project slug" value={project} onChange={(e) => setProject(e.target.value)} className={`${inp} w-32`} />
          {info?.enabled && (
            <select aria-label="Channel level" value={level} onChange={(e) => setLevel(e.target.value)} className={inp}>
              {info.scale.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
            </select>
          )}
          <button disabled={!cid || !project} className={btn}
            onClick={() => run(call("/api/admin/teams/channels", "PUT", { channel_id: cid, project, level }).then(() => setCid("")))}>map</button>
        </div>
        <table className="mt-2 w-full text-[12px]">
          <tbody>
            {d.channels.length === 0 && <tr><td className="text-mut">no channel mapped yet</td></tr>}
            {d.channels.map((c) => (
              <tr key={c.channel_id} className="border-t border-line/50">
                <td className="max-w-[16rem] truncate py-1 font-mono text-slate-200" title={c.channel_id}>{c.name || c.channel_id}</td>
                <td className="text-mut">{c.project}</td>
                <td><LevelBadge level={c.level} /></td>
                <td className="text-right"><button className="text-[11px] text-red-300 hover:underline"
                  onClick={() => run(call(`/api/admin/teams/channels?channel_id=${encodeURIComponent(c.channel_id)}`, "DELETE"))}>remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
