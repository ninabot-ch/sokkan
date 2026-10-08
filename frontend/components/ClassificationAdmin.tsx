"use client";
import { useEffect, useState } from "react";
import {
  adminClassification, adminDeleteGroupLevel, adminSetGroupLevel, adminSetRoleLevel, auditCsvUrl, fetchAudit,
  invalidateClassification, useClassification, type AccessRow, type ClassificationAdmin as Admin,
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
  // a mapping or a role level changed: the admin table AND « Your clearance » reload (3.4.3)
  const run = (p: Promise<unknown>) => p.then(() => { setErr(""); load(); invalidateClassification(); }).catch((e) => setErr(String(e.message || e)));
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
      </div>
      {info.can_audit && <AccessLog />}
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

/** 3.4 — the access log of the selected project, readable here (it was a CSV download only):
 *  who obtained which note, through which path (cockpit, nina, teams, a session, an agent),
 *  filters, the export of exactly what is shown, and how many reads are above one's clearance. */
function AccessLog() {
  const [days, setDays] = useState(30);
  const [actor, setActor] = useState("");
  const [note, setNote] = useState("");
  const [d, setD] = useState<{ entries: AccessRow[]; hidden_above_clearance: number; clearance: string | null; project: string } | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    const t = setTimeout(() => fetchAudit(days, actor.trim(), note.trim()).then((x) => { setD(x); setErr(""); }).catch((e) => setErr(String(e.message || e))), 250);
    return () => clearTimeout(t);
  }, [days, actor, note]);
  return (
    <div className="rounded-lg border border-line bg-panel2/40 p-3">
      <div>
        <div className="text-[13px] font-semibold text-slate-100">Access log{d ? ` — ${d.project}` : ""}</div>
        <div className="text-[11px] text-mut">Who obtained which note of this project, and through which path. Every read is recorded.</div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input aria-label="Filter by person" placeholder="person (email)" value={actor} onChange={(e) => setActor(e.target.value)} className={`${inp} w-44`} />
        <input aria-label="Filter by note" placeholder="note name" value={note} onChange={(e) => setNote(e.target.value)} className={`${inp} w-40`} />
        <select aria-label="Period" value={days} onChange={(e) => setDays(+e.target.value)} className={inp}>
          {[1, 7, 30, 90, 365].map((n) => <option key={n} value={n}>{n === 1 ? "last 24 h" : `last ${n} days`}</option>)}
        </select>
        <a href={auditCsvUrl(days, actor.trim(), note.trim())} className="ml-auto rounded border border-line px-2 py-1 text-[12px] text-sky-300 underline-offset-2 hover:border-sea/50 hover:underline">⤓ Export CSV</a>
      </div>
      {err && <div className="mt-2 text-[11.5px] text-red-300">{err}</div>}
      {d && d.hidden_above_clearance > 0 && (
        <div className="mt-2 rounded border border-amber-400/40 bg-amber-500/10 px-2 py-1 text-[11.5px] text-amber-200">
          {d.hidden_above_clearance} read(s) of notes above your clearance ({d.clearance}) are recorded but not shown to you.
        </div>
      )}
      <div className="mt-2 max-h-80 overflow-y-auto" tabIndex={0} aria-label="Access log entries">
        <table className="w-full table-fixed text-left text-[12px]">
          <colgroup><col className="w-[9.5rem]" /><col className="w-[10.5rem]" /><col className="w-[4.5rem]" /><col className="w-[10rem]" /><col className="w-[6.5rem]" /><col /></colgroup>
          <thead className="sticky top-0 bg-panel text-[10.5px] uppercase tracking-wide text-mut">
            <tr><th className="py-1 pr-2">when</th><th className="pr-2">who</th><th className="pr-2">through</th><th className="pr-2">note</th><th className="pr-2">level</th><th>question</th></tr>
          </thead>
          <tbody className="divide-y divide-line/50">
            {d?.entries.map((r, i) => (
              <tr key={i} className="align-top">
                <td className="whitespace-nowrap py-1 pr-2 text-mut" title={r.at}>{new Date(r.at).toLocaleString()}</td>
                <td className="break-words pr-2 text-slate-200">{r.actor || "—"}{r.actor_source ? <span className="text-mut"> ({r.actor_source})</span> : null}</td>
                <td className="pr-2 text-slate-300">{r.via}</td>
                <td className="break-words pr-2 font-mono text-[11.5px] text-slate-200">{r.note_name}</td>
                <td className="pr-2"><LevelBadge level={r.level} /></td>
                <td className="text-mut" title={r.query || ""}><span className="line-clamp-2">{r.query || ""}</span></td>
              </tr>
            ))}
            {d && d.entries.length === 0 && <tr><td colSpan={6} className="py-3 text-mut">No read in this period.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
