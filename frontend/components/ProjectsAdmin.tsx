"use client";
import { useEffect, useState } from "react";
import {
  adminProjects, adminCreateProject, adminArchiveProject, adminGrant, adminRevoke, adminOpsGroup,
  type AdminProjects,
} from "@/lib/api";

const inp = "rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50";
const btn = "rounded bg-sea/80 px-2 py-0.5 text-[11px] text-white hover:bg-sea disabled:opacity-40";

/** 3.2 — instance administration: projects, who has which role in them (a person or an
 *  SSO team), the ops team. No project CONTENT here: an admin who needs to see a
 *  project's sessions adds themself to it — and the journal says so. */
export default function ProjectsAdmin() {
  const [d, setD] = useState<AdminProjects | null>(null);
  const [err, setErr] = useState("");
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [ops, setOps] = useState("");
  const load = () => adminProjects().then((x) => { setD(x); setOps(x.ops_group); }).catch((e) => setErr(String(e.message || e)));
  useEffect(() => { load(); }, []);
  const run = (p: Promise<unknown>) => p.then(() => { setErr(""); load(); }).catch((e) => setErr(String(e.message || e)));
  if (!d) return <div className="text-[12px] text-mut">{err || "…"}</div>;
  return (
    <div className="space-y-3 text-[12.5px]">
      {err && <div className="rounded border border-red-400/40 bg-red-500/10 px-2 py-1 text-[11.5px] text-red-300">{err}</div>}
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="mb-1.5 text-[11px] text-mut">New project — its members come from grants (a person, or an SSO group seen at login)</div>
        <div className="flex flex-wrap gap-1.5">
          <input aria-label="Project slug" placeholder="slug (radio-player)" value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} className={`${inp} w-40`} />
          <input aria-label="Project name" placeholder="name" value={name} onChange={(e) => setName(e.target.value)} className={`${inp} flex-1`} />
          <button disabled={!slug} className={btn} onClick={() => run(adminCreateProject(slug, name || slug).then(() => { setSlug(""); setName(""); }))}>create</button>
        </div>
      </div>
      {d.projects.map((p) => <ProjectCard key={p.slug} p={p} roles={d.roles} teams={d.teams.map((t) => t.id)} run={run} />)}
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="text-[11px] text-mut">Ops team — the SSO group whose members get the Operate tab (with the instance admins)</div>
        <div className="mt-1.5 flex gap-1.5">
          <input aria-label="Ops group" placeholder="SSO group name" value={ops} onChange={(e) => setOps(e.target.value)} className={`${inp} flex-1`} />
          <button className={btn} onClick={() => run(adminOpsGroup(ops))}>save</button>
        </div>
      </div>
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="text-[11px] text-mut">Teams seen at login (IdP groups)</div>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {d.teams.length === 0 && <span className="text-mut">none yet — they appear when members log in through SSO</span>}
          {d.teams.map((t) => <span key={t.id} className="rounded-full border border-line px-2 py-0.5 text-[11px] text-slate-300">{t.name} · {t.members}</span>)}
        </div>
      </div>
    </div>
  );
}

function ProjectCard({ p, roles, teams, run }: {
  p: AdminProjects["projects"][number]; roles: string[]; teams: string[]; run: (x: Promise<unknown>) => void;
}) {
  const [kind, setKind] = useState<"team" | "user">("team");
  const [who, setWho] = useState("");
  const [role, setRole] = useState("dev");
  const fixed = p.slug === "default" || p.slug === "shared";
  return (
    <div className={`rounded-lg border border-line p-3 ${p.archived_at ? "opacity-50" : "bg-panel2/40"}`}>
      <div className="flex items-center gap-2">
        <span className="text-[13.5px] font-medium text-slate-100">{p.name}</span>
        <code className="text-[11px] text-mut">{p.slug}</code>
        <span className="rounded-full border border-line px-1.5 text-[10.5px] text-mut">
          {p.access_source === "instance" ? "instance roles" : p.slug === "shared" ? "read by everyone" : "grants"}
        </span>
        {!fixed && (
          <button className="ml-auto text-[11px] text-mut hover:text-red-300" onClick={() => run(adminArchiveProject(p.slug, !p.archived_at))}>
            {p.archived_at ? "unarchive" : "archive"}
          </button>
        )}
      </div>
      {p.access_source !== "instance" && (
        <>
          <ul className="mt-1.5 space-y-0.5">
            {p.grants.map((g) => (
              <li key={`${g.principal_kind}:${g.principal}`} className="flex items-center gap-2 text-[12px]">
                <span className="text-mut">{g.principal_kind === "team" ? "👥" : "👤"}</span>
                <span className="text-slate-200">{g.principal}</span>
                <span className="text-emerald-300">{g.role}</span>
                <button className="ml-auto text-[11px] text-mut hover:text-red-300"
                  onClick={() => run(adminRevoke(p.slug, g.principal_kind, g.principal))}>remove</button>
              </li>
            ))}
          </ul>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <select aria-label="Grant to" value={kind} onChange={(e) => setKind(e.target.value as "team" | "user")} className={inp}>
              <option value="team">SSO team</option><option value="user">person</option>
            </select>
            <input aria-label="Who" list={`teams-${p.slug}`} placeholder={kind === "team" ? "sso:group-name" : "email"}
              value={who} onChange={(e) => setWho(e.target.value)} className={`${inp} flex-1`} />
            <datalist id={`teams-${p.slug}`}>{teams.map((t) => <option key={t} value={t} />)}</datalist>
            <select aria-label="Role" value={role} onChange={(e) => setRole(e.target.value)} className={inp}>
              {roles.map((r) => <option key={r}>{r}</option>)}
            </select>
            <button disabled={!who} className={btn} onClick={() => run(adminGrant(p.slug, kind, who, role).then(() => setWho("")))}>grant</button>
          </div>
        </>
      )}
    </div>
  );
}
