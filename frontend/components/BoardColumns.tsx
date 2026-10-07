"use client";
import { useState } from "react";
import type { Card } from "@/lib/types";
import { PRIORITIES, ago, dueTone } from "@/lib/fmt";

// The board's columns and cards — ONE component for the Board tab and for the kanban of a
// card in Helm (3.3: opening a parent card shows ITS board, its children by column).

const BUCKET_TONES: Record<string, string> = {
  Backlog: "text-slate-300", Doing: "text-sky-300", Review: "text-amber-300", Done: "text-emerald-300",
};

type DropAt = { bucket: string; index: number } | null;

export default function BoardColumns<C extends Card>({
  buckets, cards, canWrite, busy, onMove, onOpen, onOpenSession, onSpawn, extra, compact,
}: {
  buckets: string[];
  cards: Record<string, C[]>;
  canWrite: boolean;
  busy?: number | null;
  onMove?: (id: number, bucket: string, sort: number) => void;
  onOpen: (id: number) => void;
  onOpenSession?: (sid: string) => void;
  onSpawn?: (c: C) => void;
  /** extra line on a card (Helm: computed state, sub-cards) */
  extra?: (c: C) => React.ReactNode;
  compact?: boolean;
}) {
  const [drag, setDrag] = useState<number | null>(null);
  const [dropAt, setDropAt] = useState<DropAt>(null);
  const draggable = canWrite && !!onMove;

  // dépose : insère à dropAt.index dans la colonne (sort = milieu des voisins)
  const drop = () => {
    const id = drag;
    const at = dropAt;
    setDrag(null);
    setDropAt(null);
    if (id == null || !at || !onMove) return;
    const col = (cards[at.bucket] ?? []).filter((c) => c.id !== id);
    const i = Math.min(at.index, col.length);
    const prev = col[i - 1];
    const next = col[i];
    const sort =
      prev && next ? (prev.sort + next.sort) / 2
      : prev ? prev.sort + 1
      : next ? next.sort - 1
      : Date.now() / 1000;
    onMove(id, at.bucket, sort);
  };

  return (
    <div className={`flex min-h-0 flex-1 gap-2.5 overflow-x-auto ${compact ? "p-0" : "p-2.5"}`}>
      {buckets.map((b) => {
        const col = cards[b] ?? [];
        return (
          <div
            key={b}
            className={`flex ${compact ? "min-w-[220px] flex-1" : "w-80 shrink-0"} flex-col rounded-xl border bg-panel ${dropAt?.bucket === b ? "border-sea/60 ring-1 ring-sea/30" : "border-line"}`}
            onDragOver={(e) => { if (!draggable) return; e.preventDefault(); if (dropAt?.bucket !== b) setDropAt({ bucket: b, index: col.length }); }}
            onDrop={drop}
          >
            <div className="flex items-center gap-2 border-b border-line px-3 py-2">
              <span className={`text-[12.5px] font-semibold ${BUCKET_TONES[b] || "text-slate-200"}`}>{b}</span>
              <span className="rounded-full bg-panel2 px-1.5 text-[10.5px] text-mut">{col.length}</span>
            </div>
            <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
              {col.map((c, i) => (
                <div key={c.id}>
                  {dropAt?.bucket === b && dropAt.index === i && drag !== c.id && (
                    <div className="mb-2 h-0.5 rounded bg-sea/70" />
                  )}
                  <CardTile
                    c={c} canWrite={canWrite} draggable={draggable} busy={busy === c.id}
                    dragging={drag === c.id}
                    onDragStart={() => setDrag(c.id)}
                    onDragEnd={() => { setDrag(null); setDropAt(null); }}
                    onDragOver={(e) => {
                      if (!draggable) return;
                      e.preventDefault(); e.stopPropagation();
                      const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
                      const idx = e.clientY < r.top + r.height / 2 ? i : i + 1;
                      if (dropAt?.bucket !== b || dropAt.index !== idx) setDropAt({ bucket: b, index: idx });
                    }}
                    onOpen={() => onOpen(c.id)}
                    onOpenSession={onOpenSession}
                    onSpawn={onSpawn ? () => onSpawn(c) : undefined}
                    extra={extra ? extra(c) : null}
                  />
                </div>
              ))}
              {dropAt?.bucket === b && dropAt.index >= col.length && drag != null && (
                <div className="h-0.5 rounded bg-sea/70" />
              )}
              {!col.length && <div className="px-2 py-6 text-center text-[11.5px] text-mut/60">—</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function CardTile({
  c, canWrite, draggable, busy, dragging, onDragStart, onDragEnd, onDragOver, onOpen, onOpenSession, onSpawn, extra,
}: {
  c: Card;
  canWrite: boolean;
  draggable: boolean;
  busy: boolean;
  dragging: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
  onDragOver: (e: React.DragEvent) => void;
  onOpen: () => void;
  onOpenSession?: (sid: string) => void;
  onSpawn?: () => void;
  extra?: React.ReactNode;
}) {
  const p = PRIORITIES[c.priority] ?? PRIORITIES[2];
  const done = c.checklist.filter((i) => i.done).length;
  return (
    <div
      draggable={draggable}
      onDragStart={(e) => { onDragStart(); e.dataTransfer.effectAllowed = "move"; }}
      onDragEnd={onDragEnd}
      onDragOver={onDragOver}
      onClick={onOpen}
      style={{ borderLeftColor: p.border }}
      className={`cursor-pointer rounded-lg border border-line border-l-[3px] bg-panel2/60 p-2.5 transition-colors hover:bg-panel2 ${dragging ? "opacity-40" : ""} ${c.archived ? "opacity-50" : ""}`}
    >
      <div className="flex items-center gap-1.5">
        <span className="rounded bg-brass/15 px-1.5 text-[10px] text-brass">{c.tag}</span>
        {c.kind === "project" && <span className="rounded bg-sea/15 px-1.5 text-[10px] text-sea">project</span>}
        {c.kind === "reframe" && <span className="rounded bg-amber-500/15 px-1.5 text-[10px] text-amber-300">reframe</span>}
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
      {extra}
      <div className="mt-2 flex items-center gap-2 text-[11px]" onClick={(e) => e.stopPropagation()}>
        {c.checklist.length > 0 && (
          <span className={`flex items-center gap-1 ${done === c.checklist.length ? "text-emerald-300" : "text-mut"}`}>
            ☑ {done}/{c.checklist.length}
          </span>
        )}
        {c.assignee && <span className="truncate text-[10.5px] text-mut" title="assignee">👤 {c.assignee}</span>}
        {c.session_id && onOpenSession ? (
          <button onClick={() => onOpenSession(c.session_id!)}
            className="rounded bg-sea/20 px-2 py-0.5 text-sea ring-1 ring-sea/30 hover:bg-sea/30">open</button>
        ) : canWrite && onSpawn && !c.session_id && (
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
