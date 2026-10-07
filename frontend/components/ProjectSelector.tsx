"use client";
import { useEffect, useState } from "react";
import { fetchProjects } from "@/lib/api";
import { currentProject, switchProject } from "@/lib/project";
import type { ProjectsList } from "@/lib/types";

const ROLE_COLOR: Record<string, string> = {
  admin: "text-sea", maintainer: "text-brass", dev: "text-emerald-400", viewer: "text-mut",
};

/** 3.2 — the project the cockpit works in. Hidden while the instance has one project
 *  (a 3.1 install sees no change). Every tab shows the selected project's data only;
 *  `shared` is read with every project, it is not a place to work in. */
export default function ProjectSelector() {
  const [list, setList] = useState<ProjectsList | null>(null);
  const cur = currentProject();
  useEffect(() => { fetchProjects().then(setList).catch(() => {}); }, []);
  const work = (list?.projects || []).filter((p) => !p.shared);
  useEffect(() => {
    // the stored / linked project is not (or no longer) readable: go to one that is
    if (list && work.length && !work.some((p) => p.slug === cur)) switchProject(work[0].slug);
  }, [list]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!list || !list.multi || work.length === 0) return null;
  const mine = work.find((p) => p.slug === cur);
  return (
    <label className="ml-1 flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-panel2 px-2 py-1 text-[12px] text-slate-200"
      title="The project you work in: sessions, board, agents and memory of this project only (+ shared, read-only)">
      <span className="text-mut">project</span>
      <select aria-label="Project" value={cur} onChange={(e) => switchProject(e.target.value)}
        className="max-w-[160px] bg-transparent font-medium text-slate-100 outline-none">
        {work.map((p) => <option key={p.slug} value={p.slug} className="bg-panel">{p.name}</option>)}
      </select>
      {mine && <span className={`text-[10.5px] ${ROLE_COLOR[mine.role] || "text-mut"}`}>{mine.role}</span>}
    </label>
  );
}
