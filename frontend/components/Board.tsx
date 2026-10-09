"use client";
import { useEffect, useMemo, useState } from "react";
import { addCard, fetchBoard, fetchTags, patchCard, spawnCard } from "@/lib/api";
import type { BoardData, Card } from "@/lib/types";
import { PRIORITIES } from "@/lib/fmt";
import { useCan } from "@/lib/me";
import CardModal from "./CardModal";
import BoardColumns from "./BoardColumns";
import { LevelSelect } from "./LevelBadge";


export default function Board({ onOpenSession }: { onOpenSession: (sid: string) => void }) {
  const [board, setBoard] = useState<BoardData | null>(null);
  const [tags, setTags] = useState<string[]>([]);
  const [prompt, setPrompt] = useState("");
  const [tag, setTag] = useState("backend");
  const [prio, setPrio] = useState(2);
  const [level, setLevel] = useState("project");
  const [busy, setBusy] = useState<number | null>(null);
  const canWrite = useCan("dev");
  const [openCard, setOpenCard] = useState<number | null>(null);
  // 3.4.4 deep link: /?plane=control&tab=board&project=<slug>&card=<id> (Teams outreach, Nina)
  // opens the card — `card=` was ignored, the board opened without it
  useEffect(() => {
    const v = new URLSearchParams(window.location.search).get("card");
    if (v && /^\d+$/.test(v)) setOpenCard(+v);
  }, []);
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
    await addCard(prompt.trim(), tag, "", "Backlog", prio, level);
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

  // déplacement (glisser-déposer de BoardColumns) : maj optimiste puis PATCH
  const move = async (id: number, bucket: string, sort: number) => {
    if (!board) return;
    const moved = Object.values(board.cards).flat().find((c) => c.id === id);
    if (!moved) return;
    setBoard((bd) => {
      if (!bd) return bd;
      const cards = Object.fromEntries(
        Object.entries(bd.cards).map(([k, v]) => [k, v.filter((c) => c.id !== id)])
      ) as typeof bd.cards;
      const updated = { ...moved, bucket, sort };
      cards[bucket] = [...(cards[bucket] || []), updated].sort((a, b) => a.sort - b.sort || a.id - b.id);
      return { ...bd, cards };
    });
    await patchCard(id, { bucket, sort });
    reload();
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* barre : créer + filtres */}
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel/60 p-2">
        {canWrite && (
          <>
            <select aria-label="Tag of the new card" value={tag} onChange={(e) => setTag(e.target.value)}
              className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
              {tags.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select aria-label="Priority of the new card" value={prio} onChange={(e) => setPrio(Number(e.target.value))}
              className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
              {Object.entries(PRIORITIES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
            <LevelSelect value={level} onChange={setLevel} />
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
        <select aria-label="Filter by tag" value={fTag} onChange={(e) => setFTag(e.target.value)}
          className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
          <option value="">all tags</option>
          {tags.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <label className="flex cursor-pointer items-center gap-1.5 text-[11.5px] text-mut">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} className="accent-slate-500" />
          archived
        </label>
      </div>

      <BoardColumns
        buckets={buckets} cards={visible} canWrite={canWrite} busy={busy}
        onMove={move} onOpen={setOpenCard} onOpenSession={onOpenSession} onSpawn={doSpawn}
      />

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
