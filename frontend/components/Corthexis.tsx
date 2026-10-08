"use client";
/* CortHeXis — the project memory, visible and repairable.
 * Graph of the notes (CorthexisGraph), a side panel with the selected note, the
 * memory's health (score, history, findings with their remedy) and the repairs
 * waiting for approval. Every repair is a proposal: the diff is shown, nothing is
 * written until someone approves it. */
import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import CorthexisGraph, { type Mode, TYPE_COLORS } from "@/components/CorthexisGraph";
import MemoryBench from "@/components/MemoryBench";
import { QuarantineButton } from "@/components/Quarantine";
import { memoryDigest, memorySearch, memoryStats, spawnSession } from "@/lib/api";
import { useCan } from "@/lib/me";
import { currentProject } from "@/lib/project";
import {
  cxCuration, cxDecide, cxGraph, cxNote, cxProposals, cxPropose, cxReview, cxRunReview,
  type CxFinding, type CxGraph, type CxItem, type CxNote, type CxOverview, type CxProposal, type CxReport,
  type CxProposalIn, type Severity,
} from "@/lib/corthexis";
import type { MemSearchResult, MemStore } from "@/lib/types";
import { LevelControl } from "./LevelBadge";
import { setNoteLevel } from "@/lib/classification";

const SEV: Record<Severity, { label: string; dot: string; ring: string; text: string }> = {
  crit: { label: "critical", dot: "bg-[#ff5c6c]", ring: "border-[#ff5c6c]/50", text: "text-[#ff8a96]" },
  warn: { label: "to fix", dot: "bg-[#ffb454]", ring: "border-[#ffb454]/40", text: "text-[#ffc98a]" },
  info: { label: "to watch", dot: "bg-slate-500", ring: "border-line", text: "text-slate-300" },
};
const CATEGORY: Record<string, string> = {
  chain: "Memory reachable by the agents", security: "Safety", structure: "Note format",
  drift: "Out of date", graph: "Links between notes", dates: "Dates", recall: "Recall quality (bench)",
};
const MODES: { id: Mode; label: string; title: string }[] = [
  { id: "synapse", label: "Synapses", title: "Layout by links between notes" },
  { id: "semantic", label: "Constellation", title: "Position = meaning: notes about the same thing sit together" },
  { id: "age", label: "Age", title: "Colour = time since the last update (cyan = fresh)" },
  { id: "health", label: "Health", title: "Only the notes flagged by the review stay lit" },
];
const fmt = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("en-US"));
/** 3.4.3 — the review of a project may not have run (or cannot, on the 2.x index): every
 *  field the panels read is defaulted here, so a partial payload never crashes the page. */
const EMPTY_REPORT: CxReport = {
  at: null, duration_ms: 0, score: null, signature: "", notes_total: 0,
  counts: { crit: 0, warn: 0, info: 0 }, source: null, skipped: [], findings: [], flags: {},
};
const safeReport = (ov: CxOverview | null): CxReport => {
  const r = ov?.report;
  return { ...EMPTY_REPORT, ...(r || {}), counts: { ...EMPTY_REPORT.counts, ...(r?.counts || {}) },
    skipped: r?.skipped ?? [], findings: r?.findings ?? [], flags: r?.flags ?? {} };
};
const sev = (s: Severity | undefined) => SEV[s as Severity] ?? SEV.info;
/** 3.4.2 — how to turn the 3.0 store on (the 2.x index has no project memory). */
export const STORE_DOC_URL = "https://github.com/ninabot-ch/sokkan/blob/main/docs/UPGRADE.md#turn-the-store-on-later-sqlite-mode";
const when = (iso: string | null | undefined) => iso
  ? new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }) : "—";

export default function Corthexis({ onOpenSession }: { onOpenSession?: (sid: string) => void }) {
  const canAct = useCan("dev");
  const [graph, setGraph] = useState<CxGraph | null>(null);
  const [ov, setOv] = useState<CxOverview | null>(null);
  const [props, setProps] = useState<CxProposal[]>([]);
  const [mode, setMode] = useState<Mode>("synapse");
  const [labels, setLabels] = useState(false);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [panel, setPanel] = useState<"note" | "health" | "repairs" | "bench" | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [note, setNote] = useState<CxNote | null>(null);
  const [ghost, setGhost] = useState<{ label: string; citedBy: string[] } | null>(null);
  const [highlight, setHighlight] = useState<Map<string, number>>(new Map());
  const [flashes, setFlashes] = useState<Map<string, number>>(new Map());
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MemSearchResult[] | null>(null);
  const [modal, setModal] = useState<CxProposal | null>(null);
  const [busy, setBusy] = useState("");
  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const [offline, setOffline] = useState(false);
  const [store, setStore] = useState<MemStore | null>(null);
  const project = currentProject();
  // 3.4.2: on the 2.x index, a project other than `default` has NO memory here — say it
  const noProjectMemory = !!store && !store.project_memory && project !== "default";

  const toast = useCallback((text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);

  // ---------------------------------------------------------------- data
  const loadGraph = useCallback(async (first = false) => {
    try {
      const g = await cxGraph(first ? "" : graph?.version || "");
      setOffline(false);
      if (g.unchanged) return;
      if (graph && !first) {
        const prev = new Map(graph.nodes.map((n) => [n.id, n]));
        const fl = new Map<string, number>();
        for (const n of g.nodes) {
          const o = prev.get(n.id);
          if (!o) { fl.set(n.id, performance.now()); toast(`New note · ${n.id}`); }
          else if (o.words !== n.words || o.modified !== n.modified) { fl.set(n.id, performance.now()); toast(`Note updated · ${n.id}`); }
        }
        if (fl.size) setFlashes(fl);
      }
      setGraph(g);
    } catch { setOffline(true); }
  }, [graph, toast]);
  const loadReview = useCallback(() => {
    cxReview().then(setOv).catch(() => {});
    cxProposals().then(setProps).catch(() => {});
  }, []);

  useEffect(() => {
    memoryStats().then((s) => setStore(s.store ?? null)).catch(() => {});
  }, []);

  useEffect(() => {
    loadGraph(true);
    loadReview();
    const q = new URLSearchParams(window.location.search);
    if (q.get("note")) openNote(q.get("note")!);
    else if (q.get("proposal")) setPanel("repairs");
    else if (q.get("panel") === "bench") setPanel("bench");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const a = setInterval(() => loadGraph(false), 20000);
    const b = setInterval(loadReview, 5 * 60000);
    return () => { clearInterval(a); clearInterval(b); };
  }, [loadGraph, loadReview]);
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("proposal");
    const p = id && props.find((x) => x.id === id && x.status === "pending");
    if (p) setModal(p);
  }, [props]);

  // recall
  useEffect(() => {
    if (query.trim().length < 2) { setResults(null); setHighlight(new Map()); return; }
    const t = setTimeout(() => {
      memorySearch(query, 12).then((r) => {
        const rows = (r || []).filter((x) => x.note_name);
        setResults(rows);
        const top = rows[0]?.score || 1;
        setHighlight(new Map(rows.map((x, i) => [x.note_name, Math.max(0.15, x.score / top) * (1 - i * 0.04)])));
      }).catch(() => setResults([]));
    }, 320);
    return () => clearTimeout(t);
  }, [query]);

  function openNote(id: string) {
    setSel(id); setGhost(null); setPanel("note"); setNote(null);
    cxNote(id).then(setNote).catch(() => setNote(null));
  }
  function lightNotes(names: string[]) {
    setQuery(""); setResults(null);
    setHighlight(new Map(names.map((n) => [n, 1])));
  }

  // ---------------------------------------------------------------- actions
  async function propose(p: CxProposalIn) {
    setBusy("propose");
    try { const r = await cxPropose(p); setModal(r); cxProposals().then(setProps); }
    catch (e) { toast(`Cannot prepare this repair: ${(e as Error).message}`); }
    finally { setBusy(""); }
  }
  async function decide(p: CxProposal, approve: boolean) {
    setBusy(approve ? "approve" : "refuse");
    try {
      await cxDecide(p.id, approve);
      toast(approve ? `Applied · ${p.title}` : `Refused · ${p.title}`);
      setModal(null);
      setTimeout(() => { loadReview(); loadGraph(false); if (sel) openNote(sel); }, 2500);
    } catch (e) { toast((e as Error).message); setModal(null); }
    finally { setBusy(""); cxProposals().then(setProps); }
  }
  async function curate(findingIds: string[], notes: string[] = []) {
    setBusy("curation");
    try { const s = await cxCuration(findingIds, notes); toast("Curation session started"); onOpenSession?.(s.session_id); }
    catch (e) { toast(`Cannot start the session: ${(e as Error).message}`); }
    finally { setBusy(""); }
  }
  async function rerun() {
    setBusy("review");
    try { setOv(await cxRunReview()); loadGraph(false); toast("Review done"); }
    catch (e) { toast((e as Error).message); }
    finally { setBusy(""); }
  }

  const pending = props.filter((p) => p.status === "pending");
  const score = safeReport(ov).score;
  const scoreColor = score == null ? "#64748b" : score >= 80 ? "#3ecfb2" : score >= 55 ? "#ffb454" : "#ff5c6c";
  const types = useMemo(() => Object.keys(graph?.stats.types || {}).sort(), [graph]);

  return (
    <div className="relative flex min-h-0 flex-1 overflow-hidden bg-[#06080d]">
      <CorthexisGraph graph={graph} mode={mode} labels={labels} hidden={hidden} highlight={highlight}
        selected={sel} flashes={flashes} onPick={openNote}
        onGhost={(label, citedBy) => { setGhost({ label, citedBy }); setSel(null); setPanel("note"); }} />

      {/* top bar */}
      <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex flex-wrap items-start gap-3 p-3">
        <div className="pointer-events-auto flex items-center gap-3 rounded-xl border border-line bg-panel/80 px-3 py-2 backdrop-blur">
          <div className="leading-tight">
            <div className="text-[17px] font-semibold tracking-tight text-slate-100">Cort<span className="text-brass">HeXis</span></div>
            <div className="text-[10.5px] text-mut">the project memory, live</div>
          </div>
          {[["notes", graph?.stats.notes], ["words", graph?.stats.words], ["links", graph?.stats.links], ["passages", graph?.stats.chunks]].map(([k, v]) => (
            <div key={k as string} className="hidden text-center sm:block">
              <div className="font-mono text-[15px] text-slate-100">{fmt(v as number)}</div>
              <div className="text-[10px] uppercase tracking-wide text-mut">{k}</div>
            </div>
          ))}
          <button onClick={() => setPanel(panel === "health" ? null : "health")} title="Memory health (automatic review)"
            className="relative h-12 w-12 shrink-0">
            <svg viewBox="0 0 64 64" className="h-12 w-12 -rotate-90">
              <circle cx="32" cy="32" r="27" fill="none" stroke="#1f2937" strokeWidth="5" />
              <circle cx="32" cy="32" r="27" fill="none" stroke={scoreColor} strokeWidth="5" strokeLinecap="round"
                strokeDasharray="170" strokeDashoffset={170 - 170 * (score ?? 0) / 100} style={{ transition: "stroke-dashoffset 1s" }} />
            </svg>
            <span className="absolute inset-0 flex flex-col items-center justify-center">
              <span className="font-mono text-[14px] font-semibold text-slate-100">{score ?? "—"}</span>
              <span className="text-[8px] uppercase text-mut">health</span>
            </span>
          </button>
          <span className={`flex items-center gap-1.5 text-[10.5px] ${offline ? "text-[#ff8a96]" : "text-mut"}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${offline ? "bg-[#ff5c6c]" : "bg-emerald-400 animate-pulse"}`} />
            {offline ? "offline" : "live"}
          </span>
        </div>

        <div className="pointer-events-auto relative min-w-[260px] flex-1 max-w-xl">
          <input value={query} onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") setQuery(""); if (e.key === "Enter" && results?.[0]) openNote(results[0].note_name); }}
            placeholder="Ask the memory a question — the notes that answer light up"
            className="w-full rounded-xl border border-line bg-panel/80 px-3 py-2.5 text-[13px] text-slate-100 outline-none backdrop-blur placeholder:text-mut focus:border-sea/60" />
          {results && (
            <div className="absolute inset-x-0 top-full mt-1 max-h-[50vh] overflow-y-auto rounded-xl border border-line bg-panel/95 p-1 shadow-xl backdrop-blur">
              {results.length === 0 && <div className="px-3 py-2 text-[12px] text-mut">nothing in memory about that</div>}
              {results.map((r) => (
                <button key={r.note_name} onClick={() => openNote(r.note_name)}
                  className="flex w-full gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-panel2">
                  <span className="mt-0.5 shrink-0 rounded bg-sea/15 px-1.5 font-mono text-[10px] text-sea">{r.score.toFixed(2)}</span>
                  <span className="min-w-0">
                    <span className="block truncate text-[12.5px] text-slate-100">{r.note_name}</span>
                    <span className="line-clamp-2 text-[11px] text-mut">{r.snippet}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        {noProjectMemory && (
          <div role="alert" className="pointer-events-auto basis-full rounded-xl border border-amber-400/40 bg-amber-400/10 px-3 py-2 text-[12.5px] text-amber-100 backdrop-blur">
            <span className="font-medium">Project memory needs the 3.0 store.</span>{" "}
            This instance serves the 2.x index (sqlite){store?.migrating ? " while the store is being built" : ""}: the notes of
            « {project} » are not indexed and nothing can be written here — decisions from Teams, agent deliverables and
            notes of sessions are refused rather than lost.{" "}
            {store?.migrating
              ? <span>The migration is in progress: come back once it serves.</span>
              : <span>Enable it: <code className="rounded bg-black/30 px-1">CORTHEXIS_DATABASE_URL</code> + <code className="rounded bg-black/30 px-1">CORTHEXIS_MEMORY_BACKEND=auto</code> —{" "}
                <a href={STORE_DOC_URL} target="_blank" rel="noreferrer" className="underline decoration-amber-300/60 hover:text-white">how to turn it on</a>.</span>}
          </div>
        )}

        <div className="pointer-events-auto ml-auto flex items-center gap-1.5">
          {canAct && <QuarantineButton />}
          <button onClick={() => setPanel(panel === "repairs" ? null : "repairs")}
            className={`rounded-lg border px-2.5 py-1.5 text-[12px] backdrop-blur ${pending.length ? "border-brass/60 bg-brass/15 text-brass" : "border-line bg-panel/80 text-mut hover:text-slate-200"}`}>
            Repairs{pending.length ? ` · ${pending.length} to approve` : ""}
          </button>
          <button onClick={() => memoryDigest().then((s) => onOpenSession?.(s.session_id)).catch(() => toast("digest unavailable"))}
            title="A session summarises the project state into the note project-status"
            className="rounded-lg border border-line bg-panel/80 px-2.5 py-1.5 text-[12px] text-mut backdrop-blur hover:text-slate-200">
            Summarise
          </button>
          <button onClick={() => spawnSession("docs", "", "", "sdk", "onboard-memory").then((s) => onOpenSession?.(s.session_id)).catch(() => toast("cannot start"))}
            title="A session reads the project and writes the first notes"
            className={`rounded-lg border px-2.5 py-1.5 text-[12px] backdrop-blur ${graph && graph.stats.notes === 0 ? "border-amber-500/50 bg-amber-500/10 text-amber-300" : "border-line bg-panel/80 text-mut hover:text-slate-200"}`}>
            First notes
          </button>
        </div>
      </div>

      {/* reading modes + legend */}
      <div className="absolute bottom-3 left-3 z-20 flex flex-col gap-2">
        <div className="flex flex-wrap gap-1 rounded-xl border border-line bg-panel/80 p-1 backdrop-blur">
          {MODES.map((m) => (
            <button key={m.id} title={m.title}
              onClick={() => { setMode(m.id); if (m.id === "health") setPanel("health"); }}
              className={`rounded-lg px-2.5 py-1 text-[12px] ${mode === m.id ? "bg-panel2 text-slate-100 ring-1 ring-line" : "text-mut hover:text-slate-200"}`}>
              {m.label}
            </button>
          ))}
          <label className="ml-1 flex items-center gap-1 px-1 text-[11px] text-mut">
            <input type="checkbox" checked={labels} onChange={(e) => setLabels(e.target.checked)} /> names
          </label>
        </div>
        <div className="flex flex-wrap gap-1 rounded-xl border border-line bg-panel/80 p-1.5 text-[11px] backdrop-blur">
          {[...types, "ghost"].map((t) => (
            <button key={t} onClick={() => setHidden((h) => { const n = new Set(h); if (n.has(t)) n.delete(t); else n.add(t); return n; })}
              className={`flex items-center gap-1 rounded px-1.5 ${hidden.has(t) ? "opacity-35" : ""}`}>
              <i className="inline-block h-2 w-2 rounded-full" style={{ background: t === "ghost" ? "#ff5c6c" : TYPE_COLORS[t] || TYPE_COLORS.unknown }} />
              <span className="text-slate-300">{t === "ghost" ? "missing note" : t === "feedback" ? "rule" : t === "unknown" ? "no type" : t}</span>
            </button>
          ))}
          {highlight.size > 0 && !query && (
            <button onClick={() => setHighlight(new Map())} className="rounded px-1.5 text-sea">clear highlight</button>
          )}
        </div>
      </div>

      {/* side panel */}
      {panel && (
        <aside className="absolute bottom-0 right-0 top-0 z-30 flex w-full max-w-[460px] flex-col border-l border-line bg-panel/95 backdrop-blur">
          <div className="flex items-center gap-1 border-b border-line px-2 py-1.5">
            {(["note", "health", "repairs", "bench"] as const).map((t) => (
              <button key={t} onClick={() => setPanel(t)}
                className={`rounded-md px-3 py-1 text-[12.5px] ${panel === t ? "bg-panel2 text-slate-100 ring-1 ring-line" : "text-mut hover:text-slate-200"}`}>
                {t === "note" ? "Note" : t === "health" ? "Health" : t === "repairs" ? "Repairs" : "Bench"}
                {t === "health" && safeReport(ov).counts.crit > 0 && <span className="ml-1.5 rounded-full bg-[#ff5c6c] px-1.5 text-[10px] text-white">{safeReport(ov).counts.crit}</span>}
                {t === "repairs" && pending.length > 0 && <span className="ml-1.5 rounded-full bg-brass px-1.5 text-[10px] text-ink">{pending.length}</span>}
              </button>
            ))}
            <button onClick={() => { setPanel(null); setSel(null); }} className="ml-auto rounded px-2 text-[16px] text-mut hover:text-slate-200" title="close">×</button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {panel === "note" && <NotePanel note={note} ghost={ghost} sel={sel} openNote={openNote}
              canAct={canAct} busy={busy} propose={propose} curate={curate} />}
            {panel === "health" && <HealthPanel ov={ov} canAct={canAct} busy={busy} rerun={rerun}
              openNote={openNote} light={lightNotes} propose={propose} curate={curate} />}
            {panel === "repairs" && <RepairsPanel props={props} open={setModal} />}
            {panel === "bench" && <MemoryBench noteNames={(graph?.nodes || []).map((n) => n.id)} onPickNote={openNote} />}
          </div>
        </aside>
      )}

      {modal && <ProposalModal p={modal} canAct={canAct} busy={busy} close={() => setModal(null)} decide={decide}
        swap={modal.kind === "merge" && modal.status === "pending" ? () => {
          cxDecide(modal.id, false).catch(() => {});
          propose({ kind: "merge", keep: modal.params.drop, drop: modal.params.keep });
        } : undefined} />}

      <div className="pointer-events-none absolute bottom-3 right-3 z-40 flex flex-col items-end gap-1.5">
        {toasts.map((t) => <div key={t.id} className="rounded-lg border border-line bg-panel/95 px-3 py-1.5 text-[12px] text-slate-200 shadow-lg">{t.text}</div>)}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ actions on an item

function ItemActions({ f, it, canAct, busy, propose }: {
  f: { id: string; action: CxFinding["action"] }; it: CxItem; canAct: boolean; busy: string;
  propose: (p: CxProposalIn) => void;
}) {
  const btn = "rounded-md border border-sea/40 bg-sea/10 px-2 py-0.5 text-[11px] text-sky-200 hover:bg-sea/20 disabled:opacity-40";
  const dis = !canAct || !!busy;
  const tip = canAct ? "" : "you need the developer role to repair the memory";
  if (f.action === "relink" && it.suggestion)
    return <button className={btn} disabled={dis} title={tip} onClick={() => propose({ kind: "relink", target: String(it.target), new_target: String(it.suggestion), note: String(it.note) })}>
      Point to “{String(it.suggestion)}”</button>;
  if (f.action === "merge" && it.other)
    return <button className={btn} disabled={dis} title={tip} onClick={() => propose({ kind: "merge", keep: String(it.note), drop: String(it.other) })}>Merge…</button>;
  if (f.action === "rename")
    return <button className={btn} disabled={dis} title={tip} onClick={() => propose({ kind: "rename", note: String(it.note), new_name: String(it.name || "") })}>
      Rename to {String(it.expected || it.name)}</button>;
  if (f.action === "close")
    return <button className={btn} disabled={dis} title={tip} onClick={() => propose({ kind: "close", note: String(it.note) })}>Close the project</button>;
  return null;
}

function itemText(f: { id: string }, it: CxItem): string {
  switch (f.id) {
    case "broken_links": return `[[${it.target}]]${it.suggestion ? ` — probably “${it.suggestion}”` : " — no obvious replacement"}`;
    case "near_duplicates": return `≈ ${it.other} (${Math.round(Number(it.cosine) * 100)} % similar)`;
    case "naming": return `file ${it.file} → ${it.expected}`;
    case "stale_open": return `${it.age_days} days old, still says “${it.marker}”`;
    case "secrets": return `${it.kind}, ${Number(it.line) === 0 ? "in the description" : `line ${it.line}`} (value hidden)`;
    case "injection": return `line ${it.line}`;
    case "dead_paths": return String(it.path);
    case "description_drift": return String(it.message || "");
    case "giant_notes": return `${it.words} words`;
    case "frontmatter": return String(it.problem || "");
    case "index_desync": return String(it.state || "");
    default: return "";
  }
}

// ------------------------------------------------------------------ note panel

function NotePanel({ note, ghost, sel, openNote, canAct, busy, propose, curate }: {
  note: CxNote | null; ghost: { label: string; citedBy: string[] } | null; sel: string | null;
  openNote: (id: string) => void; canAct: boolean; busy: string;
  propose: (p: CxProposalIn) => void; curate: (ids: string[], notes?: string[]) => void;
}) {
  const md = useMemo(() => (note?.body || "").replace(/\[\[([^\]|#]+)(?:[|#]([^\]]*))?\]\]/g,
    (_m, t: string, label?: string) => `[${(label || t).trim()}](#note:${encodeURIComponent(t.trim())})`), [note]);
  if (ghost) return (
    <div>
      <div className="font-mono text-[15px] text-[#ff8a96]">[[{ghost.label}]]</div>
      <div className="mt-2 rounded-lg border border-[#ffb454]/40 bg-[#ffb454]/5 p-2.5 text-[12.5px] text-slate-200">
        <b className="block text-[#ffc98a]">Missing note</b>
        No note has this name. Either the subject was handled without writing a note, or the note was renamed:
        point the link to the right note (Health tab) or write the missing note.
      </div>
      <div className="mt-3 text-[11px] uppercase tracking-wide text-mut">cited by</div>
      {ghost.citedBy.map((s) => <button key={s} onClick={() => openNote(s)} className="block py-0.5 text-[12.5px] text-sky-300 hover:underline">{s}</button>)}
    </div>
  );
  if (!sel) return <div className="mt-10 text-center text-[13px] text-mut">Click a note in the graph, or ask the memory a question.</div>;
  if (!note) return <div className="mt-10 text-center text-[13px] text-mut">loading…</div>;
  const approx = note.date_source && !["frontmatter", "indexed", "transcript"].includes(note.date_source);
  const flags = note.flags ?? [], cites = note.in ?? [], cited = note.out ?? [];
  return (
    <article>
      <h2 className="font-mono text-[15px] font-semibold text-slate-100">{note.priority && <span className="text-brass" title="pinned: boosted at recall">★ </span>}{note.id}</h2>
      <div className="mt-1.5 flex flex-wrap gap-1 text-[10.5px]">
        <NoteLevel name={note.id} level={note.classification} />
        <span className="rounded-full border border-line px-2 py-0.5" style={{ color: TYPE_COLORS[note.type] || TYPE_COLORS.unknown }}>{note.type === "feedback" ? "rule" : note.type}</span>
        <span className="rounded-full border border-line px-2 py-0.5 text-slate-300">{fmt(note.words)} words</span>
        <span className={`rounded-full border px-2 py-0.5 ${note.indexed ? "border-line text-slate-300" : "border-[#ffb454]/50 text-[#ffc98a]"}`}>
          {note.indexed ? `${note.chunks} passage${note.chunks === 1 ? "" : "s"} indexed` : "not indexed yet"}</span>
        <span title={`date source: ${note.date_source}`} className={`rounded-full border px-2 py-0.5 ${approx || !note.modified ? "border-[#ffb454]/50 text-[#ffc98a]" : "border-line text-slate-300"}`}>
          {note.modified ? `updated ${note.modified} · ${note.age} d${approx ? " (estimated)" : ""}` : "no date"}</span>
        <span className="rounded-full border border-line px-2 py-0.5 text-mut">{note.file}</span>
      </div>
      {note.desc ? <p className="mt-2 text-[13px] text-slate-200">{note.desc}</p>
        : <div className="mt-2 rounded-lg border border-[#ff5c6c]/50 bg-[#ff5c6c]/5 p-2 text-[12px] text-[#ff8a96]"><b>No description.</b> This note is silent in the index.</div>}
      {flags.length > 0 && (
        <div className="mt-3 space-y-1.5">
          {flags.map((f) => (
            <div key={f.id} className={`rounded-lg border ${sev(f.severity).ring} bg-black/20 p-2 text-[12px]`}>
              <div className="flex items-center gap-1.5"><i className={`h-1.5 w-1.5 rounded-full ${sev(f.severity).dot}`} />
                <b className={sev(f.severity).text}>{f.title}</b></div>
              {(f.items ?? []).map((it, i) => <div key={i} className="mt-0.5 text-slate-300">{itemText(f, it)}</div>)}
              <div className="mt-1 text-mut">{f.remedy}</div>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {(f.items ?? []).map((it, i) => <ItemActions key={i} f={f} it={it} canAct={canAct} busy={busy} propose={propose} />)}
                {f.judgement && <button disabled={!canAct || !!busy} onClick={() => curate([f.id], [note.id])}
                  className="rounded-md border border-violet-400/40 bg-violet-400/10 px-2 py-0.5 text-[11px] text-violet-200 hover:bg-violet-400/20 disabled:opacity-40">
                  Ask a curation session</button>}
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="mt-3 grid grid-cols-2 gap-2 text-[12px]">
        <div><div className="text-[10.5px] uppercase tracking-wide text-mut">{cites.length} cite it</div>
          {cites.map((x) => <button key={x} onClick={() => openNote(x)} className="block truncate text-sky-300 hover:underline">{x}</button>)}</div>
        <div><div className="text-[10.5px] uppercase tracking-wide text-mut">it cites {cited.length}</div>
          {cited.map((x) => x.resolved
            ? <button key={x.target} onClick={() => openNote(x.resolved!)} className="block truncate text-sky-300 hover:underline">{x.target}</button>
            : <span key={x.target} className="block truncate text-[#ff8a96] line-through" title="missing note">{x.target}</span>)}</div>
      </div>
      <div className="md mt-3 border-t border-line pt-2 text-slate-200">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
          a: ({ href, children }) => href?.startsWith("#note:")
            ? <a href="#" onClick={(e) => { e.preventDefault(); openNote(decodeURIComponent(href.slice(6))); }}>{children}</a>
            : <a href={href} target="_blank" rel="noreferrer">{children}</a>,
        }}>{md}</ReactMarkdown>
      </div>
    </article>
  );
}

// ------------------------------------------------------------------ health panel

function Spark({ hist }: { hist: CxOverview["history"] | undefined }) {
  if (!hist || hist.length < 2) return <div className="text-[11px] text-mut">The curve draws itself as the reviews go (one per hour).</div>;
  const w = 420, h = 54, xs = (i: number) => 2 + (i * (w - 4)) / (hist.length - 1), ys = (v: number) => h - 3 - (v / 100) * (h - 6);
  const line = hist.map((p, i) => `${i ? "L" : "M"}${xs(i).toFixed(1)},${ys(p.score).toFixed(1)}`).join("");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-14 w-full" preserveAspectRatio="none">
      <path d={`${line}L${xs(hist.length - 1)},${h}L2,${h}Z`} fill="rgba(212,167,74,.15)" />
      <path d={line} fill="none" stroke="#f0d38a" strokeWidth="1.5" />
      <circle cx={xs(hist.length - 1)} cy={ys(hist[hist.length - 1].score)} r="3" fill="#fff" />
    </svg>
  );
}

function HealthPanel({ ov, canAct, busy, rerun, openNote, light, propose, curate }: {
  ov: CxOverview | null; canAct: boolean; busy: string; rerun: () => void; openNote: (id: string) => void;
  light: (names: string[]) => void; propose: (p: CxProposalIn) => void; curate: (ids: string[]) => void;
}) {
  if (!ov) return <div className="mt-10 text-center text-[13px] text-mut">loading…</div>;
  const r = safeReport(ov), s = ov.summary || {};
  const cls = r.score == null ? "text-mut" : r.score >= 80 ? "text-[#3ecfb2]" : r.score >= 55 ? "text-[#ffb454]" : "text-[#ff5c6c]";
  const cats = [...new Set(r.findings.map((f) => f.category))];
  const judgement = r.findings.filter((f) => f.judgement).map((f) => f.id);
  return (
    <div>
      <div className="flex items-end gap-3">
        <span className={`font-mono text-[44px] font-semibold leading-none ${cls}`}>{r.score ?? "—"}</span>
        <span className="pb-1 text-[11.5px] leading-snug text-mut">memory health · {r.notes_total} notes<br />
          {r.at ? <>last review {when(r.at)} · {r.duration_ms} ms{r.source ? ` · ${r.source}` : ""}</>
            : ov.per_project === "off" ? "no review of its own for this project (per-project review off, or the 2.x index)"
            : "no review yet"}</span>
      </div>
      <Spark hist={ov.history} />
      <div className="mt-1 grid grid-cols-3 gap-1.5 text-center">
        {[["problems open", s.open], ["open > 7 days", s.open_over_7d], ["mean time to fix", s.mean_fix_hours != null ? `${s.mean_fix_hours} h` : "—"]].map(([k, v]) => (
          <div key={k as string} className="rounded-lg border border-line bg-black/20 py-1.5">
            <div className="font-mono text-[15px] text-slate-100">{v ?? "—"}</div>
            <div className="text-[10px] text-mut">{k}</div>
          </div>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button onClick={rerun} disabled={!canAct || !!busy} className="rounded-md border border-line bg-panel2 px-2.5 py-1 text-[12px] text-slate-200 hover:bg-line disabled:opacity-40">
          {busy === "review" ? "reviewing…" : "Review now"}</button>
        {judgement.length > 0 && <button onClick={() => curate(judgement)} disabled={!canAct || !!busy}
          className="rounded-md border border-violet-400/40 bg-violet-400/10 px-2.5 py-1 text-[12px] text-violet-200 hover:bg-violet-400/20 disabled:opacity-40">
          Curation session for the {judgement.length} judgement case{judgement.length > 1 ? "s" : ""}</button>}
        <span className="text-[11px] text-mut">{ov.notify ? "alerts on: a daily digest when something changes, right away if critical"
          : "alerts off — add a channel in Setup › Notifications"}</span>
      </div>
      {r.skipped.length > 0 && <div className="mt-1.5 text-[11px] text-mut">Not checked: {r.skipped.join(", ")}</div>}
      {r.findings.length === 0 && r.at && <div className="mt-8 text-center text-[13px] text-[#3ecfb2]">Nothing to report. The memory is clean.</div>}
      {cats.map((cat) => (
        <section key={cat} className="mt-4">
          <h3 className="mb-1.5 text-[11px] uppercase tracking-wide text-mut">{CATEGORY[cat] || cat}</h3>
          {r.findings.filter((f) => f.category === cat).map((f) => (
            <details key={f.id} open={f.severity === "crit"} className={`mb-1.5 rounded-lg border ${sev(f.severity).ring} bg-black/20`}>
              <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 text-[12.5px]">
                <i className={`h-2 w-2 shrink-0 rounded-full ${sev(f.severity).dot}`} />
                <span className="flex-1 text-slate-100">{f.title}</span>
                <span className={`text-[10.5px] ${sev(f.severity).text}`}>{sev(f.severity).label}</span>
                {f.count > 0 && <span className="rounded bg-panel2 px-1.5 font-mono text-[10.5px] text-slate-300">{f.count}</span>}
              </summary>
              <div className="space-y-1.5 px-2.5 pb-2.5 text-[12px]">
                <p className="text-slate-300">{f.detail}</p>
                <p className="rounded bg-sea/5 px-2 py-1 text-sky-200/90">{f.remedy}</p>
                {(f.notes ?? []).length > 0 && <button onClick={() => light(f.notes)} className="text-[11px] text-sea hover:underline">light up these notes in the graph</button>}
                <div className="space-y-1">
                  {(f.items?.length ? f.items : (f.notes ?? []).map((n): CxItem => ({ note: n }))).slice(0, 40).map((it: CxItem, i: number) => (
                    <div key={i} className="flex flex-wrap items-center gap-1.5">
                      {it.note && <button onClick={() => openNote(String(it.note))} className="font-mono text-[11.5px] text-sky-300 hover:underline">{String(it.note)}</button>}
                      <span className="text-[11.5px] text-mut">{itemText(f, it)}</span>
                      <ItemActions f={f} it={it} canAct={canAct} busy={busy} propose={propose} />
                    </div>
                  ))}
                  {(f.items ?? []).length > 40 && <div className="text-[11px] text-mut">… and {f.items.length - 40} more</div>}
                </div>
                {f.judgement && <button onClick={() => curate([f.id])} disabled={!canAct || !!busy}
                  className="rounded-md border border-violet-400/40 bg-violet-400/10 px-2 py-0.5 text-[11px] text-violet-200 hover:bg-violet-400/20 disabled:opacity-40">
                  Ask a curation session</button>}
              </div>
            </details>
          ))}
        </section>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ repairs

const STATUS: Record<string, string> = {
  pending: "waiting for approval", applied: "applied", refused: "refused",
  conflict: "not applied — the note changed meanwhile", failed: "failed",
};

function RepairsPanel({ props, open }: { props: CxProposal[]; open: (p: CxProposal) => void }) {
  if (!props.length) return <div className="mt-10 text-center text-[13px] text-mut">No repair yet. They start from the Health tab: every one is shown before anything is written.</div>;
  return (
    <div className="space-y-1.5">
      {props.map((p) => (
        <button key={p.id} onClick={() => open(p)}
          className={`block w-full rounded-lg border px-2.5 py-2 text-left hover:bg-panel2 ${p.status === "pending" ? "border-brass/50 bg-brass/5" : "border-line"}`}>
          <div className="text-[12.5px] text-slate-100">{p.title}</div>
          <div className="text-[11px] text-mut">{STATUS[p.status] || p.status} · {p.files} file{p.files > 1 ? "s" : ""} · {p.created_by}
            {p.decided_by ? ` → ${p.decided_by}` : ""} · {new Date(p.created_at * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}</div>
        </button>
      ))}
    </div>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="max-h-[38vh] overflow-auto rounded-lg border border-line bg-[#0b0f16] p-2 font-mono text-[11px] leading-[1.45]">
      {text.split("\n").map((l, i) => (
        <div key={i} className={l.startsWith("+++") || l.startsWith("---") ? "text-mut"
          : l.startsWith("+") ? "bg-emerald-500/10 text-emerald-200" : l.startsWith("-") ? "bg-[#ff5c6c]/10 text-[#ffb3ba]"
          : l.startsWith("@@") ? "text-sky-400/80" : "text-slate-400"}>{l || " "}</div>
      ))}
    </pre>
  );
}

function ProposalModal({ p, canAct, busy, close, decide, swap }: {
  p: CxProposal; canAct: boolean; busy: string; close: () => void;
  decide: (p: CxProposal, approve: boolean) => void; swap?: () => void;
}) {
  const pending = p.status === "pending";
  return (
    <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/60 p-3" onClick={close}>
      <div onClick={(e) => e.stopPropagation()} className="flex max-h-[92%] w-full max-w-3xl flex-col rounded-xl border border-brass/40 bg-panel shadow-2xl">
        <div className="border-b border-line px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-brass">{pending ? "Approval needed" : STATUS[p.status]}</div>
          <div className="text-[15px] font-semibold text-slate-100">{p.title}</div>
          <div className="mt-0.5 text-[12.5px] text-slate-300">{p.summary}</div>
          {p.error && <div className="mt-1 text-[12px] text-[#ff8a96]">{p.error}</div>}
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
          {p.changes.map((c) => (
            <div key={c.path}>
              <div className="mb-1 text-[12px] text-slate-300">
                <span className="font-mono">{c.path}</span>{" "}
                <span className="text-mut">{c.before === null ? "· new file" : c.after === null ? "· removed (a copy is kept)" : "· modified"}</span>
              </div>
              <Diff text={c.diff} />
            </div>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
          {pending ? (<>
            <button disabled={!canAct || !!busy} onClick={() => decide(p, true)}
              className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-[12.5px] text-emerald-100 hover:bg-emerald-500/20 disabled:opacity-40">
              {busy === "approve" ? "applying…" : "Approve and apply"}</button>
            <button disabled={!canAct || !!busy} onClick={() => decide(p, false)}
              className="rounded-md border border-[#ff5c6c]/40 bg-[#ff5c6c]/10 px-3 py-1.5 text-[12.5px] text-[#ffb3ba] hover:bg-[#ff5c6c]/20 disabled:opacity-40">
              Refuse</button>
            {swap && <button disabled={!canAct || !!busy} onClick={swap} className="rounded-md border border-line px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-panel2 disabled:opacity-40">
              Keep the other note instead</button>}
            {!canAct && <span className="text-[11.5px] text-mut">you need the developer role to approve</span>}
          </>) : null}
          <button onClick={close} className="ml-auto rounded-md px-3 py-1.5 text-[12.5px] text-mut hover:text-slate-200">{pending ? "Later" : "Close"}</button>
        </div>
      </div>
    </div>
  );
}


/** 3.4 — the note's classification, and (dev+) reclassify it: raising is free, lowering
 *  needs a maintainer cleared for the current level and a reason (journaled). */
function NoteLevel({ name, level }: { name: string; level?: string }) {
  return <LevelControl key={name} level={level} onSet={(v, reason) => setNoteLevel(name, v, reason)} />;
}
