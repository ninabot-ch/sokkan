"use client";
import { useState } from "react";
import type { Card, CardDetail } from "@/lib/types";

// 3.3 Helm — in the card dialog: where the card sits (breadcrumb, parent), what flows
// DOWN to the sessions of the cards below it (intent, constraints, decisions — each
// change rewrites the project memory note `helm-card-<id>`), and the cards under it.

export default function CardHierarchy({ card, canWrite, patch, onOpenCard }: {
  card: CardDetail;
  canWrite: boolean;
  patch: (f: Partial<Card>) => Promise<void>;
  onOpenCard?: (id: number) => void;
}) {
  const [decision, setDecision] = useState("");
  const [parent, setParent] = useState("");
  const decisions = card.decisions || [];
  const kids = card.children || [];
  const crumbs = card.breadcrumb || [];
  const steering = card.kind === "project" || kids.length > 0;
  const field = "w-full rounded border border-line bg-panel2 px-2 py-1 text-[12px] text-slate-200 placeholder:text-mut/50 disabled:opacity-60";

  return (
    <div className="rounded-lg border border-line bg-panel2/30 p-3" aria-label="Helm: hierarchy and context">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[12px]">
        <span className="font-medium text-slate-300">Helm</span>
        <nav aria-label="breadcrumb" className="flex flex-wrap items-center gap-1 text-[11.5px] text-mut">
          {crumbs.length === 0 ? <span>top level</span> : crumbs.map((c) => (
            <span key={c.id} className="flex items-center gap-1">
              <button onClick={() => onOpenCard?.(c.id)} className="text-sea hover:underline">#{c.id} {c.title}</button>
              <span aria-hidden>›</span>
            </span>
          ))}
          {crumbs.length > 0 && <span className="text-slate-300">#{card.id}</span>}
        </nav>
        <label className="ml-auto flex items-center gap-1.5 text-[11.5px]">
          <span className="text-mut">kind</span>
          <select value={card.kind || "task"} disabled={!canWrite} onChange={(e) => patch({ kind: e.target.value as Card["kind"] })}
            className="rounded border border-line bg-panel2 px-1.5 py-0.5 text-slate-200">
            <option value="task">task</option>
            <option value="project">project</option>
            <option value="reframe">reframe</option>
          </select>
        </label>
      </div>
      {canWrite && (
        <div className="mb-2 flex items-center gap-1.5 text-[11.5px]">
          <span className="text-mut">move under card #</span>
          <input value={parent} onChange={(e) => setParent(e.target.value.replace(/\D/g, ""))} placeholder="id"
            className="w-16 rounded border border-line bg-panel2 px-1.5 py-0.5 text-slate-200" />
          <button disabled={!parent} onClick={() => patch({ parent_id: Number(parent) }).then(() => setParent(""))}
            className="rounded border border-line px-2 py-0.5 text-mut hover:text-slate-200 disabled:opacity-40">move</button>
          {card.parent_id ? (
            <button onClick={() => patch({ parent_id: 0 })} className="rounded border border-line px-2 py-0.5 text-mut hover:text-slate-200">to top level</button>
          ) : null}
        </div>
      )}
      {(steering || card.intent || card.constraints || decisions.length > 0) && (
        <div className="space-y-2">
          <div className="text-[11px] text-mut">Flows down to every session spawned from a card below (and to the project memory as <code>card:{card.id}</code>).</div>
          <label className="block text-[11.5px]">
            <span className="text-mut">intent — the goal the cards below serve</span>
            <textarea key={`i-${card.intent}`} defaultValue={card.intent || ""} disabled={!canWrite} rows={2}
              onBlur={(e) => e.target.value.trim() !== (card.intent || "") && patch({ intent: e.target.value.trim() })}
              className={field} placeholder="e.g. listeners on the web player jump to a chapter" />
          </label>
          <label className="block text-[11.5px]">
            <span className="text-mut">constraints</span>
            <textarea key={`c-${card.constraints}`} defaultValue={card.constraints || ""} disabled={!canWrite} rows={2}
              onBlur={(e) => e.target.value.trim() !== (card.constraints || "") && patch({ constraints: e.target.value.trim() })}
              className={field} placeholder="e.g. WCAG AA; no new backend service; ship by 30.11" />
          </label>
          <div className="text-[11.5px]">
            <span className="text-mut">decisions</span>
            <ul className="mt-1 space-y-1">
              {decisions.map((d, i) => (
                <li key={i} className="flex items-start gap-2 rounded bg-panel/60 px-2 py-1 text-slate-200">
                  <span className="flex-1">{d}</span>
                  {canWrite && <button aria-label="withdraw this decision" onClick={() => patch({ decisions: decisions.filter((_, j) => j !== i) })} className="text-mut hover:text-red-300">✕</button>}
                </li>
              ))}
            </ul>
            {canWrite && (
              <div className="mt-1 flex gap-1.5">
                <input value={decision} onChange={(e) => setDecision(e.target.value)} placeholder="record a decision — e.g. no Kafka: metadata stays on the API"
                  onKeyDown={(e) => { if (e.key === "Enter" && decision.trim()) { patch({ decisions: [...decisions, decision.trim()] }); setDecision(""); } }}
                  className={field} />
                <button disabled={!decision.trim()} onClick={() => { patch({ decisions: [...decisions, decision.trim()] }); setDecision(""); }}
                  className="rounded border border-line px-2 text-mut hover:text-slate-200 disabled:opacity-40">add</button>
              </div>
            )}
          </div>
        </div>
      )}
      {kids.length > 0 && (
        <div className="mt-2 text-[11.5px]">
          <span className="text-mut">cards under it ({kids.length})</span>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {kids.map((k) => (
              <li key={k.id}>
                <button onClick={() => onOpenCard?.(k.id)} className="rounded border border-line bg-panel/60 px-1.5 py-0.5 text-slate-300 hover:border-sea/50">
                  #{k.id} {k.title} <span className="text-mut">· {k.bucket}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
