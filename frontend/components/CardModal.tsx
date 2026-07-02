"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { deleteCard, fetchCardDetail, patchCard, spawnCard } from "@/lib/api";
import type { Card, CardDetail, ChecklistItem } from "@/lib/types";
import { PRIORITIES, ago, dueTone, stamp } from "@/lib/fmt";
import { useCan } from "@/lib/me";

// modal portalée sur document.body (gotcha Safari : position:fixed + ancêtre transformé)
export default function CardModal({
  cardId, tags, onClose, onOpenSession, onChanged,
}: {
  cardId: number;
  tags: string[];
  onClose: () => void;
  onOpenSession: (sid: string) => void;
  onChanged: () => void;
}) {
  const [card, setCard] = useState<CardDetail | null>(null);
  const [descEdit, setDescEdit] = useState(false);
  const [desc, setDesc] = useState("");
  const [newItem, setNewItem] = useState("");
  const [confirmDel, setConfirmDel] = useState(false);
  const [busy, setBusy] = useState(false);
  const canWrite = useCan("dev");
  const titleRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const d = await fetchCardDetail(cardId);
      setCard(d);
      setDesc(d.description);
    } catch { onClose(); }
  }, [cardId, onClose]);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const patch = async (fields: Partial<Card>) => {
    if (!card || !canWrite) return;
    setCard({ ...card, ...fields } as CardDetail); // optimiste
    await patchCard(card.id, fields).catch(() => {});
    await load();
    onChanged();
  };

  const checklist = card?.checklist ?? [];
  const doneCount = checklist.filter((i) => i.done).length;
  const setChecklist = (items: ChecklistItem[]) => patch({ checklist: items });

  const doSpawn = async () => {
    if (!card) return;
    setBusy(true);
    try {
      const r = await spawnCard(card.id);
      onChanged();
      onOpenSession(r.session_id);
      onClose();
    } finally { setBusy(false); }
  };

  if (!card) return null;
  const p = PRIORITIES[card.priority] ?? PRIORITIES[2];

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-start justify-center overflow-y-auto bg-black/60 p-4 pt-[6vh]" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="w-full max-w-2xl rounded-2xl border border-line bg-panel shadow-2xl">
        {/* header */}
        <div className="flex items-start gap-3 border-b border-line px-5 py-4">
          <span className={`mt-2 h-2.5 w-2.5 shrink-0 rounded-full ${p.dot}`} title={`priorité ${p.label}`} />
          <div className="min-w-0 flex-1">
            <input
              ref={titleRef}
              defaultValue={card.title}
              disabled={!canWrite}
              onBlur={(e) => e.target.value.trim() && e.target.value !== card.title && patch({ title: e.target.value.trim() })}
              onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
              className="w-full bg-transparent text-[17px] font-semibold text-slate-100 outline-none focus:border-b focus:border-sea/50"
            />
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-mut">
              <span>#{card.id}</span>
              <span>· {card.bucket}</span>
              <span>· créée {ago(card.created_at)}</span>
              {card.updated_at && <span>· modifiée {ago(card.updated_at)}</span>}
              {card.archived ? <span className="rounded bg-red-500/15 px-1.5 text-red-300">archivée</span> : null}
            </div>
          </div>
          <button onClick={onClose} className="rounded-md px-2 py-1 text-mut hover:bg-panel2 hover:text-slate-200">✕</button>
        </div>

        <div className="space-y-5 px-5 py-4">
          {/* propriétés */}
          <div className="flex flex-wrap items-center gap-x-5 gap-y-3 text-[12px]">
            <label className="flex items-center gap-2">
              <span className="text-mut">tag</span>
              <select value={card.tag} disabled={!canWrite} onChange={(e) => patch({ tag: e.target.value })}
                className="rounded border border-line bg-panel2 px-1.5 py-1 text-slate-200">
                {tags.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <div className="flex items-center gap-1.5">
              <span className="text-mut">priorité</span>
              {Object.entries(PRIORITIES).map(([k, v]) => (
                <button key={k} disabled={!canWrite} onClick={() => patch({ priority: Number(k) })}
                  className={`rounded-full px-2 py-0.5 ring-1 ${card.priority === Number(k) ? `${v.text} ring-current bg-panel2` : "text-mut ring-line hover:text-slate-300"}`}>
                  {v.label}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2">
              <span className="text-mut">échéance</span>
              <input type="date" value={card.due || ""} disabled={!canWrite}
                onChange={(e) => patch({ due: e.target.value })}
                className={`rounded border border-line bg-panel2 px-1.5 py-0.5 text-slate-200 ${card.due ? dueTone(card.due) : ""}`} />
              {card.due && canWrite && <button onClick={() => patch({ due: "" })} className="text-mut hover:text-slate-300">✕</button>}
            </label>
          </div>

          {/* description (markdown) */}
          <div>
            <div className="mb-1.5 flex items-center gap-2">
              <span className="text-[12px] font-medium text-slate-300">Description</span>
              {canWrite && (
                <button onClick={() => { setDescEdit(!descEdit); if (descEdit && desc !== card.description) patch({ description: desc }); }}
                  className="rounded border border-line px-2 py-0.5 text-[11px] text-mut hover:text-slate-200">
                  {descEdit ? "enregistrer" : "éditer"}
                </button>
              )}
            </div>
            {descEdit ? (
              <textarea value={desc} onChange={(e) => setDesc(e.target.value)} rows={6} autoFocus
                onBlur={() => { setDescEdit(false); if (desc !== card.description) patch({ description: desc }); }}
                className="w-full rounded-lg border border-line bg-[#0b0f16] p-3 text-[13px] text-slate-100 outline-none focus:border-sea/50" />
            ) : card.description ? (
              <div className="md rounded-lg border border-line/60 bg-panel2/40 p-3 text-[13px] text-slate-200">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{card.description}</ReactMarkdown>
              </div>
            ) : (
              <div className="rounded-lg border border-dashed border-line p-3 text-[12px] text-mut">
                pas de description — c’est elle qui seede la session au spawn
              </div>
            )}
          </div>

          {/* checklist */}
          <div>
            <div className="mb-1.5 flex items-center gap-2">
              <span className="text-[12px] font-medium text-slate-300">Checklist</span>
              {checklist.length > 0 && (
                <>
                  <span className="text-[11px] text-mut">{doneCount}/{checklist.length}</span>
                  <div className="h-1 w-24 overflow-hidden rounded bg-panel2">
                    <div className="h-full bg-emerald-500/80 transition-all" style={{ width: `${(doneCount / checklist.length) * 100}%` }} />
                  </div>
                </>
              )}
            </div>
            <div className="space-y-1">
              {checklist.map((it, i) => (
                <div key={i} className="group flex items-center gap-2 rounded px-1 py-0.5 hover:bg-panel2/60">
                  <input type="checkbox" checked={it.done} disabled={!canWrite}
                    onChange={() => setChecklist(checklist.map((x, j) => (j === i ? { ...x, done: !x.done } : x)))}
                    className="accent-emerald-500" />
                  <span className={`flex-1 text-[12.5px] ${it.done ? "text-mut line-through" : "text-slate-200"}`}>{it.text}</span>
                  {canWrite && (
                    <button onClick={() => setChecklist(checklist.filter((_, j) => j !== i))}
                      className="invisible text-mut hover:text-red-400 group-hover:visible">✕</button>
                  )}
                </div>
              ))}
              {canWrite && (
                <input value={newItem} onChange={(e) => setNewItem(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && newItem.trim()) {
                      setChecklist([...checklist, { text: newItem.trim(), done: false }]);
                      setNewItem("");
                    }
                  }}
                  placeholder="+ ajouter un point (Entrée)"
                  className="w-full rounded border border-line/60 bg-transparent px-2 py-1 text-[12px] text-slate-200 outline-none placeholder:text-mut/60 focus:border-sea/50" />
              )}
            </div>
          </div>

          {/* activité */}
          <div>
            <div className="mb-1.5 text-[12px] font-medium text-slate-300">Activité</div>
            <div className="max-h-44 space-y-1 overflow-y-auto pr-1">
              {card.events.map((ev, i) => (
                <div key={i} className="flex items-baseline gap-2 text-[11.5px]">
                  <span className="shrink-0 tabular-nums text-mut/70">{stamp(ev.ts)}</span>
                  <span className="shrink-0 rounded bg-panel2 px-1.5 text-[10.5px] text-slate-300">{ev.action}</span>
                  <span className="truncate text-mut">{ev.detail}</span>
                  {ev.user && <span className="ml-auto shrink-0 text-[10px] text-mut/60">{ev.user.split("@")[0]}</span>}
                </div>
              ))}
              {!card.events.length && <div className="text-[11.5px] text-mut">aucun événement</div>}
            </div>
          </div>
        </div>

        {/* footer actions */}
        {canWrite && (
          <div className="flex items-center gap-2 border-t border-line px-5 py-3">
            {card.session_id ? (
              <button onClick={() => { onOpenSession(card.session_id!); onClose(); }}
                className="rounded-md bg-sea/20 px-3 py-1.5 text-[12.5px] text-sea ring-1 ring-sea/30 hover:bg-sea/30">
                ouvrir la session {card.window ? `(${card.window.split(":").pop()})` : ""}
              </button>
            ) : (
              <button onClick={doSpawn} disabled={busy}
                className="rounded-md bg-emerald-600/20 px-3 py-1.5 text-[12.5px] text-emerald-300 ring-1 ring-emerald-600/30 hover:bg-emerald-600/30 disabled:opacity-40">
                {busy ? "spawn…" : "▶ spawn une session"}
              </button>
            )}
            <div className="ml-auto flex items-center gap-2">
              <button onClick={() => patch({ archived: card.archived ? 0 : 1 })}
                className="rounded-md px-3 py-1.5 text-[12px] text-mut ring-1 ring-line hover:text-slate-200">
                {card.archived ? "restaurer" : "archiver"}
              </button>
              <button
                onClick={() => {
                  if (!confirmDel) { setConfirmDel(true); setTimeout(() => setConfirmDel(false), 3000); return; }
                  deleteCard(card.id).then(() => { onChanged(); onClose(); });
                }}
                className={`rounded-md px-3 py-1.5 text-[12px] ring-1 ${confirmDel ? "bg-red-600/20 text-red-300 ring-red-600/40" : "text-mut ring-line hover:text-red-300"}`}>
                {confirmDel ? "confirmer ?" : "supprimer"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body
  );
}
