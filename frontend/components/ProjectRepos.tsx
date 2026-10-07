"use client";
import { useEffect, useState } from "react";
import { adminAddRepo, adminRemoveRepo, adminRepos, type ForgeRepo } from "@/lib/forge";

const inp = "rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50";

/** 3.2 lot 5 — the GitLab repositories of a `forge` project: a person's role in the
 *  project is their LOWEST GitLab level over these repositories. */
export default function ProjectRepos({ slug }: { slug: string }) {
  const [repos, setRepos] = useState<ForgeRepo[]>([]);
  const [path, setPath] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => { adminRepos(slug).then((d) => setRepos(d.repos)).catch((e) => setErr(String(e.message || e))); }, [slug]);
  const run = (p: Promise<{ repos: ForgeRepo[] }>) => p.then((d) => { setRepos(d.repos); setErr(""); }).catch((e) => setErr(String(e.message || e)));
  return (
    <div className="mt-2 rounded border border-line/60 p-2">
      <div className="text-[11px] text-mut">GitLab repositories — role = the lowest level over them (Reporter → viewer, Developer → dev, Maintainer → maintainer, Owner → admin)</div>
      <ul className="mt-1 space-y-0.5">
        {repos.map((r) => (
          <li key={`${r.base_url}/${r.repo_path}`} className="flex items-center gap-2 text-[12px]">
            <code className="text-slate-200">{r.repo_path}</code><span className="text-[10.5px] text-mut">{r.base_url}</span>
            <button className="ml-auto text-[11px] text-mut hover:text-red-300" onClick={() => run(adminRemoveRepo(slug, r))}>remove</button>
          </li>
        ))}
        {repos.length === 0 && <li className="text-[11.5px] text-amber-300">No repository: nobody has access yet.</li>}
      </ul>
      <div className="mt-1.5 flex gap-1.5">
        <input aria-label="Repository path" placeholder="group/subgroup/repository" value={path} onChange={(e) => setPath(e.target.value)} className={`${inp} flex-1`} />
        <button disabled={!path} className="rounded bg-sea/80 px-2 py-0.5 text-[11px] text-white hover:bg-sea disabled:opacity-40"
          onClick={() => run(adminAddRepo(slug, path).then((d) => { setPath(""); return d; }))}>add</button>
      </div>
      {err && <div className="mt-1 text-[11px] text-red-300">{err}</div>}
    </div>
  );
}
