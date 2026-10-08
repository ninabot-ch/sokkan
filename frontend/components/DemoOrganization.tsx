"use client";
// 3.2.2 — Setup › Organization on the public « Captains » demo: the visitor (a viewer) sees
// the fictional members, projects and teams, read-only. The API serves @example.com people
// only (backend/demo_captains.py); nothing here can change anything.
import { useEffect, useState } from "react";

interface DemoOrg {
  read_only: boolean;
  note: string;
  members: { email: string; name: string; role: string }[];
  teams: { id: string; name: string; members: string[] }[];
  projects: { slug: string; name: string; description: string; access_source: string;
    grants: { kind: "user" | "team"; principal: string; role: string }[] }[];
}

let cache: Promise<DemoOrg> | null = null;
function load(): Promise<DemoOrg> {
  cache ??= fetch("/api/demo/organization", { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }).catch((e) => { cache = null; throw e; });
  return cache;
}

function useDemoOrg() {
  const [d, setD] = useState<DemoOrg | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => { load().then(setD).catch((e) => setErr(String(e.message || e))); }, []);
  return { d, err };
}

export function ReadOnlyNote({ children }: { children?: React.ReactNode }) {
  return (
    <div className="mb-3 flex items-start gap-2 rounded-lg border border-line bg-panel2/40 px-3 py-2 text-[11.5px] text-mut">
      <span aria-hidden>👁</span>
      <span><b className="font-medium text-slate-200">Read-only demo.</b> {children}</span>
    </div>
  );
}

const ROLE_TONE: Record<string, string> = {
  admin: "text-brass", maintainer: "text-brass", dev: "text-sea", viewer: "text-mut",
};

export function DemoMembers() {
  const { d, err } = useDemoOrg();
  if (!d) return <div className="text-[12px] text-mut">{err ? `Could not load the members (${err}).` : "Loading…"}</div>;
  return (
    <div>
      <ReadOnlyNote>{d.note} An admin adds, changes or removes members here.</ReadOnlyNote>
      <ul className="divide-y divide-line/60 rounded-lg border border-line" aria-label="Members">
        {d.members.map((m) => (
          <li key={m.email} className="flex items-center gap-3 px-3 py-2 text-[12.5px]">
            <span aria-hidden className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-panel2 text-[11px] font-semibold text-slate-200">
              {m.name.split(" ").map((x) => x[0]).join("").slice(0, 2)}</span>
            <span className="min-w-0 flex-1"><span className="block truncate text-slate-100">{m.name}</span>
              <span className="block truncate text-[11px] text-mut">{m.email}</span></span>
            <span className={`text-[11.5px] ${ROLE_TONE[m.role] || "text-mut"}`}>{m.role}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function DemoProjects() {
  const { d, err } = useDemoOrg();
  if (!d) return <div className="text-[12px] text-mut">{err ? `Could not load the projects (${err}).` : "Loading…"}</div>;
  return (
    <div className="space-y-3">
      <ReadOnlyNote>Each project has its own sessions, board, agents, memory and secrets; a team gets a role in it.</ReadOnlyNote>
      {d.projects.map((p) => (
        <section key={p.slug} className="rounded-lg border border-line bg-panel2/30 p-3" aria-label={`Project ${p.name}`}>
          <div className="flex flex-wrap items-baseline gap-2">
            <h4 className="text-[13px] font-semibold text-slate-100">{p.name}</h4>
            <code className="text-[11px] text-mut">{p.slug}</code>
          </div>
          {p.description && <p className="mt-0.5 text-[12px] text-slate-300">{p.description}</p>}
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {p.grants.length === 0 && <li className="text-[11.5px] text-mut">every member of the instance (default project)</li>}
            {p.grants.map((g) => (
              <li key={g.kind + g.principal} className="rounded-full border border-line px-2 py-0.5 text-[11.5px] text-slate-300">
                {g.kind === "team" ? "👥 " : ""}{g.principal} · <span className={ROLE_TONE[g.role] || "text-mut"}>{g.role}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
      <section className="rounded-lg border border-line bg-panel2/30 p-3" aria-label="Teams">
        <h4 className="mb-1.5 text-[12.5px] font-medium text-slate-100">Teams</h4>
        <ul className="space-y-1 text-[12px]">
          {d.teams.map((t) => (
            <li key={t.id}><span className="text-slate-200">{t.name}</span> <span className="text-mut">— {t.members.join(", ")}</span></li>
          ))}
        </ul>
      </section>
    </div>
  );
}
