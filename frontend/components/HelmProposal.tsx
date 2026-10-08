"use client";
import { useEffect, useMemo, useState } from "react";
import { helmCreateProject, helmTargets, type HelmTarget, type ProjectProposal } from "@/lib/helm";
import { currentProject } from "@/lib/project";

// 3.3 Helm — Nina's proposal after the interview: the project card and its breakdown into
// cards. Everything is editable here; nothing exists until the person validates.
// 3.4 (manager journey of 08.10): the proposal says WHERE it will be created (a project the
// person steers first — otherwise it never shows in their Helm), owners are chosen among the
// project's people (Nina invented addresses, silently dropped at creation), and the link
// after creation opens the right project.

type Child = { title: string; description?: string; assignee?: string };

function pickTarget(targets: HelmTarget[], wanted?: string): string {
  if (wanted && targets.some((t) => t.slug === wanted)) return wanted;
  const here = targets.find((t) => t.slug === currentProject());
  if (here?.steers) return here.slug;
  return (targets.find((t) => t.steers) || here || targets[0])?.slug || "";
}

export default function HelmProposal({ proposal }: { proposal: ProjectProposal }) {
  const [p, setP] = useState<ProjectProposal>({ ...proposal, children: (proposal.children || []).map((c) => ({ ...c })) });
  const [targets, setTargets] = useState<HelmTarget[] | null>(null);
  const [state, setState] = useState<{ id?: number; project?: string; steers?: boolean; dropped?: number; name?: string; role?: string; count?: number; err?: string; busy?: boolean }>({});
  // a proposal already created (the conversation is replayed from history) shows its result,
  // never a second « Create the project » button
  const key = useMemo(() => {
    const t = JSON.stringify(proposal);
    let h = 0;
    for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) | 0;
    return `sokkan_helm_created:${h}`;
  }, [proposal]);
  useEffect(() => {
    try {
      const v = localStorage.getItem(key);
      if (v) setState(JSON.parse(v));
    } catch { /* storage unavailable: the button stays */ }
  }, [key]);
  useEffect(() => {
    helmTargets().then((t) => { setTargets(t); setP((x) => ({ ...x, project: pickTarget(t, proposal.project) })); }).catch(() => setTargets([]));
  }, [proposal.project]);
  const target = useMemo(() => targets?.find((t) => t.slug === p.project), [targets, p.project]);
  const people = target?.people || [];
  const member = (e?: string) => !!e && people.some((x) => x.email === e.toLowerCase());
  const kids = (p.children || []) as Child[];
  const setKid = (i: number, f: Partial<Child>) => setP({ ...p, children: kids.map((c, j) => (j === i ? { ...c, ...f } : c)) });
  const inp = "w-full rounded border border-line bg-panel px-1.5 py-1 text-[12px] text-slate-200 placeholder:text-mut/50";
  // the owner select of a card row: fixed width — `w-full` from `inp` took the whole row and
  // squeezed the card titles to nothing (journey 08.10, after)
  const sel = "w-40 shrink-0 rounded border border-line bg-panel px-1.5 py-1 text-[12px] text-slate-200";
  const unknownOwners = kids.filter((c) => c.assignee && !member(c.assignee));

  const create = async () => {
    setState({ busy: true });
    try {
      const out = await helmCreateProject({
        ...p, decisions: (p.decisions || []).map((d) => d.trim()).filter(Boolean), team: (p.team || []).filter(member),
        children: kids.filter((c) => c.title.trim()).map((c) => ({ ...c, assignee: member(c.assignee) ? c.assignee : "" })),
      });
      const done = { id: out.card.id, project: out.project, steers: !!target?.steers, dropped: out.dropped_owners?.length || 0, name: target?.name, role: target?.role, count: out.children.length };
      setState(done);
      try { localStorage.setItem(key, JSON.stringify(done)); } catch { /* private mode */ }
    } catch (e) { setState({ err: String((e as Error).message) }); }
  };

  if (state.id) {
    const where = state.project || p.project || "";
    const href = state.steers
      ? `/?project=${where}&plane=control&tab=helm&card=${state.id}`
      : `/?project=${where}&plane=control&tab=board`;
    return (
      <div className="mt-2 rounded-lg border border-line bg-panel2/70 p-2.5 text-[12px]">
        <a href={href} className="text-sea hover:underline">
          ✓ Project card #{state.id} created in « {state.name || target?.name || where} » with {state.count ?? kids.filter((c) => c.title.trim()).length} card(s) under it —
          {state.steers ? " open it in Helm →" : " open its board →"}
        </a>
        {!state.steers && <div className="mt-1 text-mut">You are {state.role || target?.role} there: only its maintainers follow it in Helm.</div>}
        {!!state.dropped && <div className="mt-1 text-amber-300">{state.dropped} owner(s) were not people of this project: those cards have no owner yet.</div>}
      </div>
    );
  }
  return (
    <div className="crew-c-idle crew-card mt-2 space-y-2 whitespace-normal rounded-lg border border-line bg-panel2/70 p-2.5 text-[12px]">
      <div className="flex items-center gap-2">
        <span className="crew-fg text-[10.5px] font-semibold uppercase">● proposed project — edit, then validate</span>
      </div>
      <label className="block"><span className="text-mut">create it in</span>
        {targets === null ? <div className="text-mut">…</div> : targets.length === 0 ? (
          <div className="text-red-300">You have no project where you can create cards (developer role needed).</div>
        ) : (
          <select aria-label="project where the card is created" value={p.project || ""} onChange={(e) => setP({ ...p, project: e.target.value })} className={inp}>
            {targets.map((t) => <option key={t.slug} value={t.slug}>{t.name} — {t.steers ? "you steer it" : `you are ${t.role}`}</option>)}
          </select>
        )}
        {target && !target.steers && <span className="mt-0.5 block text-amber-300">You can create it here, but only the project&apos;s maintainers follow it in Helm.</span>}
      </label>
      <label className="block"><span className="text-mut">title</span>
        <input aria-label="project title" value={p.title} onChange={(e) => setP({ ...p, title: e.target.value })} className={`${inp} text-[13px] font-semibold`} /></label>
      <label className="block"><span className="text-mut">goal</span>
        <textarea rows={2} value={p.intent || ""} onChange={(e) => setP({ ...p, intent: e.target.value })} className={inp} /></label>
      <label className="block"><span className="text-mut">scope</span>
        <textarea rows={2} value={p.scope || ""} onChange={(e) => setP({ ...p, scope: e.target.value })} className={inp} /></label>
      <label className="block"><span className="text-mut">constraints</span>
        <textarea rows={2} value={p.constraints || ""} onChange={(e) => setP({ ...p, constraints: e.target.value })} className={inp} /></label>
      <label className="block"><span className="text-mut">decisions already taken — one per line (Helm flags the work that contradicts them)</span>
        <textarea rows={2} value={(p.decisions || []).join("\n")} onChange={(e) => setP({ ...p, decisions: e.target.value.split("\n") })} className={inp} /></label>
      <label className="block w-40"><span className="text-mut">deadline</span>
        <input type="date" value={p.deadline || ""} onChange={(e) => setP({ ...p, deadline: e.target.value })} className={inp} /></label>
      <div>
        <div className="text-mut">cards under it ({kids.length}) — title and owner</div>
        <ul className="mt-1 space-y-1.5">
          {kids.map((c, i) => {
            const bad = !!c.assignee && !member(c.assignee);
            return (
              <li key={i} className="flex items-start gap-1.5">
                <textarea rows={2} aria-label={`card ${i + 1} title`} value={c.title} onChange={(e) => setKid(i, { title: e.target.value })}
                  className={`${inp} min-w-0 flex-1 resize-y`} />
                <select aria-label={`card ${i + 1} owner`} value={bad ? "" : (c.assignee || "").toLowerCase()} onChange={(e) => setKid(i, { assignee: e.target.value })}
                  className={`${sel} ${bad ? "border-amber-400/60" : ""}`} title={bad ? `${c.assignee} is not a person of this project` : undefined}>
                  <option value="">{bad ? "⚠ choose…" : "no owner"}</option>
                  {people.map((x) => <option key={x.email} value={x.email}>{x.name}</option>)}
                </select>
                <button aria-label={`remove card ${i + 1}`} onClick={() => setP({ ...p, children: kids.filter((_, j) => j !== i) })} className="px-1 py-1 text-mut hover:text-red-300">✕</button>
              </li>
            );
          })}
        </ul>
        <button onClick={() => setP({ ...p, children: [...kids, { title: "" }] })} className="mt-1 text-sea hover:underline">+ add a card</button>
        {unknownOwners.length > 0 && (
          <div className="mt-1 text-amber-300">⚠ {unknownOwners.map((c) => c.assignee).join(", ")} {unknownOwners.length > 1 ? "are" : "is"} not in this project: choose an owner or leave the card without one.</div>
        )}
      </div>
      <div className="flex items-center gap-2 pt-1">
        <button onClick={create} disabled={state.busy || !p.title.trim() || !p.project} className="rounded-md bg-brass/90 px-2.5 py-1 font-semibold text-ink hover:bg-brass disabled:opacity-50">Create the project</button>
        {state.err && <span className="text-red-400">{state.err}</span>}
      </div>
    </div>
  );
}
