"use client";
import { useEffect, useMemo, useState } from "react";
import { addCard, fetchBoard, fetchTags, patchCard, spawnCard } from "@/lib/api";
import type { BoardData, Card } from "@/lib/types";
import { PRIORITIES, ago, dueTone } from "@/lib/fmt";
import { useCan } from "@/lib/me";
import CardModal from "./CardModal";

const BUCKET_TONES: Record<string, string> = {
  Backlog: "text-slate-300", Doing: "text-sky-300", Review: "text-amber-300", Done: "text-emerald-300",
};

type DropAt = { bucket: string; index: number } | null;

export default function Board({ onOpenSession }: { onOpenSession: (sid: string) => void }) {
  const [board, setBoard] = useState<BoardData | null>(null);
  const [tags, setTags] = useState<string[]>([]);
  const [prompt, setPrompt] = useState("");
  const [tag, setTag] = useState("backend");
  const [prio, setPrio] = useState(2);
  const [busy, setBusy] = useState<number | null>(null);
  const canWrite = useCan("dev");
  const [drag, setDrag] = useState<number | null>(null);
  const [dropAt, setDropAt] = useState<DropAt>(null);
  const [openCard, setOpenCard] = useState<number | null>(null);
  // filtres
  const [q, setQ] = useState("");
  const [fTag, setFTag] = useState<string>("");
  const [showArchived, setShowArchived] = useState(false);

  const reload = async () => { try { setBoard(await fetchBoard(showArchived)); } catch { /* keep */ } };
  useEffect(() => {
    reload();
    fetchTags().then(setTags).catch(() => {});
    const iv = setInterval(reload, 5000);
    return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showArchived]);

  const buckets = board?.buckets ?? ["Backlog", "Doing", "Review", "Done"];

  const visible = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const out: Record<string, Card[]> = {};
    for (const b of buckets) {
      out[b] = (board?.cards[b] ?? []).filter(
        (c) =>
          (!fTag || c.tag === fTag) &&
          (!ql || c.title.toLowerCase().includes(ql) || c.description.toLowerCase().includes(ql))
      );
    }
    return out;
  }, [board, buckets, q, fTag]);

  const create = async () => {
    if (!prompt.trim()) return;
    await addCard(prompt.trim(), tag, "", "Backlog", prio);
    setPrompt("");
    reload();
  };

  const doSpawn = async (c: Card) => {
    setBusy(c.id);
    try {
      const r = await spawnCard(c.id);
      onOpenSession(r.session_id);
      reload();
    } finally { setBusy(null); }
  };

  // dépose : insère à dropAt.index dans la colonne (sort = milieu des voisins)
  const drop = async () => {
    const id = drag;
    const at = dropAt;
    setDrag(null);
    setDropAt(null);
    if (id == null || !at || !board) return;
    const all = Object.values(board.cards).flat();
    const moved = all.find((c) => c.id === id);
    if (!moved) return;
    const col = (visible[at.bucket] ?? []).filter((c) => c.id !== id);
    const i = Math.min(at.index, col.length);
    const prev = col[i - 1];
    const next = col[i];
    const sort =
      prev && next ? (prev.sort + next.sort) / 2
      : prev ? prev.sort + 1
      : next ? next.sort - 1
      : Date.now() / 1000;
    // maj optimiste
    setBoard((bd) => {
      if (!bd) return bd;
      const cards = Object.fromEntries(
        Object.entries(bd.cards).map(([k, v]) => [k, v.filter((c) => c.id !== id)])
      ) as typeof bd.cards;
      const updated = { ...moved, bucket: at.bucket, sort };
      cards[at.bucket] = [...(cards[at.bucket] || []), updated].sort((a, b) => a.sort - b.sort || a.id - b.id);
      return { ...bd, cards };
    });
    await patchCard(id, { bucket: at.bucket, sort });
    reload();
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* barre : créer + filtres */}
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel/60 p-2">
        {canWrite && (
          <>
            <select value={tag} onChange={(e) => setTag(e.target.value)}
              className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
              {tags.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select value={prio} onChange={(e) => setPrio(Number(e.target.value))}
              className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
              {Object.entries(PRIORITIES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
            <input value={prompt} onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && create()}
              placeholder="describe the task (prompt) — e.g. 'fix the jobup promo filter on the inbox side'"
              className="min-w-64 flex-1 rounded border border-line bg-[#0b0f16] px-2.5 py-1.5 text-[12.5px] text-slate-100 outline-none focus:border-sea/50" />
            <button onClick={create} className="rounded bg-sea/80 px-3 py-1.5 text-[12.5px] font-medium text-white hover:bg-sea">+ card</button>
            <span className="mx-1 h-5 w-px bg-line" />
          </>
        )}
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="filter…"
          className="w-36 rounded border border-line bg-panel2 px-2 py-1 text-[12px] text-slate-200 outline-none focus:border-sea/50" />
        <select value={fTag} onChange={(e) => setFTag(e.target.value)}
          className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
          <option value="">all tags</option>
          {tags.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <label className="flex cursor-pointer items-center gap-1.5 text-[11.5px] text-mut">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} className="accent-slate-500" />
          archived
        </label>
      </div>

      <div className="flex min-h-0 flex-1 gap-2.5 overflow-x-auto p-2.5">
        {buckets.map((b) => {
          const cards = visible[b] ?? [];
          return (
            <div
              key={b}
              className={`flex w-80 shrink-0 flex-col rounded-xl border bg-panel ${dropAt?.bucket === b ? "border-sea/60 ring-1 ring-sea/30" : "border-line"}`}
              onDragOver={(e) => { e.preventDefault(); if (dropAt?.bucket !== b) setDropAt({ bucket: b, index: cards.length }); }}
              onDrop={drop}
            >
              <div className="flex items-center gap-2 border-b border-line px-3 py-2">
                <span className={`text-[12.5px] font-semibold ${BUCKET_TONES[b] || "text-slate-200"}`}>{b}</span>
                <span className="rounded-full bg-panel2 px-1.5 text-[10.5px] text-mut">{cards.length}</span>
              </div>
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
                {cards.map((c, i) => (
                  <div key={c.id}>
                    {dropAt?.bucket === b && dropAt.index === i && drag !== c.id && (
                      <div className="mb-2 h-0.5 rounded bg-sea/70" />
                    )}
                    <CardTile
                      c={c} canWrite={canWrite} busy={busy === c.id}
                      dragging={drag === c.id}
                      onDragStart={() => setDrag(c.id)}
                      onDragEnd={() => { setDrag(null); setDropAt(null); }}
                      onDragOver={(e) => {
                        e.preventDefault(); e.stopPropagation();
                        const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
                        const idx = e.clientY < r.top + r.height / 2 ? i : i + 1;
                        if (dropAt?.bucket !== b || dropAt.index !== idx) setDropAt({ bucket: b, index: idx });
                      }}
                      onOpen={() => setOpenCard(c.id)}
                      onOpenSession={onOpenSession}
                      onSpawn={() => doSpawn(c)}
                    />
                  </div>
                ))}
                {dropAt?.bucket === b && dropAt.index >= cards.length && drag != null && (
                  <div className="h-0.5 rounded bg-sea/70" />
                )}
                {!cards.length && <div className="px-2 py-6 text-center text-[11.5px] text-mut/60">—</div>}
              </div>
            </div>
          );
        })}
      </div>

      {openCard != null && (
        <CardModal
          cardId={openCard} tags={tags}
          onClose={() => setOpenCard(null)}
          onOpenSession={onOpenSession}
          onChanged={reload}
        />
      )}
    </div>
  );
}

function CardTile({
  c, canWrite, busy, dragging, onDragStart, onDragEnd, onDragOver, onOpen, onOpenSession, onSpawn,
}: {
  c: Card;
  canWrite: boolean;
  busy: boolean;
  dragging: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
  onDragOver: (e: React.DragEvent) => void;
  onOpen: () => void;
  onOpenSession: (sid: string) => void;
  onSpawn: () => void;
}) {
  const p = PRIORITIES[c.priority] ?? PRIORITIES[2];
  const done = c.checklist.filter((i) => i.done).length;
  return (
    <div
      draggable={canWrite}
      onDragStart={(e) => { onDragStart(); e.dataTransfer.effectAllowed = "move"; }}
      onDragEnd={onDragEnd}
      onDragOver={onDragOver}
      onClick={onOpen}
      style={{ borderLeftColor: p.border }}
      className={`cursor-pointer rounded-lg border border-line border-l-[3px] bg-panel2/60 p-2.5 transition-colors hover:bg-panel2 ${dragging ? "opacity-40" : ""} ${c.archived ? "opacity-50" : ""}`}
    >
      <div className="flex items-center gap-1.5">
        <span className="rounded bg-brass/15 px-1.5 text-[10px] text-brass">{c.tag}</span>
        {c.priority < 2 && <span className={`text-[10px] ${p.text}`}>{p.label}</span>}
        {c.due && (
          <span className={`rounded px-1 text-[10px] ring-1 ${dueTone(c.due)}`}>
            {new Date(`${c.due}T12:00:00`).toLocaleDateString("en-CH", { day: "2-digit", month: "2-digit" })}
          </span>
        )}
        {c.archived ? <span className="text-[10px] text-red-300/70">archived</span> : null}
        <span className="ml-auto text-[10px] text-mut/60">{ago(c.updated_at || c.created_at)}</span>
      </div>
      <div className="mt-1.5 text-[13px] leading-snug text-slate-100">{c.title}</div>
      {c.description && c.description !== c.title && (
        <div className="mt-0.5 line-clamp-2 text-[11px] text-mut">{c.description}</div>
      )}
      <div className="mt-2 flex items-center gap-2 text-[11px]" onClick={(e) => e.stopPropagation()}>
        {c.checklist.length > 0 && (
          <span className={`flex items-center gap-1 ${done === c.checklist.length ? "text-emerald-300" : "text-mut"}`}>
            ☑ {done}/{c.checklist.length}
          </span>
        )}
        {c.session_id ? (
          <button onClick={() => onOpenSession(c.session_id!)}
            className="rounded bg-sea/20 px-2 py-0.5 text-sea ring-1 ring-sea/30 hover:bg-sea/30">open</button>
        ) : canWrite && (
          <button onClick={onSpawn} disabled={busy}
            className="rounded bg-emerald-600/20 px-2 py-0.5 text-emerald-300 ring-1 ring-emerald-600/30 hover:bg-emerald-600/30 disabled:opacity-40">
            {busy ? "…" : "▶ spawn"}
          </button>
        )}
        {c.window && <span className="text-[10px] text-mut">{c.window.split(":").pop()}</span>}
      </div>
    </div>
  );
}
