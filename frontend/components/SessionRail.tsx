"use client";
import { useEffect, useState } from "react";
import { deleteSession, fetchSessions, fetchTags, spawnSession } from "@/lib/api";
import type { SessionSummary } from "@/lib/types";
import { useCan } from "@/lib/me";
import { useFeatures } from "@/lib/features";

function ago(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}j`;
}

export default function SessionRail({
  open,
  onOpen,
  onDelete,
}: {
  open: string[];
  onOpen: (s: { session_id: string; kind?: "sdk" | "tmux"; title?: string; tag?: string }) => void;
  onDelete: (id: string) => void;
}) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [tag, setTag] = useState("backend");
  const [prompt, setPrompt] = useState("");
  const [adding, setAdding] = useState(false);
  const [asTmux, setAsTmux] = useState(false);
  const [busy, setBusy] = useState(false);
  const canWrite = useCan("dev");
  const feats = useFeatures();

  const reload = async () => {
    try { setSessions(await fetchSessions()); } catch { /* keep */ }
  };
  useEffect(() => {
    reload();
    fetchTags().then(setTags).catch(() => {});
    const iv = setInterval(reload, 5000);
    return () => clearInterval(iv);
  }, []);

  const create = async () => {
    setBusy(true);
    try {
      const kind = asTmux ? "tmux" as const : "sdk" as const;
      const s = await spawnSession(tag, prompt, "", kind);
      setPrompt(""); setAdding(false);
      onOpen({ session_id: s.session_id, kind, title: s.title, tag: s.tag });
      reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-line bg-panel">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="text-[12px] font-semibold text-slate-200">Sessions</span>
        {canWrite && (
          <button
            onClick={() => setAdding(!adding)}
            className="ml-auto rounded bg-emerald-600/20 px-2 py-0.5 text-[11px] text-emerald-300 ring-1 ring-emerald-600/30 hover:bg-emerald-600/30"
          >+ session</button>
        )}
      </div>

      {adding && (
        <div className="space-y-1.5 border-b border-line bg-panel2/40 p-2">
          <select
            value={tag}
            onChange={(e) => setTag(e.target.value)}
            className="w-full rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200"
          >
            {tags.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={2}
            placeholder="prompt initial (optionnel)…"
            className="w-full resize-y rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50"
          />
          {feats.tmux && (
          <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-mut">
            <input type="checkbox" checked={asTmux} onChange={(e) => setAsTmux(e.target.checked)} className="accent-slate-500" />
            terminal (tmux) — mode power user
          </label>
          )}
          <button
            onClick={create}
            disabled={busy}
            className="w-full rounded bg-sea/80 py-1 text-[12px] font-medium text-white disabled:opacity-40 hover:bg-sea"
          >{busy ? "création…" : `ouvrir une session « ${tag} »`}</button>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {sessions.map((s) => {
          const isOpen = open.includes(s.session_id);
          const del = async () => {
            await deleteSession(s.session_id);
            onDelete(s.session_id);
            reload();
          };
          return (
            <div
              key={s.session_id}
              className={`group flex items-start gap-2 border-b border-line/50 px-3 py-2 hover:bg-panel2 ${isOpen ? "bg-panel2" : ""}`}
            >
              <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
                !s.alive ? "bg-slate-600"
                  : s.live_state === "working" ? "animate-pulse bg-emerald-400"
                  : s.live_state === "awaiting" ? "animate-pulse bg-amber-400"
                  : s.live_state === "booting" ? "bg-sky-400"
                  : s.active ? "bg-emerald-500" : "bg-emerald-700"
              }`} />
              <button onClick={() => onOpen(s)} className="min-w-0 flex-1 text-left">
                <span className="flex items-center gap-1.5">
                  <span className="rounded bg-brass/15 px-1.5 text-[10px] text-brass">
                    {s.kind === "sdk" ? s.tag : s.window.split(":").pop()}
                  </span>
                  {s.kind !== "sdk" && <span className="rounded bg-panel2 px-1 text-[9px] text-mut ring-1 ring-line">term</span>}
                  {isOpen && <span className="text-[9px] text-brass">●</span>}
                  {!s.alive && <span className="text-[9px] text-mut">terminée</span>}
                </span>
                <span className={`mt-0.5 block truncate text-[12px] ${s.alive ? "text-slate-200" : "text-mut line-through"}`}>{s.title}</span>
                <span className="block text-[10px] text-mut">{
                  !s.alive ? "fenêtre fermée"
                    : s.live_state === "booting" ? "démarrage…"
                    : s.live_state === "working" ? "en cours…"
                    : s.live_state === "awaiting" ? "choix en attente"
                    : s.exists ? ago(s.age_s) : "prête"
                }</span>
              </button>
              <button
                onClick={del}
                className="mt-0.5 rounded px-1 text-mut opacity-0 hover:text-red-400 group-hover:opacity-100"
                title="supprimer la session (ferme la fenêtre tmux)"
              >✕</button>
            </div>
          );
        })}
        {!sessions.length && (
          <div className="px-3 py-3 text-[12px] text-mut">aucune session — clique « + session »</div>
        )}
      </div>
    </aside>
  );
}
