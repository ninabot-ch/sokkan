"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { patchCard } from "@/lib/api";
import {
  helmActivity, helmApprove, helmBaseline, helmCard, helmCosts, helmDeck, helmFilters, helmIgnore, helmRefresh,
  STATE_META, SUGGESTION_LABEL,
  type DeckItem, type HelmCosts, type HelmDeck, type HelmDetail, type HelmState, type KanbanCard, type Rollup, type Suggestion,
} from "@/lib/helm";
import BoardColumns from "./BoardColumns";
import CardModal from "./CardModal";

// « Helm » (3.3) — the management view. Every project card of the teams a manager steers,
// in a deck with Crew's grammar: one card each, state = colour + label, the card breathes
// while sessions or agents work under it, click = popout (Kanban · Activity · Suggestions
// · Costs). The state is COMPUTED from the cards below (never declared). Spec: docs/HELM.md.

const COLUMNS: HelmState[] = ["todo", "in_progress", "waiting", "blocked", "done"];
const EMPTY: Record<HelmState, string> = {
  todo: "Nothing waiting to start.",
  in_progress: "Nothing moving right now.",
  waiting: "Nothing waits for an approval.",
  blocked: "Nothing blocked. 🎉",
  done: "No project finished yet.",
};
const usd = (v: number) => (!v ? "$0" : v < 0.01 ? "<$0.01" : `$${v.toFixed(2)}`);
const ago = (ts: number | null | undefined) => {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`;
};

const tone = (state: HelmState) => ({ ["--c" as string]: STATE_META[state].color }) as React.CSSProperties;

function StatePill({ state, small }: { state: HelmState; small?: boolean }) {
  const m = STATE_META[state];
  return (
    <span style={tone(state)} title={m.hint}
      className={`crew-fg crew-bd crew-bg inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 ${small ? "text-[9.5px]" : "text-[10.5px]"} font-semibold uppercase tracking-wide`}>
      <span aria-hidden>{m.icon}</span>{small ? m.short : m.label}
    </span>
  );
}

function Progress({ r }: { r: Rollup }) {
  const pct = Math.round((r.progress || 0) * 100);
  return (
    <div className="flex items-center gap-2" title={`${r.counts.done}/${r.total} done`}>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-700/60" role="progressbar" aria-label="progress (cards done)" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div className="crew-dot h-full rounded-full" style={{ ...tone("done"), width: `${pct}%` }} />
      </div>
      <span className="text-[10.5px] tabular-nums text-mut">{r.counts.done}/{r.total}</span>
    </div>
  );
}

function Signals({ r }: { r: Rollup }) {
  const s = r.signals;
  if (!s) return null;
  return (
    <div className="flex flex-wrap items-center gap-1 text-[10.5px]">
      {s.sessions_working > 0 && <span className="rounded bg-panel px-1.5 py-px text-slate-200" title="sessions working now">🖥 {s.sessions_working} working</span>}
      {s.runs_active > 0 && <span className="rounded bg-panel px-1.5 py-px text-slate-200" title="agent runs going">⚙ {s.runs_active} run{s.runs_active > 1 ? "s" : ""}</span>}
      {s.mrs.length > 0 && <span className="rounded bg-panel px-1.5 py-px text-slate-300" title="merge requests linked">⇄ {s.mrs.length} MR</span>}
      {s.incidents.length > 0 && <span style={tone("blocked")} className="crew-fg crew-bd rounded border px-1.5 py-px" title={s.incidents.map((i) => `#${i.id} ${i.title}`).join("\n")}>⚠ {s.incidents.length} incident{s.incidents.length > 1 ? "s" : ""}</span>}
      {s.runs_failed > 0 && <span style={tone("blocked")} className="crew-fg rounded px-1.5 py-px">✕ {s.runs_failed} failed run{s.runs_failed > 1 ? "s" : ""}</span>}
    </div>
  );
}

// ---- deck -------------------------------------------------------------------
// 3.2.2 Captains demo: a member who does not steer reads Helm — every action is greyed
const ReadOnly = createContext(false);
const RO_TIP = "read-only demo";

export default function Helm({ onOpenSession, readOnly = false }: { onOpenSession?: (sid: string) => void; readOnly?: boolean }) {
  const [deck, setDeck] = useState<HelmDeck | null>(null);
  const [err, setErr] = useState("");
  const [filters, setFilters] = useState<{ projects: { slug: string; name: string }[]; teams: string[]; people: string[] } | null>(null);
  const [fProject, setFProject] = useState("");
  const [fTeam, setFTeam] = useState("");
  const [fPerson, setFPerson] = useState("");
  const [openId, setOpenId] = useState<number | null>(null);

  const reload = useCallback(() => {
    helmDeck({ project: fProject, team: fTeam, person: fPerson })
      .then((d) => { setDeck(d); setErr(""); })
      .catch((e) => setErr(String(e.message || e)));
  }, [fProject, fTeam, fPerson]);
  useEffect(() => { reload(); const iv = setInterval(reload, 5000); return () => clearInterval(iv); }, [reload]);
  useEffect(() => { helmFilters().then(setFilters).catch(() => {}); }, []);
  useEffect(() => {  // deep link /?tab=helm&card=12
    const c = new URLSearchParams(window.location.search).get("card");
    if (c && /^\d+$/.test(c)) setOpenId(+c);
  }, []);

  const byCol = useMemo(() => {
    const m = Object.fromEntries(COLUMNS.map((c) => [c, [] as DeckItem[]])) as Record<HelmState, DeckItem[]>;
    for (const it of deck?.items || []) m[it.rollup.state]?.push(it);
    return m;
  }, [deck]);
  const sel = "rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200";

  return (
    <ReadOnly.Provider value={readOnly}>
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel/60 px-3 py-2">
        <div className="mr-2">
          <div className="flex items-center gap-2 text-[14px] font-semibold text-slate-100">Helm
            {readOnly && <span title="You see Helm as a project member: steering actions belong to the project's managers" className="rounded-full border border-line bg-panel2 px-2 py-0.5 text-[10.5px] font-normal text-mut">👁 {RO_TIP}</span>}</div>
          <div className="text-[10.5px] text-mut">every project of your teams — progress computed from the work, never declared</div>
        </div>
        <label className="flex items-center gap-1 text-[11px] text-mut">project
          <select value={fProject} onChange={(e) => setFProject(e.target.value)} className={sel}>
            <option value="">all</option>
            {filters?.projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1 text-[11px] text-mut">team
          <select value={fTeam} onChange={(e) => setFTeam(e.target.value)} className={sel}>
            <option value="">all</option>
            {filters?.teams.map((t) => <option key={t} value={t}>{t.replace(/^sso:/, "")}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1 text-[11px] text-mut">person
          <select value={fPerson} onChange={(e) => setFPerson(e.target.value)} className={sel}>
            <option value="">all</option>
            {filters?.people.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        {(deck?.project_suggestions.length || 0) > 0 && (
          <span className="ml-auto rounded-lg border border-brass/40 bg-brass/10 px-2 py-1 text-[11.5px] text-brass" title={deck!.project_suggestions.map((s) => s.title).join("\n")}>
            {deck!.project_suggestions.length} project-level suggestion(s): {deck!.project_suggestions[0].title}
          </span>
        )}
      </div>
      {err && <div className="m-3 rounded border border-red-500/40 bg-red-500/10 p-2 text-[12px] text-red-300">{err}</div>}

      <div className="grid min-h-0 flex-1 auto-cols-[minmax(240px,1fr)] grid-flow-col gap-3 overflow-x-auto p-3">
        {COLUMNS.map((col) => (
          <section key={col} style={tone(col)} className="flex min-h-0 flex-col rounded-xl border border-line bg-panel/50" aria-label={`${STATE_META[col].label} projects`}>
            <header className="flex items-center gap-2 border-b border-line px-3 py-2" title={STATE_META[col].hint}>
              <span className="crew-fg text-[12.5px] font-semibold"><span aria-hidden>{STATE_META[col].icon}</span> {STATE_META[col].label}</span>
              <span className="ml-auto rounded-full bg-panel2 px-1.5 text-[10.5px] text-mut">{byCol[col].length}</span>
            </header>
            <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
              {byCol[col].length === 0 && <div className="px-1 py-6 text-center text-[11px] text-mut/70">{EMPTY[col]}</div>}
              {byCol[col].map((it) => <ProjectCard key={it.card.id} it={it} onOpen={() => setOpenId(it.card.id)} />)}
            </div>
          </section>
        ))}
      </div>
      {deck && deck.items.length === 0 && (
        <div className="mx-auto mb-10 max-w-lg rounded-xl border border-line bg-panel2/40 p-4 text-center text-[12.5px] text-mut">
          No project card yet. Ask Nina « create a project »: she interviews you (goal, scope, constraints,
          deadline, team), builds the project card and proposes the cards under it — you edit, then validate.
        </div>
      )}
      {openId !== null && <HelmPopout id={openId} onClose={() => { setOpenId(null); reload(); }} onOpenSession={onOpenSession} />}
    </div>
    </ReadOnly.Provider>
  );
}

function ProjectCard({ it, onOpen }: { it: DeckItem; onOpen: () => void }) {
  const r = it.rollup;
  return (
    <button onClick={onOpen} style={tone(r.state)}
      className={`crew-card ${it.breathing ? "crew-breathe" : ""} block w-full rounded-lg border border-line bg-panel2/70 p-2.5 text-left transition hover:bg-panel2 focus:outline-none focus-visible:ring-2 focus-visible:ring-sea`}
      aria-label={`${it.card.title}, ${r.label}${it.breathing ? ", work going on" : ""}`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="line-clamp-2 text-[13px] font-semibold leading-snug text-slate-100">{it.card.title}</div>
          <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-mut">{it.card.intent || it.card.description}</div>
        </div>
        <StatePill state={r.state} small />
      </div>
      <div className="mt-2"><Progress r={r} /></div>
      <div className="mt-1.5"><Signals r={r} /></div>
      <div className="mt-2 flex items-center gap-2 border-t border-line/60 pt-1.5 text-[10.5px] text-mut">
        {it.breathing ? (
          <span className="crew-fg flex items-center gap-1 font-medium"><span className="crew-dot crew-live-dot h-1.5 w-1.5 rounded-full" aria-hidden />work going on</span>
        ) : <span>{r.descendants} card{r.descendants === 1 ? "" : "s"} under it</span>}
        {it.suggestions > 0 && <span className="ml-auto rounded border border-brass/50 bg-brass/10 px-1.5 font-medium text-brass">{it.suggestions} suggestion{it.suggestions > 1 ? "s" : ""}</span>}
      </div>
      <div className="mt-1 flex items-center gap-2 text-[10px] text-mut/70">
        <span className="truncate" title={it.people.join(", ")}>{it.card.project}{it.people.length === 1 ? ` · ${it.people[0].split("@")[0]}` : it.people.length ? ` · ${it.people.length} people` : ""}</span>
        {it.card.due && <span className="ml-auto shrink-0">due {it.card.due}</span>}
      </div>
    </button>
  );
}

// ---- popout -----------------------------------------------------------------
type PopTab = "Kanban" | "Activity" | "Suggestions" | "Costs";

function HelmPopout({ id, onClose, onOpenSession }: { id: number; onClose: () => void; onOpenSession?: (sid: string) => void }) {
  const ro = useContext(ReadOnly);
  const [cur, setCur] = useState(id);
  const [d, setD] = useState<HelmDetail | null>(null);
  const [tab, setTab] = useState<PopTab>("Kanban");
  const [msg, setMsg] = useState("");
  const [cardModal, setCardModal] = useState<number | null>(null);

  const load = useCallback(() => {
    helmCard(cur).then((x) => { setD(x); setMsg(""); }).catch((e) => setMsg(String(e.message || e)));
  }, [cur]);
  useEffect(() => { load(); const iv = setInterval(load, 4000); return () => clearInterval(iv); }, [load]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape" && cardModal === null) onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose, cardModal]);

  const state: HelmState = d?.rollup?.state || "todo";
  const breathing = !!d?.rollup?.working;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/55 p-2 pt-[5vh] md:p-6" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-label={d ? `Project card ${d.title}` : "Project card"} style={tone(state)}
        className={`crew-card flex max-h-[90vh] w-full max-w-6xl flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-2xl ${breathing ? "crew-breathe" : ""}`}>
        <header className="border-b border-line px-4 py-3">
          <nav aria-label="breadcrumb" className="mb-1 flex flex-wrap items-center gap-1 text-[11px] text-mut">
            <button onClick={onClose} className="hover:text-slate-200">Helm</button>
            {(d?.breadcrumb || []).map((b) => (
              <span key={b.id} className="flex items-center gap-1"><span aria-hidden>›</span>
                <button onClick={() => setCur(b.id)} className="text-sea hover:underline">#{b.id} {b.title}</button>
              </span>
            ))}
            {d && <span className="flex items-center gap-1"><span aria-hidden>›</span><span className="text-slate-300">#{d.id}</span></span>}
          </nav>
          <div className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="min-w-0 break-words text-[16px] font-semibold leading-snug text-slate-100">{d?.title || "…"}</span>
                {d && <StatePill state={state} />}
                {breathing && <span className="crew-fg flex items-center gap-1 text-[11px] font-medium"><span className="crew-dot crew-live-dot h-1.5 w-1.5 rounded-full" aria-hidden />work going on</span>}
              </div>
              {d && <div className="mt-1 max-w-md"><Progress r={d.rollup} /></div>}
            </div>
            {d && <button onClick={() => setCardModal(d.id)} className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut hover:text-slate-200">{ro ? "Open card" : "Edit card"}</button>}
            <button onClick={onClose} className="rounded-md px-2 py-1 text-mut hover:bg-panel2 hover:text-slate-200" aria-label="close">✕</button>
          </div>
        </header>
        <nav className="flex gap-1 border-b border-line px-3 pt-2" role="tablist">
          {(["Kanban", "Activity", "Suggestions", "Costs"] as PopTab[]).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
              className={`rounded-t-md px-3 py-1.5 text-[12.5px] ${tab === t ? "bg-panel2 text-slate-100 ring-1 ring-line" : "text-mut hover:text-slate-200"}`}>
              {t}{t === "Suggestions" && d?.suggestions.length ? <span className="ml-1.5 rounded-full bg-brass/20 px-1.5 text-[10px] text-brass">{d.suggestions.length}</span> : null}
            </button>
          ))}
        </nav>
        {msg && <div className="mx-4 mt-2 rounded border border-red-500/40 bg-red-500/10 px-2 py-1 text-[12px] text-red-300">{msg}</div>}
        <div className="min-h-0 flex-1 overflow-y-auto">
          {d && tab === "Kanban" && <KanbanTab d={d} onDrill={setCur} onOpenCard={setCardModal} onOpenSession={onOpenSession} onChanged={load} />}
          {d && tab === "Activity" && <ActivityTab id={d.id} />}
          {d && tab === "Suggestions" && <SuggestionsTab d={d} onChanged={load} onError={setMsg} />}
          {d && tab === "Costs" && <CostsTab id={d.id} />}
        </div>
      </div>
      {cardModal !== null && (
        <CardModal cardId={cardModal} tags={[]} onClose={() => setCardModal(null)}
          onOpenSession={(sid) => { setCardModal(null); onOpenSession?.(sid); }} onChanged={load} />
      )}
    </div>
  );
}

function KanbanTab({ d, onDrill, onOpenCard, onOpenSession, onChanged }: {
  d: HelmDetail; onDrill: (id: number) => void; onOpenCard: (id: number) => void;
  onOpenSession?: (sid: string) => void; onChanged: () => void;
}) {
  const ro = useContext(ReadOnly);
  const move = async (id: number, bucket: string, sort: number) => { await patchCard(id, { bucket, sort }); onChanged(); };
  const ctx = d.intent || d.constraints || (d.decisions || []).length;
  return (
    <div className="flex flex-col gap-3 p-3">
      {ctx ? (
        <div className="grid gap-2 rounded-lg border border-line bg-panel2/30 p-3 text-[12px] md:grid-cols-3">
          <div><div className="text-[10.5px] uppercase tracking-wide text-mut">intent</div><div className="text-slate-200">{d.intent || "—"}</div></div>
          <div><div className="text-[10.5px] uppercase tracking-wide text-mut">constraints</div><div className="text-slate-200">{d.constraints || "—"}</div></div>
          <div><div className="text-[10.5px] uppercase tracking-wide text-mut">decisions</div>
            <ul className="list-disc pl-4 text-slate-200">{(d.decisions || []).map((x, i) => <li key={i}>{x}</li>)}</ul></div>
          <div className="text-[10.5px] text-mut md:col-span-3">Flows down to every session of the cards below{d.context_note ? <> · memory note <code>{d.context_note}</code> (card:{d.id})</> : null}.</div>
        </div>
      ) : null}
      <div className="flex min-h-[46vh]">
        <BoardColumns<KanbanCard>
          buckets={d.kanban.buckets} cards={d.kanban.cards} canWrite={!ro} onMove={ro ? undefined : move} compact
          onOpen={(cid) => onOpenCard(cid)} onOpenSession={onOpenSession}
          extra={(c) => (
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
              <StatePill state={c.rollup.state} small />
              {c.rollup.children > 0 && (
                <button onClick={() => onDrill(c.id)} className="rounded border border-sea/40 px-1.5 text-[10.5px] text-sea hover:bg-sea/10">
                  open its board · {c.rollup.counts.done}/{c.rollup.total}
                </button>
              )}
              {c.rollup.working > 0 && <span style={tone("in_progress")} className="crew-fg text-[10.5px]">● working</span>}
            </div>
          )}
        />
      </div>
    </div>
  );
}

function ActivityTab({ id }: { id: number }) {
  const [ev, setEv] = useState<Awaited<ReturnType<typeof helmActivity>> | null>(null);
  useEffect(() => { helmActivity(id).then(setEv).catch(() => setEv([])); }, [id]);
  if (!ev) return <div className="p-4 text-[12px] text-mut">Loading…</div>;
  if (!ev.length) return <div className="p-4 text-[12px] text-mut">No activity yet.</div>;
  return (
    <ol className="divide-y divide-line/60 p-2">
      {ev.map((e, i) => (
        <li key={i} className="flex gap-3 px-2 py-1.5 text-[12px]">
          <span className="w-24 shrink-0 text-mut" title={new Date(e.ts * 1000).toLocaleString()}>{ago(e.ts)}</span>
          <span className="w-40 shrink-0 truncate text-slate-300" title={e.card_title}>#{e.card_id} {e.card_title}</span>
          <span className="shrink-0 font-medium text-slate-200">{e.action}</span>
          <span className="min-w-0 flex-1 truncate text-mut" title={e.detail}>{e.detail}</span>
          <span className="shrink-0 text-[11px] text-mut/80">{e.user}{e.via ? ` · ${e.via}` : ""}</span>
        </li>
      ))}
    </ol>
  );
}

function SuggestionsTab({ d, onChanged, onError }: { d: HelmDetail; onChanged: () => void; onError: (m: string) => void }) {
  const ro = useContext(ReadOnly);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const off = busy || ro;
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); onError("");
    try { await fn(); onChanged(); } catch (e) { onError(String((e as Error).message)); } finally { setBusy(false); }
  };
  return (
    <div className="space-y-2 p-3">
      <div className="flex flex-wrap items-center gap-2 text-[11.5px] text-mut">
        <span>Helm checks this project every 15 min: drift from the goal, decisions contradicted, scope growth, cards without owner, pace, incidents. Nothing is applied without you.</span>
        <button disabled={off} title={ro ? RO_TIP : undefined} onClick={() => act(async () => { const r = await helmRefresh(d.id); setNote(`${r.new} new, ${r.resolved} resolved${r.notes.length ? ` — ${r.notes.join("; ")}` : ""}`); })}
          className="ml-auto rounded border border-line px-2 py-0.5 text-slate-300 hover:border-sea/50 disabled:opacity-40">↻ Check now</button>
      </div>
      {note && <div className="text-[11px] text-mut">{note}</div>}
      {d.suggestions.length === 0 && <div className="rounded-lg border border-line bg-panel2/40 p-4 text-[12.5px] text-mut">No reframe to suggest. 🎯</div>}
      {d.suggestions.map((s: Suggestion) => (
        <article key={s.id} className="rounded-lg border border-brass/40 bg-brass/5 p-3" aria-label={`suggestion: ${s.title}`}>
          <div className="flex items-start gap-2">
            <span className="rounded border border-brass/50 bg-brass/10 px-1.5 text-[10px] font-semibold uppercase tracking-wide text-brass">reframe · {SUGGESTION_LABEL[s.kind]}</span>
            <span className="ml-auto text-[10.5px] text-mut">{ago(s.created_at)}</span>
          </div>
          <div className="mt-1.5 text-[13px] font-medium text-slate-100">{s.title}</div>
          <div className="mt-0.5 text-[12px] text-slate-300">{s.detail}</div>
          <div className="mt-2 flex items-center gap-2">
            <button disabled={off} onClick={() => act(() => helmApprove(s.id))} className="rounded-md bg-brass/90 px-2.5 py-1 text-[12px] font-semibold text-ink hover:bg-brass disabled:opacity-40" title={ro ? RO_TIP : "creates a « reframe » card assigned to you; the work itself is not touched"}>Approve</button>
            <button disabled={off} onClick={() => act(() => helmIgnore(s.id))} className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut hover:text-slate-200 disabled:opacity-40" title={ro ? RO_TIP : "not proposed again for 7 days"}>Ignore</button>
            {ro && <span className="text-[11px] text-mut">{RO_TIP} — a project manager approves or ignores</span>}
            {s.kind === "scope" && s.card_id && (
              <button disabled={off} title={ro ? RO_TIP : undefined} onClick={() => act(() => helmBaseline(s.card_id!))} className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut hover:text-slate-200 disabled:opacity-40">Accept the new scope</button>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}

function CostsTab({ id }: { id: number }) {
  const [c, setC] = useState<HelmCosts | null>(null);
  useEffect(() => { helmCosts(id).then(setC).catch(() => {}); }, [id]);
  if (!c) return <div className="p-4 text-[12px] text-mut">Loading…</div>;
  return (
    <div className="space-y-3 p-3 text-[12px]">
      <div className="flex items-baseline gap-2"><span className="text-[20px] font-semibold text-slate-100">{usd(c.total_usd)}</span><span className="text-mut">{c.note}</span></div>
      <table className="w-full text-left">
        <thead className="text-[10.5px] uppercase tracking-wide text-mut"><tr><th className="py-1">what</th><th>card</th><th className="text-right">cost</th></tr></thead>
        <tbody className="divide-y divide-line/60">
          {c.sessions.map((s) => <tr key={s.session_id}><td className="py-1 text-slate-200">🖥 {s.title}</td><td className="text-mut">#{s.card_id}</td><td className="text-right tabular-nums">{usd(s.cost_usd)}</td></tr>)}
          {c.runs.map((r) => <tr key={r.id}><td className="py-1 text-slate-200">⚙ run #{r.id} · {r.status}</td><td className="text-mut">#{r.card_id}</td><td className="text-right tabular-nums">{usd(r.cost_usd)}</td></tr>)}
          {!c.sessions.length && !c.runs.length && <tr><td colSpan={3} className="py-2 text-mut">No session or agent run linked under this card yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
