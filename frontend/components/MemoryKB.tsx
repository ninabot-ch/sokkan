"use client";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { memoryNote, memoryNotes, memorySearch, memoryStats } from "@/lib/api";
import type { MemNote, MemSearchResult, MemStats } from "@/lib/types";

function ago(ts: number | null): string {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}j`;
}

export default function MemoryKB() {
  const [stats, setStats] = useState<MemStats | null>(null);
  const [notes, setNotes] = useState<MemNote[]>([]);
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MemSearchResult[] | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [body, setBody] = useState("");

  useEffect(() => {
    memoryStats().then(setStats).catch(() => {});
    memoryNotes().then(setNotes).catch(() => {});
  }, []);

  // recherche sémantique (debounce)
  useEffect(() => {
    if (!query.trim()) { setResults(null); return; }
    const t = setTimeout(() => { memorySearch(query, 10).then(setResults).catch(() => {}); }, 350);
    return () => clearTimeout(t);
  }, [query]);

  // corps de la note sélectionnée
  useEffect(() => {
    if (!sel) { setBody(""); return; }
    memoryNote(sel).then((d) => setBody(d.body)).catch(() => setBody(""));
  }, [sel]);

  const byName = (n: string) => notes.find((x) => x.name === n);
  const pick = (n: string) => { setSel(n); setQuery(""); };
  const note = sel ? byName(sel) : null;
  const filtered = notes.filter((n) =>
    !filter || n.name.includes(filter.toLowerCase()) || (n.description || "").toLowerCase().includes(filter.toLowerCase())
  );

  const Chip = ({ n }: { n: string }) => (
    <button onClick={() => pick(n)}
      className={`rounded px-1.5 py-0.5 text-[10.5px] ${byName(n) ? "bg-brass/15 text-brass hover:bg-brass/25" : "bg-panel2 text-mut line-through"}`}>
      {n}
    </button>
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-line bg-panel/60 px-3 py-1.5">
        <span className="text-[12px] text-mut">
          {stats ? <>{stats.notes} notes · {stats.chunks} chunks · <span className="text-slate-300">{stats.model}</span> · réindex {ago(stats.last_mtime)}</> : "…"}
        </span>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="🔎 recherche sémantique (playground RAG)…"
          className="ml-auto w-80 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50"
        />
      </div>

      <div className="flex min-h-0 flex-1">
        {/* liste des notes */}
        <aside className="flex w-72 shrink-0 flex-col border-r border-line">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filtrer les notes…"
            className="m-2 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50"
          />
          <div className="min-h-0 flex-1 overflow-y-auto">
            {filtered.map((n) => (
              <button key={n.name} onClick={() => pick(n.name)}
                className={`block w-full border-b border-line/40 px-3 py-1.5 text-left hover:bg-panel2 ${sel === n.name ? "bg-panel2" : ""}`}>
                <span className="block truncate text-[12px] text-slate-200">{n.name}</span>
                <span className="block truncate text-[10px] text-mut">{n.type} · {n.chunks} chunks · {n.links.length}↗ {n.backlinks.length}↘</span>
              </button>
            ))}
          </div>
        </aside>

        {/* détail / résultats */}
        <main className="min-h-0 flex-1 overflow-y-auto p-3">
          {results ? (
            <div className="space-y-2">
              <div className="text-[11px] text-mut">{results.length} résultats pour « {query} »</div>
              {results.map((r) => (
                <button key={r.note_name} onClick={() => pick(r.note_name)}
                  className="block w-full rounded-lg border border-line bg-panel2/50 p-2 text-left hover:bg-panel2">
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-sea/15 px-1.5 text-[10px] text-sea">{r.score}</span>
                    <span className="truncate text-[12.5px] text-slate-100">{r.note_name}</span>
                  </div>
                  <div className="mt-0.5 line-clamp-2 text-[11px] text-mut">{r.snippet}</div>
                </button>
              ))}
            </div>
          ) : note ? (
            <div>
              <div className="text-[15px] font-semibold text-slate-100">{note.name}</div>
              <div className="mt-0.5 text-[11px] text-mut">{note.type}</div>
              {note.description && <div className="mt-1 text-[12.5px] text-slate-300">{note.description}</div>}
              {(note.links.length > 0 || note.backlinks.length > 0) && (
                <div className="mt-2 space-y-1 rounded-lg border border-line bg-panel2/30 p-2">
                  {note.links.length > 0 && <div className="flex flex-wrap items-center gap-1"><span className="text-[10.5px] text-mut">cite ↗</span>{note.links.map((l) => <Chip key={l} n={l} />)}</div>}
                  {note.backlinks.length > 0 && <div className="flex flex-wrap items-center gap-1"><span className="text-[10.5px] text-mut">cité par ↘</span>{note.backlinks.map((l) => <Chip key={l} n={l} />)}</div>}
                </div>
              )}
              <div className="md mt-3 text-slate-200"><ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown></div>
            </div>
          ) : (
            <div className="mt-10 text-center text-[13px] text-mut">choisis une note à gauche, ou lance une recherche sémantique en haut</div>
          )}
        </main>
      </div>
    </div>
  );
}
