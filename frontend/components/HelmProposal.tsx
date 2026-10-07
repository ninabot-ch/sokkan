"use client";
import { useState } from "react";
import { helmCreateProject, type ProjectProposal } from "@/lib/helm";

// 3.3 Helm — Nina's proposal after the interview: the project card and its breakdown into
// cards. Everything is editable here; nothing exists until the person validates.

type Child = { title: string; description?: string; assignee?: string };

export default function HelmProposal({ proposal }: { proposal: ProjectProposal }) {
  const [p, setP] = useState<ProjectProposal>({ ...proposal, children: (proposal.children || []).map((c) => ({ ...c })) });
  const [state, setState] = useState<{ id?: number; err?: string; busy?: boolean }>({});
  const kids = (p.children || []) as Child[];
  const setKid = (i: number, f: Partial<Child>) => setP({ ...p, children: kids.map((c, j) => (j === i ? { ...c, ...f } : c)) });
  const inp = "w-full rounded border border-line bg-panel px-1.5 py-0.5 text-[11.5px] text-slate-200 placeholder:text-mut/50";

  const create = async () => {
    setState({ busy: true });
    try {
      const out = await helmCreateProject({ ...p, children: kids.filter((c) => c.title.trim()) });
      setState({ id: out.card.id });
    } catch (e) { setState({ err: String((e as Error).message) }); }
  };

  if (state.id) {
    return (
      <div className="mt-2 rounded-lg border border-line bg-panel2/70 p-2.5 text-[11.5px]">
        <a href={`/?tab=helm&card=${state.id}`} className="text-sea hover:underline">✓ Project card #{state.id} created with {kids.length} card(s) under it — open it in Helm →</a>
      </div>
    );
  }
  return (
    <div className="crew-c-idle crew-card mt-2 space-y-1.5 whitespace-normal rounded-lg border border-line bg-panel2/70 p-2.5 text-[11.5px]">
      <div className="flex items-center gap-2">
        <span className="crew-fg text-[10px] font-semibold uppercase">● proposed project — edit, then validate</span>
      </div>
      <input aria-label="project title" value={p.title} onChange={(e) => setP({ ...p, title: e.target.value })} className={`${inp} text-[13px] font-semibold`} />
      <label className="block"><span className="text-mut">goal</span>
        <textarea rows={2} value={p.intent || ""} onChange={(e) => setP({ ...p, intent: e.target.value })} className={inp} /></label>
      <label className="block"><span className="text-mut">scope</span>
        <input value={p.scope || ""} onChange={(e) => setP({ ...p, scope: e.target.value })} className={inp} /></label>
      <label className="block"><span className="text-mut">constraints</span>
        <input value={p.constraints || ""} onChange={(e) => setP({ ...p, constraints: e.target.value })} className={inp} /></label>
      <div className="flex gap-2">
        <label className="block flex-1"><span className="text-mut">deadline</span>
          <input type="date" value={p.deadline || ""} onChange={(e) => setP({ ...p, deadline: e.target.value })} className={inp} /></label>
        <label className="block flex-1"><span className="text-mut">team</span>
          <input value={(p.team || []).join(", ")} onChange={(e) => setP({ ...p, team: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} className={inp} placeholder="emails" /></label>
      </div>
      <div>
        <div className="text-mut">cards under it ({kids.length})</div>
        <ul className="mt-1 space-y-1">
          {kids.map((c, i) => (
            <li key={i} className="flex gap-1.5">
              <input aria-label={`card ${i + 1} title`} value={c.title} onChange={(e) => setKid(i, { title: e.target.value })} className={inp} />
              <input aria-label={`card ${i + 1} owner`} value={c.assignee || ""} onChange={(e) => setKid(i, { assignee: e.target.value })} placeholder="owner" className={`${inp} w-36`} />
              <button aria-label="remove this card" onClick={() => setP({ ...p, children: kids.filter((_, j) => j !== i) })} className="px-1 text-mut hover:text-red-300">✕</button>
            </li>
          ))}
        </ul>
        <button onClick={() => setP({ ...p, children: [...kids, { title: "" }] })} className="mt-1 text-sea hover:underline">+ add a card</button>
      </div>
      <div className="flex items-center gap-2 pt-1">
        <button onClick={create} disabled={state.busy || !p.title.trim()} className="rounded-md bg-brass/90 px-2.5 py-1 font-semibold text-ink hover:bg-brass disabled:opacity-50">Create the project</button>
        {state.err && <span className="text-red-400">{state.err}</span>}
      </div>
    </div>
  );
}
