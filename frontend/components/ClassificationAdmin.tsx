"use client";
import { useEffect, useState } from "react";
import {
  adminClassification, adminDeleteGroupLevel, adminSetGroupLevel, adminSetRoleLevel, auditCsvUrl,
  useClassification, type ClassificationAdmin as Admin,
} from "@/lib/classification";
import LevelBadge from "./LevelBadge";

const inp = "rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50";
const btn = "rounded bg-sea/80 px-2 py-0.5 text-[11px] text-white hover:bg-sea disabled:opacity-40";

/** 3.4 — classification: my clearance in the selected project, the access log export (project
 *  admins), and for instance admins the mapping SSO group → level and role → level. */
export default function ClassificationAdmin({ instanceAdmin }: { instanceAdmin: boolean }) {
  const info = useClassification();
  const [d, setD] = useState<Admin | null>(null);
  const [err, setErr] = useState("");
  const [team, setTeam] = useState("");
  const [level, setLevel] = useState("confidential");
  const [project, setProject] = useState("*");
  const load = () => { if (instanceAdmin) adminClassification().then(setD).catch((e) => setErr(String(e.message || e))); };
  useEffect(load, [instanceAdmin]);
  const run = (p: Promise<unknown>) => p.then(() => { setErr(""); load(); }).catch((e) => setErr(String(e.message || e)));
  if (!info) return <div className="text-[12px] text-mut">…</div>;
  const scale = info.scale;
  return (
    <div className="space-y-3 text-[12.5px]">
      {err && <div className="rounded border border-red-400/40 bg-red-500/10 px-2 py-1 text-[11.5px] text-red-300">{err}</div>}
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="text-[11px] text-mut">Scale (the customer&apos;s labels) — default level: {info.default}</div>
        <div className="mt-1.5 flex flex-wrap gap-1.5">{scale.map((l) => <LevelBadge key={l.id} level={l.rank} />)}</div>
        {!info.enabled && <div className="mt-1.5 text-[11.5px] text-amber-300">Classification is off: nothing above « project » is reachable by anyone.</div>}
        {info.enabled && info.project && (
          <div className="mt-1.5 text-[12px] text-slate-300">Your clearance in <b>{info.project}</b>: <LevelBadge level={info.clearance} /></div>
        )}
        {info.can_audit && (
          <a href={auditCsvUrl(30)} className="mt-2 inline-block text-[12px] text-sky-300 hover:underline">
            Export the access log of this project (CSV, 30 days)</a>
        )}
      </div>
      {instanceAdmin && d && (
        <>
          <div className="rounded-lg border border-line bg-panel2/40 p-3">
            <div className="mb-1.5 text-[11px] text-mut">SSO group → level (a person&apos;s clearance = the highest of their groups and of their project role)</div>
            <div className="flex flex-wrap gap-1.5">
              <input aria-label="SSO group" list="cls-teams" placeholder="IdP group (or user:email)" value={team} onChange={(e) => setTeam(e.target.value)} className={`${inp} flex-1`} />
              <datalist id="cls-teams">{d.teams.map((t) => <option key={t.id} value={t.name} />)}</datalist>
              <select aria-label="Level" value={level} onChange={(e) => setLevel(e.target.value)} className={inp}>
                {scale.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
              </select>
              <select aria-label="Project" value={project} onChange={(e) => setProject(e.target.value)} className={inp}>
                <option value="*">every project</option>
                {d.projects.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <button disabled={!team} className={btn} onClick={() => run(adminSetGroupLevel(team, level, project).then(() => setTeam("")))}>map</button>
            </div>
            <table className="mt-2 w-full text-[12px]">
              <tbody>
                {d.groups.length === 0 && <tr><td className="text-mut">no mapping yet — every role reads up to its role level</td></tr>}
                {d.groups.map((g) => (
                  <tr key={`${g.team_id}|${g.project}`} className="border-t border-line/50">
                    <td className="py-1 font-mono text-slate-200">{g.team_id}</td>
                    <td className="text-mut">{g.project === "*" ? "every project" : g.project}</td>
                    <td><LevelBadge level={g.level} /></td>
                    <td className="text-right"><button className="text-[11px] text-red-300 hover:underline" onClick={() => run(adminDeleteGroupLevel(g.team_id, g.project))}>remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="rounded-lg border border-line bg-panel2/40 p-3">
            <div className="mb-1.5 text-[11px] text-mut">Level each project role reads up to</div>
            <div className="grid grid-cols-2 gap-1.5">
              {Object.entries(d.roles).map(([role, lv]) => (
                <label key={role} className="flex items-center gap-2 text-slate-300">
                  <span className="w-20">{role}</span>
                  <select aria-label={`Level of ${role}`} value={scale[lv]?.id} onChange={(e) => run(adminSetRoleLevel(role, e.target.value))} className={inp}>
                    {scale.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
                  </select>
                </label>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
