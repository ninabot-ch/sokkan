"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  agentsList, agentsMeta, agentGet, agentCreate, agentPatch, agentAction, agentRuns,
  agentRunCancel, spawnSession, fetchTags,
  type Agent, type AgentRun, type AgentsList, type AgentsMeta, type DeckState,
} from "@/lib/api";
import { useCan, useMe } from "@/lib/me";
import { agentTemplate, agentTemplates } from "@/lib/helm";
import { useFeatures } from "@/lib/features";
import { crewEngines } from "@/lib/uifeatures";
import AgentChatPane from "./AgentChatPane";
import CardModal from "./CardModal";
import { QuarantineReview } from "./Quarantine";

// « Crew » (3.1) — un agent = une carte. Le deck est un kanban dont les colonnes
// sont les états : la carte change de colonne toute seule quand l'agent vit.
// Spec : docs/AGENTS.md § The Crew tab.

const COLUMNS: { id: Exclude<DeckState, "archived">; label: string; icon: string; hint: string; empty: string }[] = [
  { id: "idle", label: "Idle", icon: "●", hint: "not armed — draft, waiting for approval, paused or manual", empty: "No idle agent." },
  { id: "armed", label: "Armed", icon: "◉", hint: "active, waiting for its trigger", empty: "No agent armed on a schedule or an alert." },
  { id: "running", label: "Running", icon: "▶", hint: "a run is going", empty: "Nothing running right now." },
  { id: "error", label: "Error", icon: "✕", hint: "the last run failed — until a run succeeds", empty: "No failure. 🎉" },
];

const RUN_STATUS: Record<string, { cls: string; label: string }> = {
  queued: { cls: "crew-c-running", label: "queued" },
  running: { cls: "crew-c-running", label: "running" },
  succeeded: { cls: "crew-c-armed", label: "succeeded" },
  incomplete: { cls: "crew-c-error", label: "incomplete" },
  failed: { cls: "crew-c-error", label: "failed" },
  timeout: { cls: "crew-c-error", label: "timeout" },
  budget: { cls: "crew-c-error", label: "budget hit" },
  interrupted: { cls: "crew-c-error", label: "interrupted" },
  cancelled: { cls: "crew-c-archived", label: "cancelled" },
  skipped: { cls: "crew-c-archived", label: "skipped" },
};

const ago = (ts: number | null | undefined) => {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  const a = Math.abs(s);
  const v = a < 60 ? "now" : a < 3600 ? `${Math.floor(a / 60)} min` : a < 86400 ? `${Math.floor(a / 3600)} h` : `${Math.floor(a / 86400)} d`;
  if (v === "now") return "just now";
  return s >= 0 ? `${v} ago` : `in ${v}`;
};
const clock = (ts: number | null | undefined, tz = "Europe/Zurich") =>
  ts ? new Date(ts * 1000).toLocaleString("en-GB", { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
const dur = (r: { started_at: number | null; ended_at: number | null }) => {
  if (!r.started_at) return "—";
  const s = Math.round((r.ended_at || Date.now() / 1000) - r.started_at);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}`;
};
const usd = (v: number | undefined) => (!v ? "$0" : v < 0.01 ? "<$0.01" : `$${v.toFixed(2)}`);

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
/** Cron → words for the common shapes; the raw expression otherwise. */
export function cronWords(expr: string): string {
  const p = expr.trim().split(/\s+/);
  if (p.length !== 5) return expr;
  const [mi, h, dom, mon, dow] = p;
  const hhmm = /^\d+$/.test(mi) && /^\d+$/.test(h) ? `${h.padStart(2, "0")}:${mi.padStart(2, "0")}` : "";
  if (mi.startsWith("*/") && h === "*" && dom === "*" && mon === "*" && dow === "*") return `every ${mi.slice(2)} min`;
  if (/^\d+$/.test(mi) && h === "*" && dom === "*" && mon === "*" && dow === "*") return `hourly at :${mi.padStart(2, "0")}`;
  if (hhmm && dom === "*" && mon === "*" && dow === "*") return `every day ${hhmm}`;
  if (hhmm && dom === "*" && mon === "*" && dow === "1-5") return `weekdays ${hhmm}`;
  if (hhmm && dom === "*" && mon === "*" && /^[0-7]$/.test(dow)) return `every ${DOW[+dow % 7]} ${hhmm}`;
  if (hhmm && dom === "*" && mon === "*" && /^[a-z]{3}$/i.test(dow)) return `every ${dow[0].toUpperCase()}${dow.slice(1, 3).toLowerCase()} ${hhmm}`;
  if (hhmm && /^\d+$/.test(dom) && mon === "*" && dow === "*") return `monthly, day ${dom} ${hhmm}`;
  return expr;
}
function triggerWords(a: Agent): string {
  const tz = a.timezone && a.timezone !== "Europe/Zurich" ? ` (${a.timezone})` : "";
  if (a.trigger === "cron") return `${cronWords(a.schedule)}${tz}`;
  if (a.trigger === "once") return a.once_at ? `once · ${new Date(a.once_at * 1000).toLocaleString("en-GB", { timeZone: a.timezone || "Europe/Zurich", dateStyle: "medium", timeStyle: "short" })}` : "once";
  if (a.trigger === "event") return a.event.includes(":") ? `on alert ${a.event.split(":")[1]}` : "on any Operate alert";
  return "manual";
}

/** Read-only (viewer with SOKKAN_CREW_VIEWER_READONLY=1): an action is shown, greyed
 *  out, with the reason as a tooltip — the visitor sees what an owner could do. */
function useReadOnly() {
  const canWrite = useCan("dev");
  const feats = useFeatures();
  return { ro: !canWrite, tip: feats.demo ? "read-only demo" : "read-only — your role can see agents, not change them" };
}
function Locked({ tip, children }: { tip: string; children: React.ReactNode }) {
  return <span title={tip} className="inline-flex cursor-not-allowed [&>button]:pointer-events-none [&>button]:opacity-45">{children}</span>;
}

function StatePill({ state, small }: { state: DeckState; small?: boolean }) {
  const col = COLUMNS.find((c) => c.id === state);
  return (
    <span className={`crew-c-${state} crew-fg crew-bd crew-bg inline-flex items-center gap-1 rounded-full border px-1.5 ${small ? "text-[9.5px]" : "text-[10.5px]"} font-semibold uppercase tracking-wide`}>
      <span aria-hidden>{col?.icon ?? "■"}</span>{col?.label ?? state}
    </span>
  );
}

// ---- deck -------------------------------------------------------------------
export default function Crew({ onOpenSession, onOpenIncident }: {
  onOpenSession?: (sid: string) => void; onOpenIncident?: (id: number) => void;
}) {
  const { ro, tip } = useReadOnly();
  const [data, setData] = useState<AgentsList | null>(null);
  const [focusRun, setFocusRun] = useState<number | null>(null);
  const [err, setErr] = useState("");
  const [archived, setArchived] = useState(false);
  const [openId, setOpenId] = useState<number | "new" | null>(null);
  const [chatSid, setChatSid] = useState<string | null>(null);
  const [newMenu, setNewMenu] = useState(false);
  // 3.3 : ready-made agents (Helm's morning brief) — pre-fill the form, nothing is created
  const [templates, setTemplates] = useState<{ id: string; label: string; description: string }[]>([]);
  const [draft, setDraft] = useState<Partial<Agent> | null>(null);
  const me = useMe();
  useEffect(() => { agentTemplates().then(setTemplates).catch(() => setTemplates([])); }, []);
  const fromTemplate = async (tid: string) => {
    setNewMenu(false);
    try {
      const t = await agentTemplate(tid, me?.email || "");
      setDraft(t.fields as Partial<Agent>);
      setOpenId("new");
    } catch (e) { setErr(String((e as Error).message)); }
  };

  const reload = useCallback(() => {
    agentsList(archived).then((d) => { setData(d); setErr(""); }).catch((e) => setErr(String(e.message || e)));
  }, [archived]);
  useEffect(() => { reload(); const iv = setInterval(reload, 4000); return () => clearInterval(iv); }, [reload]);
  // lien profond : /?tab=crew&agent=12
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const id = q.get("agent");
    if (id && /^\d+$/.test(id)) setOpenId(+id);
    const run = q.get("run");  // /?tab=crew&agent=12&run=34 : History, on that run (lien depuis Operate)
    if (run && /^\d+$/.test(run)) setFocusRun(+run);
    const chat = q.get("chat");  // /?tab=crew&chat=<sid> : reprendre une création par le chat
    if (chat && /^[0-9a-f]{32}$/.test(chat)) setChatSid(chat);
  }, []);

  const startChat = async () => {
    setNewMenu(false);
    const s = await spawnSession("devops", "", "New agent", "sdk", "new-agent");
    setChatSid(s.session_id);
  };

  const byCol = useMemo(() => {
    const m: Record<string, Agent[]> = { idle: [], armed: [], running: [], error: [], archived: [] };
    for (const a of data?.agents || []) (m[a.deck] ||= []).push(a);
    return m;
  }, [data]);
  const pendingCount = (data?.pending.agents.length || 0) + (data?.pending.runs.length || 0);

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel/60 px-3 py-2">
          <div className="mr-2">
            <div className="text-[14px] font-semibold text-slate-100">Crew</div>
            <div className="text-[10.5px] text-mut">agents that run on their own — one card each, human-gated</div>
          </div>
          {ro && <span title={tip} className="rounded-full border border-line bg-panel2 px-2 py-0.5 text-[10.5px] text-mut">👁 read-only</span>}
          {data?.scheduler?.held && (
            <span role="status" title={data.scheduler.reason || ""} className="rounded-lg border border-red-400/40 bg-red-500/10 px-2 py-1 text-[11.5px] text-red-300">
              ⏸ Scheduler stopped: no model credentials configured for this instance — nothing runs until they are set (Profile → Model).
            </span>
          )}
          {pendingCount > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-brass/40 bg-brass/10 px-2 py-1 text-[11.5px] text-brass">
              <b>{pendingCount} waiting for {ro ? "an approval" : "you"}:</b>
              {data!.pending.agents.map((a) => (
                <button key={`a${a.id}`} onClick={() => setOpenId(a.id)} className="rounded border border-brass/40 px-1.5 hover:bg-brass/20">
                  {a.pending_change ? "change to" : "new"} {a.name}
                </button>
              ))}
              {data!.pending.runs.map((r) => (
                <button key={`r${r.id}`} onClick={() => setOpenId(r.agent_id)} className="rounded border border-brass/40 px-1.5 hover:bg-brass/20">
                  {r.agent_name} run #{r.id} needs approval
                </button>
              ))}
            </div>
          )}
          <label className="ml-auto flex items-center gap-1 text-[11px] text-mut">
            <input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} /> archived
          </label>
          <div className="relative">
            {ro ? (
              <Locked tip={tip}><button disabled className="rounded-md bg-brass/90 px-3 py-1.5 text-[12.5px] font-semibold text-ink">+ New agent</button></Locked>
            ) : (
            <button onClick={() => setNewMenu((o) => !o)} className="rounded-md bg-brass/90 px-3 py-1.5 text-[12.5px] font-semibold text-ink hover:bg-brass">
              + New agent
            </button>
            )}
            {newMenu && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setNewMenu(false)} />
                <div className="absolute right-0 top-full z-50 mt-1 w-72 overflow-hidden rounded-lg border border-line bg-panel shadow-xl">
                  <button onClick={startChat} className="block w-full px-3 py-2.5 text-left hover:bg-panel2">
                    <div className="text-[12.5px] font-medium text-slate-100">💬 Build it in a chat <span className="ml-1 rounded bg-sea/20 px-1 text-[9.5px] text-sea">recommended</span></div>
                    <div className="text-[11px] text-mut">An agent asks you the right questions one at a time, then builds the card.</div>
                  </button>
                  <button onClick={() => { setNewMenu(false); setDraft(null); setOpenId("new"); }} className="block w-full border-t border-line px-3 py-2.5 text-left hover:bg-panel2">
                    <div className="text-[12.5px] font-medium text-slate-100">📝 Fill a form</div>
                    <div className="text-[11px] text-mut">Every field at once.</div>
                  </button>
                  {templates.map((t) => (
                    <button key={t.id} onClick={() => fromTemplate(t.id)} className="block w-full border-t border-line px-3 py-2.5 text-left hover:bg-panel2">
                      <div className="text-[12.5px] font-medium text-slate-100">🌅 {t.label} <span className="ml-1 rounded bg-panel2 px-1 text-[9.5px] text-mut">template</span></div>
                      <div className="text-[11px] text-mut">{t.description}</div>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>

        {err && <div className="m-3 rounded border border-red-500/40 bg-red-500/10 p-2 text-[12px] text-red-300">{err}</div>}

        <div className="grid min-h-0 flex-1 auto-cols-[minmax(250px,1fr)] grid-flow-col gap-3 overflow-x-auto p-3">
          {COLUMNS.map((col) => (
            <section key={col.id} className={`crew-c-${col.id} flex min-h-0 flex-col rounded-xl border border-line bg-panel/50`} aria-label={`${col.label} agents`}>
              <header className="flex items-center gap-2 border-b border-line px-3 py-2" title={col.hint}>
                <span className="crew-fg text-[12.5px] font-semibold"><span aria-hidden>{col.icon}</span> {col.label}</span>
                <span className="ml-auto rounded-full bg-panel2 px-1.5 text-[10.5px] text-mut">{byCol[col.id].length}</span>
              </header>
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
                {byCol[col.id].length === 0 && (
                  <div className="px-1 py-6 text-center text-[11px] text-mut/70">{col.empty}</div>
                )}
                {byCol[col.id].map((a) => <AgentCard key={a.id} a={a} onOpen={() => setOpenId(a.id)} />)}
              </div>
            </section>
          ))}
          {archived && byCol.archived.length > 0 && (
            <section className="crew-c-archived flex min-h-0 flex-col rounded-xl border border-line bg-panel/30">
              <header className="flex items-center gap-2 border-b border-line px-3 py-2">
                <span className="crew-fg text-[12.5px] font-semibold">■ Archived</span>
                <span className="ml-auto rounded-full bg-panel2 px-1.5 text-[10.5px] text-mut">{byCol.archived.length}</span>
              </header>
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
                {byCol.archived.map((a) => <AgentCard key={a.id} a={a} onOpen={() => setOpenId(a.id)} />)}
              </div>
            </section>
          )}
        </div>
        {data && data.agents.length === 0 && !chatSid && (
          <div className="mx-auto mb-10 max-w-lg rounded-xl border border-line bg-panel2/40 p-4 text-center text-[12.5px] text-mut">
            No agent yet. An agent is a job that runs on its own — a nightly CVE audit, a log triage every morning,
            a weekly ops report — and hands back a deliverable you review.
            {!ro && <div className="mt-3"><button onClick={startChat} className="rounded-md bg-brass/90 px-3 py-1.5 text-[12.5px] font-semibold text-ink hover:bg-brass">💬 Build the first one in a chat</button></div>}
          </div>
        )}
      </div>

      {chatSid && (
        <aside className="flex w-full min-w-0 flex-col border-l border-line bg-ink p-2 md:w-[440px] md:shrink-0">
          <div className="mb-1.5 flex items-center gap-2 px-1 text-[11px] text-mut">
            <span>The card appears in the deck as soon as the chat builds it.</span>
            <button onClick={() => setChatSid(null)} className="ml-auto rounded px-1.5 hover:bg-panel2 hover:text-slate-200" title="close the chat">✕</button>
          </div>
          <div className="flex min-h-0 flex-1 flex-col">
            <AgentChatPane sid={chatSid} title="New agent" tag="crew" onClose={() => setChatSid(null)} />
          </div>
        </aside>
      )}

      {openId !== null && (
        <AgentPopout id={openId} initialRun={focusRun} draft={openId === "new" ? draft : null}
          onClose={() => {
            setOpenId(null); setFocusRun(null); reload();
            try {  // the deep link has been followed: closing does not reopen it on reload
              const q = new URLSearchParams(window.location.search);
              if (q.has("agent") || q.has("run")) { q.delete("agent"); q.delete("run"); window.history.replaceState(null, "", `${window.location.pathname}?${q}`); }
            } catch { /* no history API */ }
          }}
          onCreated={(id) => { setOpenId(id); reload(); }} onChanged={reload} onOpenSession={onOpenSession}
          onOpenIncident={onOpenIncident} />
      )}
    </div>
  );
}

function AgentCard({ a, onOpen }: { a: Agent; onOpen: () => void }) {
  const running = a.deck === "running";
  return (
    <button onClick={onOpen}
      className={`crew-c-${a.deck} crew-card ${running ? "crew-breathe" : ""} group block w-full rounded-lg border border-line bg-panel2/70 p-2.5 text-left transition hover:bg-panel2 focus:outline-none focus-visible:ring-2 focus-visible:ring-sea`}
      aria-label={`${a.name}, ${a.deck}${a.needs_approval ? ", needs approval" : ""}`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold text-slate-100">{a.name}</div>
          <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-mut">{a.purpose}</div>
        </div>
        <StatePill state={a.deck} small />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1 text-[10.5px]">
        <span className="rounded bg-panel px-1.5 py-px text-slate-300" title="trigger">⏱ {triggerWords(a)}</span>
        <span className="rounded bg-panel px-1.5 py-px text-slate-300" title="model">{a.model || "default model"}</span>
        {a.secrets.length > 0 && <span className="rounded bg-panel px-1.5 py-px text-slate-300" title={`vault: ${a.secrets.join(", ")}`}>🔑 {a.secrets.length}</span>}
        {a.needs_approval && <span className="rounded border border-brass/50 bg-brass/10 px-1.5 py-px font-medium text-brass">{a.approval && !a.approval.can_approve && a.approval.reason !== "read-only" ? a.approval.reason : "needs approval"}</span>}
        {a.waiting_for_human && <span className="rounded border border-brass/50 bg-brass/10 px-1.5 py-px font-medium text-brass">waiting for you</span>}
        {a.status === "paused" && <span className="rounded bg-panel px-1.5 py-px text-mut">⏸ paused</span>}
        {a.status === "draft" && <span className="rounded bg-panel px-1.5 py-px text-mut">draft</span>}
      </div>
      <div className="mt-2 flex items-center gap-2 border-t border-line/60 pt-1.5 text-[10.5px] text-mut">
        {running ? (
          <span className="crew-fg flex items-center gap-1 font-medium"><span className="crew-dot crew-live-dot h-1.5 w-1.5 rounded-full" aria-hidden />running now</span>
        ) : a.last_run ? (
          <span>last: <span className={`${RUN_STATUS[a.last_run.status]?.cls || ""} crew-fg`}>{RUN_STATUS[a.last_run.status]?.label || a.last_run.status}</span> · {ago(a.last_run.ended_at)} · {usd(a.last_run.cost_usd)}</span>
        ) : <span>never ran</span>}
        {a.deck === "armed" && a.next_run_at && <span className="ml-auto text-slate-300" title={clock(a.next_run_at, a.timezone)}>next {ago(a.next_run_at)}</span>}
        {a.deck !== "armed" && a.stats && a.stats.runs > 0 && <span className="ml-auto shrink-0">{a.stats.runs} run{a.stats.runs > 1 ? "s" : ""} · {usd(a.stats.cost_usd)}</span>}
      </div>
      <div className="mt-1 truncate text-[10px] text-mut/70">{a.owner}</div>
    </button>
  );
}

// ---- popout -----------------------------------------------------------------
type PopTab = "Settings" | "Live" | "History";

function AgentPopout({ id, initialRun, draft, onClose, onCreated, onChanged, onOpenSession, onOpenIncident }: {
  draft?: Partial<Agent> | null;
  id: number | "new"; initialRun?: number | null; onClose: () => void; onCreated: (id: number) => void;
  onChanged: () => void; onOpenSession?: (sid: string) => void; onOpenIncident?: (id: number) => void;
}) {
  const { ro, tip } = useReadOnly();
  const [a, setA] = useState<Agent | null>(null);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [tab, setTab] = useState<PopTab>(initialRun ? "History" : "Settings");
  const [meta, setMeta] = useState<AgentsMeta | null>(null);
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    if (id === "new") return;
    agentGet(id).then(setA).catch((e) => setMsg(String(e.message || e)));
    agentRuns(id).then(setRuns).catch(() => {});
  }, [id]);
  useEffect(() => { agentsMeta().then(setMeta).catch(() => {}); }, []);
  useEffect(() => { load(); const iv = setInterval(load, 3000); return () => clearInterval(iv); }, [load]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);

  const live = runs.filter((r) => r.status === "running" || r.status === "queued");
  const act = async (action: "approve" | "reject" | "pause" | "resume" | "archive" | "run", override = false) => {
    setMsg("");
    try {
      await agentAction(id as number, action, override);
      if (action === "run") setTab("Live");
      load(); onChanged();
    } catch (e) { setMsg(String((e as Error).message)); }
  };

  const state: DeckState = a?.deck || "idle";
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/55 p-2 pt-[6vh] md:p-6" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-label={a ? `Agent ${a.name}` : "New agent"}
        className={`crew-c-${state} crew-card flex max-h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-2xl ${state === "running" ? "crew-breathe" : ""}`}>
        <header className="flex items-center gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="truncate text-[16px] font-semibold text-slate-100">{a ? a.name : "New agent"}</span>
              {a && <StatePill state={a.deck} />}
              {a?.needs_approval && <span className="rounded border border-brass/50 bg-brass/10 px-1.5 text-[10.5px] font-medium text-brass">{a.approval && !a.approval.can_approve && a.approval.reason !== "read-only" ? a.approval.reason : "needs approval"}</span>}
            </div>
            {a && <div className="mt-0.5 truncate text-[11px] text-mut">⏱ {triggerWords(a)} · {a.model || "default model"} · owner {a.owner} · {a.status}{a.created_by.startsWith("session:") ? " · proposed by a session" : a.created_by.startsWith("nina:") ? " · proposed by Nina" : ""}</div>}
          </div>
          {a && ro && (
            <div className="ml-auto flex flex-wrap items-center gap-1.5">
              {a.status === "active" && <Locked tip={tip}><button disabled className="rounded-md border border-sea/50 bg-sea/10 px-2.5 py-1 text-[12px] text-sea">▶ Run now</button></Locked>}
              {a.status === "active" && <Locked tip={tip}><button disabled className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut">⏸ Pause</button></Locked>}
              {a.status === "paused" && <Locked tip={tip}><button disabled className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut">⏵ Resume</button></Locked>}
              {a.status !== "archived" && <Locked tip={tip}><button disabled className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut">Archive</button></Locked>}
            </div>
          )}
          {a && !ro && (
            <div className="ml-auto flex flex-wrap items-center gap-1.5">
              {a.status === "active" && live.length === 0 && <button onClick={() => act("run")} className="rounded-md border border-sea/50 bg-sea/10 px-2.5 py-1 text-[12px] text-sea hover:border-sea">▶ Run now</button>}
              {a.status === "active" && <button onClick={() => act("pause")} className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut hover:text-slate-200">⏸ Pause</button>}
              {a.status === "paused" && <button onClick={() => act("resume")} className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut hover:text-slate-200">⏵ Resume</button>}
              {a.status !== "archived" && <button onClick={() => { if (confirm(`Archive ${a.name}? It stops for good; its history stays.`)) act("archive"); }} className="rounded-md border border-line px-2.5 py-1 text-[12px] text-mut hover:text-red-300">Archive</button>}
            </div>
          )}
          <button onClick={onClose} className={`${a ? "" : "ml-auto"} rounded-md px-2 py-1 text-mut hover:bg-panel2 hover:text-slate-200`} aria-label="close">✕</button>
        </header>

        {a?.needs_approval && (
          <ApprovalBar a={a} ro={ro} tip={tip} isAdmin={!!meta?.is_admin} onApprove={() => act("approve")} onReject={() => act("reject")}
            onOverride={() => { if (confirm(`Let ${(a.alert_write_rules || []).join(", ")} run unasked on an agent started by alerts? An alert payload is external input. This override is journaled.`)) act("approve", true); }} />
        )}

        <nav className="flex gap-1 border-b border-line px-3 pt-2" role="tablist">
          {(["Settings", "Live", "History"] as PopTab[]).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} disabled={id === "new" && t !== "Settings"} onClick={() => setTab(t)}
              className={`rounded-t-md px-3 py-1.5 text-[12.5px] ${tab === t ? "bg-panel2 text-slate-100 ring-1 ring-line" : "text-mut hover:text-slate-200 disabled:opacity-40"}`}>
              {t === "Live" ? <>Live{live.length > 0 && <span className="crew-c-running crew-dot crew-live-dot ml-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle" />}</> : t === "History" ? `History${runs.length ? ` (${runs.length})` : ""}` : t}
            </button>
          ))}
        </nav>
        {msg && <div className="mx-4 mt-2 rounded border border-red-500/40 bg-red-500/10 px-2 py-1 text-[12px] text-red-300">{msg}</div>}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {tab === "Settings" && meta && (
            <Settings a={id === "new" ? null : a} draft={draft} meta={meta} readOnlyRole={ro}
              onSaved={(saved) => { setMsg(""); if (id === "new") onCreated(saved.id); else { setA(saved); onChanged(); } }}
              onError={setMsg} />
          )}
          {tab === "Live" && a && (
            <div className="p-3">
              {live.length === 0 ? (
                <div className="rounded-lg border border-line bg-panel2/40 p-4 text-[12.5px] text-mut">
                  No run going. {a.status === "active" ? (ro ? "The agent waits for its trigger." : <button onClick={() => act("run")} className="ml-1 text-sea hover:underline">▶ Run now</button>) : `The agent is ${a.status}.`}
                </div>
              ) : live.map((r) => (
                <div key={r.id} className="mb-3">
                  <div className="mb-1.5 flex items-center gap-2 text-[11.5px] text-mut">
                    <span className="crew-c-running crew-fg font-medium">run #{r.id} · {r.status}</span>
                    <span>{r.trigger} · started {ago(r.started_at)} · {dur(r)}</span>
                    {r.waiting_approval && <span className="rounded border border-brass/50 bg-brass/10 px-1.5 font-medium text-brass">a tool call waits for your approval below</span>}
                    {ro || isSimulated(r) ? (
                      <span className="ml-auto"><Locked tip={isSimulated(r) && !ro ? "simulated run" : tip}><button disabled className="rounded border border-line px-2 py-0.5 text-[11px]">Stop run</button></Locked></span>
                    ) : (
                      <button onClick={() => agentRunCancel(r.id).then(load)} className="ml-auto rounded border border-line px-2 py-0.5 text-[11px] hover:text-red-300">Stop run</button>
                    )}
                  </div>
                  {isSimulated(r) ? (
                    <SimulatedLive run={r} />
                  ) : r.session_id ? (
                    <div className="flex h-[52vh] flex-col"><AgentChatPane sid={r.session_id} title={`${a.name} · run #${r.id}`} tag="agent" /></div>
                  ) : <div className="text-[12px] text-mut">Starting…</div>}
                </div>
              ))}
            </div>
          )}
          {tab === "History" && a && <History runs={runs} initialRun={initialRun} ro={ro} onOpenSession={onOpenSession} onOpenIncident={onOpenIncident} />}
        </div>
      </div>
    </div>
  );
}

/** 3.1.2 — mirror of agents.is_write_rule: rules that let a write run unasked */
const READ_MCP = new Set([
  "mcp__sokkan-memory__memory_search", "mcp__sokkan-memory__memory_get", "mcp__sokkan-memory__memory_links",
  "mcp__sokkan-board__list_tags", "mcp__sokkan-board__list_board",
  "mcp__sokkan-board__get_card", "mcp__sokkan-board__search_cards",
  "mcp__sokkan-board__get_card_tree", "mcp__sokkan-board__morning_brief",
  "mcp__sokkan-observability__query_metrics", "mcp__sokkan-observability__query_logs", "mcp__sokkan-observability__list_dashboards",
  "mcp__sokkan-agents__list_agents", "mcp__sokkan-agents__get_agent", "mcp__sokkan-agents__list_runs", "mcp__sokkan-agents__get_run",
]);
function alertWriteRules(rules: string[]): string[] {
  return rules.filter((r) => {
    const base = r.split("(")[0];
    return base.startsWith("mcp__") ? !READ_MCP.has(base) : ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"].includes(base);
  });
}

function ApprovalBar({ a, ro, tip, isAdmin, onApprove, onReject, onOverride }: {
  a: Agent; ro: boolean; tip: string; isAdmin: boolean; onApprove: () => void; onReject: () => void; onOverride: () => void;
}) {
  const change = a.pending_change;
  const held = a.alert_write_rules || [];
  const covered = held.length > 0 && held.every((r) => (a.alert_write_override?.rules || []).includes(r));
  return (
    <div className="border-b border-brass/30 bg-brass/10 px-4 py-2.5 text-[12px] text-brass">
      <div className="flex flex-wrap items-center gap-2">
        <b>{change ? "A change is waiting for your approval" : "This agent waits for your approval — it does not run before."}</b>
        <span className="text-brass/80">{a.created_by.startsWith("session:") ? "Proposed from a session." : a.created_by.startsWith("nina:") ? "Proposed in Nina's chat." : ""} Review the settings below.</span>
        <span className="ml-auto flex gap-1.5">
          {ro ? (
            <>
              <Locked tip={tip}><button disabled className="rounded-md bg-emerald-600/25 px-3 py-1 font-medium text-emerald-200 ring-1 ring-emerald-500/50">✓ Approve</button></Locked>
              <Locked tip={tip}><button disabled className="rounded-md px-3 py-1 text-mut ring-1 ring-line">Reject</button></Locked>
            </>
          ) : a.approval && !a.approval.can_approve ? (
            <span className="rounded-md border border-brass/50 px-2.5 py-1 font-medium" title={`approval mode: ${a.approval.mode}`}>
              {a.approval.reason === "needs a second approver" ? "🔒 needs a second approver — someone other than the proposer and the owner" : `🔒 ${a.approval.reason}`}
            </span>
          ) : held.length > 0 && !covered ? (
            isAdmin
              ? <button onClick={onOverride} title="journaled in the audit log" className="rounded-md bg-red-600/20 px-3 py-1 font-medium text-red-200 ring-1 ring-red-500/50 hover:bg-red-600/35">✓ Approve with override (admin)</button>
              : <span className="rounded-md border border-red-500/50 px-2.5 py-1 font-medium text-red-300">🔒 remove the write rules, or an admin overrides</span>
          ) : (
            <button onClick={onApprove} className="rounded-md bg-emerald-600/25 px-3 py-1 font-medium text-emerald-200 ring-1 ring-emerald-500/50 hover:bg-emerald-600/40">✓ Approve</button>
          )}
          {!ro && <button onClick={onReject} className="rounded-md px-3 py-1 text-mut ring-1 ring-line hover:text-red-300">Reject</button>}
        </span>
      </div>
      {held.length > 0 && !covered && (
        <div className="mt-1.5 text-[11.5px] text-red-300">
          ⚠ Started by Operate alerts, and {held.join(", ")} would run without asking. An alert payload is external input that could try to steer the run:
          remove these from “Runs without asking” (the calls then wait for a human), or an admin approves with the override.
        </div>
      )}
      {change && (
        <table className="mt-2 w-full text-[11.5px]">
          <tbody>
            {Object.entries(change).map(([k, v]) => (
              <tr key={k} className="border-t border-brass/20">
                <td className="py-0.5 pr-3 font-mono text-brass/80">{k}</td>
                <td className="py-0.5 pr-3 text-mut line-through">{fmtVal((a as unknown as Record<string, unknown>)[k])}</td>
                <td className="py-0.5 text-slate-100">{fmtVal(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
const fmtVal = (v: unknown) => (Array.isArray(v) ? v.join(", ") || "—" : v === null || v === undefined || v === "" ? "—" : String(v));

function History({ runs, initialRun, ro, onOpenSession, onOpenIncident }: {
  runs: AgentRun[]; initialRun?: number | null; ro: boolean;
  onOpenSession?: (sid: string) => void; onOpenIncident?: (id: number) => void;
}) {
  const [sel, setSel] = useState<number | null>(
    (initialRun && runs.some((r) => r.id === initialRun) ? initialRun : null)
    ?? runs.find((r) => !["skipped", "running", "queued"].includes(r.status))?.id
    ?? runs.find((r) => r.status !== "skipped")?.id ?? null);
  const [transcript, setTranscript] = useState(false);
  const [card, setCard] = useState<number | null>(null);
  const [tags, setTags] = useState<string[]>([]);
  useEffect(() => { fetchTags().then(setTags).catch(() => {}); }, []);
  // the runs arrive after the first render: follow the deep link once they are there
  useEffect(() => { if (initialRun && runs.some((r) => r.id === initialRun)) setSel((s) => s ?? initialRun); }, [initialRun, runs]);
  const run = runs.find((r) => r.id === sel) || null;
  if (runs.length === 0) return <div className="p-4 text-[12.5px] text-mut">No run yet.</div>;
  return (
    <div className="grid min-h-0 gap-0 md:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
      <div className="border-b border-line md:border-b-0 md:border-r">
        <table className="w-full text-[11.5px]">
          <thead className="sticky top-0 bg-panel text-left text-[10.5px] uppercase tracking-wide text-mut">
            <tr><th className="px-3 py-1.5">Run</th><th className="px-2">Status</th><th className="px-2">When</th><th className="px-2">Took</th><th className="px-2 text-right">Cost</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const st = RUN_STATUS[r.status] || { cls: "", label: r.status };
              return (
                <tr key={r.id} onClick={() => { setSel(r.id); setTranscript(false); }}
                  className={`cursor-pointer border-t border-line/60 hover:bg-panel2 ${sel === r.id ? "bg-panel2" : ""}`}>
                  <td className="px-3 py-1.5 text-slate-300">#{r.id} <span className="text-mut">{r.trigger}</span></td>
                  <td className="px-2"><span className={`${st.cls} crew-fg font-medium`}>{st.label}</span></td>
                  <td className="px-2 text-mut">{ago(r.started_at || r.created_at)}</td>
                  <td className="px-2 text-mut">{dur(r)}</td>
                  <td className="px-2 text-right text-slate-300">{usd(r.cost_usd)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="min-w-0 p-3">
        {run && (
          <>
            <div className="mb-2 flex flex-wrap items-center gap-2 text-[11.5px] text-mut">
              <span className={`${RUN_STATUS[run.status]?.cls || ""} crew-fg font-semibold`}>{RUN_STATUS[run.status]?.label || run.status}</span>
              <span>run #{run.id} · {run.trigger}{run.requested_by ? ` · by ${run.requested_by}` : ""}</span>
              <span>· {run.tokens_in.toLocaleString()} in / {run.tokens_out.toLocaleString()} out · {run.num_turns} turn{run.num_turns === 1 ? "" : "s"} · {usd(run.cost_usd)}</span>
              {run.session_id && (
                <span className="ml-auto flex gap-1.5">
                  <button onClick={() => setTranscript((t) => !t)} className="rounded border border-line px-2 py-0.5 hover:text-slate-200">{transcript ? "deliverable" : "transcript"}</button>
                  {onOpenSession && <button onClick={() => onOpenSession(run.session_id)} className="rounded border border-sea/40 px-2 py-0.5 text-sea hover:border-sea">open session →</button>}
                </span>
              )}
            </div>
            {run.error && <div className="mb-2 rounded border border-red-500/40 bg-red-500/10 px-2 py-1 text-[12px] text-red-300">{run.error}</div>}
            {Object.keys(run.outputs || {}).length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1.5 text-[10.5px]">
                {Object.entries(run.outputs).map(([k, v]) => k === "card" && typeof v === "number" ? (
                  <button key={k} onClick={() => setCard(v)} title="open the card on the board"
                    className="rounded border border-sea/40 bg-sea/10 px-1.5 py-px text-sea hover:border-sea">board card #{v} →</button>
                ) : k === "incident" && typeof v === "number" ? (
                  <button key={k} onClick={() => onOpenIncident?.(v)} disabled={!onOpenIncident} title="this failure opened an incident in Operate"
                    className="rounded border border-red-500/40 bg-red-500/10 px-1.5 py-px text-red-300 hover:border-red-400">🚨 incident #{v} →</button>
                ) : (
                  <span key={k} className="rounded bg-panel2 px-1.5 py-px text-slate-300">{k === "card" ? `board card #${v}` : k === "memory" ? `memory note ${v}` : k === "memory_quarantined" ? "⚠ quarantined" : k === "file" ? `file ${String(v).split("/").slice(-2).join("/")}` : k === "notify" ? "notified" : k === "cost_basis" ? `💱 cost: ${v}` : `${k}: ${v}`}</span>
                ))}
              </div>
            )}
            {typeof run.context?.incident === "number" && (
              <div className="mb-2 text-[11.5px] text-mut">
                triggered by{" "}
                <button onClick={() => onOpenIncident?.(run.context.incident as number)} disabled={!onOpenIncident}
                  className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-px text-amber-200 hover:border-amber-400">
                  incident #{String(run.context.incident)}{run.context.alertname ? ` · ${String(run.context.alertname)}` : ""} →
                </button>
              </div>
            )}
            {typeof run.outputs?.memory === "string" && run.outputs?.memory_quarantined === true && (
              ro ? <div className="mb-2 text-[11.5px] text-mut">The note waits in quarantine for a reviewer (dev+); its content is not shown here.</div>
                : <div className="mb-2"><QuarantineReview name={run.outputs.memory as string} /></div>
            )}
            {transcript && run.session_id ? (
              <div className="flex h-[52vh] flex-col"><AgentChatPane sid={run.session_id} title={`run #${run.id}`} tag="agent" /></div>
            ) : run.deliverable ? (
              <div className="md rounded-lg border border-line/60 bg-panel2/40 p-3 text-[13px] text-slate-200">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{run.deliverable.replace(/\n?\s*\**DELIVERY\**\s*:.*$/i, "").trim()}</ReactMarkdown>
              </div>
            ) : <div className="text-[12px] text-mut">No deliverable.</div>}
          </>
        )}
      </div>
      {card !== null && (
        <CardModal cardId={card} tags={tags} onClose={() => setCard(null)} onChanged={() => {}}
          onOpenSession={(sid) => { setCard(null); onOpenSession?.(sid); }} />
      )}
    </div>
  );
}

// ---- simulated run (public demo only, SOKKAN_DEMO_CREW=1) -------------------
type SimStep = { at: number; kind: "recall" | "tool" | "text"; text?: string; tool?: string; input?: string; result?: string };
const isSimulated = (r: AgentRun) => !!(r.context && (r.context as { simulated?: boolean }).simulated);

function SimulatedLive({ run }: { run: AgentRun }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => { const iv = setInterval(() => setNow(Date.now() / 1000), 1000); return () => clearInterval(iv); }, []);
  const ctx = run.context as { steps?: SimStep[]; duration_s?: number };
  const total = ctx.duration_s || 150;
  const el = Math.max(0, now - (run.started_at || now));
  const shown = (ctx.steps || []).filter((s) => s.at <= el);
  const end = useRef<HTMLDivElement | null>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "nearest" }); }, [shown.length]);
  return (
    <div className="rounded-lg border border-line bg-ink/60">
      <div className="flex items-center gap-2 border-b border-line px-3 py-1.5 text-[11px] text-mut">
        <span className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 text-amber-200">simulated</span>
        <span>This public demo replays a recorded run — no model is called, nothing is changed.</span>
        <span className="ml-auto tabular-nums">{Math.min(100, Math.round((el / total) * 100))}%</span>
      </div>
      <div className="h-0.5 bg-panel2"><div className="crew-c-running crew-dot h-0.5 transition-[width] duration-1000" style={{ width: `${Math.min(100, (el / total) * 100)}%` }} /></div>
      <div className="max-h-[46vh] space-y-2 overflow-y-auto p-3 text-[12.5px]">
        {shown.map((s, i) => s.kind === "tool" ? (
          <div key={i} className="rounded-md border border-line bg-panel2/50 px-2 py-1.5">
            <div className="font-mono text-[11.5px] text-slate-200"><span className="text-sea">⚙ {s.tool}</span> {s.input}</div>
            {s.result && <div className="mt-0.5 font-mono text-[11px] text-mut">→ {s.result}</div>}
          </div>
        ) : s.kind === "recall" ? (
          <div key={i} className="rounded-md border border-sea/30 bg-sea/5 px-2 py-1.5 text-[11.5px] text-sea">🧠 {s.text}</div>
        ) : (
          <div key={i} className="text-slate-200">{s.text}</div>
        ))}
        <div className="crew-c-running crew-fg flex items-center gap-1.5 text-[11.5px]">
          <span className="crew-dot crew-live-dot h-1.5 w-1.5 rounded-full" aria-hidden />working…
        </div>
        <div ref={end} />
      </div>
    </div>
  );
}

// ---- settings (create + edit) -----------------------------------------------
const EMPTY: Partial<Agent> = {
  name: "", purpose: "", deliverable: "", done_criteria: "", model: "", trigger: "cron",
  schedule: "0 2 * * *", timezone: "Europe/Zurich", event: "alert", tools: [], mcp: ["sokkan-memory"],
  auto_approve: [], secrets: [], budget_usd: 1, max_minutes: 30, outputs: ["card"],
  notify_on: ["failure", "timeout", "budget", "approval"], playbook: "",
};

function Settings({ a, draft, meta, readOnlyRole, onSaved, onError }: {
  a: Agent | null; draft?: Partial<Agent> | null; meta: AgentsMeta; readOnlyRole?: boolean; onSaved: (a: Agent) => void; onError: (m: string) => void;
}) {
  const [f, setF] = useState<Partial<Agent>>(() => (a ? { ...a } : { ...EMPTY, tools: meta.default_tools, ...(draft || {}) }));
  const [activate, setActivate] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  // 3.2 connect_ai : connected engines can drive this card (model `engine:<id>`)
  const [engines, setEngines] = useState<{ value: string; label: string; model: string }[]>([]);
  useEffect(() => { crewEngines().then(setEngines).catch(() => setEngines([])); }, []);
  // re-sync from the server while the user has not touched the form
  useEffect(() => { if (a && !dirty) setF({ ...a }); }, [a, dirty]);
  const set = <K extends keyof Agent>(k: K, v: Agent[K]) => { setDirty(true); setF((x) => ({ ...x, [k]: v })); };
  const toggle = (k: "tools" | "mcp" | "secrets" | "outputs" | "notify_on", v: string) =>
    set(k, ((f[k] as string[]) || []).includes(v) ? ((f[k] as string[]) || []).filter((x) => x !== v) : [...((f[k] as string[]) || []), v]);
  const readOnly = a?.status === "archived" || !!readOnlyRole;
  // names only: the agent's own secret names, plus the vault's (never a value)
  const secretNames = Array.from(new Set([...(meta.secrets || []), ...((f.secrets as string[]) || [])]));

  const save = async () => {
    setSaving(true); onError("");
    const body: Partial<Agent> = {
      name: f.name, purpose: f.purpose, deliverable: f.deliverable, done_criteria: f.done_criteria,
      model: f.model, trigger: f.trigger, schedule: f.trigger === "cron" ? f.schedule : "",
      timezone: f.timezone, once_at: f.trigger === "once" ? f.once_at : null,
      event: f.trigger === "event" ? f.event : "", tools: f.tools, mcp: f.mcp,
      auto_approve: f.auto_approve, secrets: f.secrets, budget_usd: Number(f.budget_usd) || 0,
      max_minutes: Number(f.max_minutes) || 30, outputs: f.outputs, notify_on: f.notify_on, playbook: f.playbook,
    };
    const send = (override: boolean) => {
      const b = override ? { ...body, override_alert_writes: true } : body;
      return a ? agentPatch(a.id, b) : agentCreate({ ...b, activate: meta.self_activation ? activate : true });
    };
    try {
      let saved: Agent;
      try { saved = await send(false); } catch (e) {
        const m = String((e as Error).message);
        // 3.1.2 : an admin may override the alert write rule — explicitly, journaled
        if (!(meta.is_admin && m.includes("alert-triggered agent cannot auto-approve")
          && confirm(`${m}\n\nOverride as an admin? It is journaled in the audit log.`))) throw e;
        saved = await send(true);
      }
      setDirty(false);
      onSaved(saved);
    } catch (e) { onError(String((e as Error).message)); }
    finally { setSaving(false); }
  };

  const inp = "w-full rounded-md border border-line bg-ink px-2 py-1.5 text-[12.5px] text-slate-100 outline-none focus:border-sea/60 disabled:opacity-60";
  const lbl = "mb-1 block text-[11px] font-medium uppercase tracking-wide text-mut";
  const chip = (on: boolean) => `rounded-md border px-2 py-0.5 text-[11.5px] ${on ? "border-sea/60 bg-sea/15 text-sea" : "border-line text-mut hover:text-slate-200"}`;
  const localInput = (ts: number | null | undefined) => {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  };

  return (
    <fieldset disabled={readOnly} className="grid gap-4 p-4 md:grid-cols-2">
      <div className="space-y-3">
        <div><label className={lbl}>Name</label>
          <input className={inp} value={f.name || ""} onChange={(e) => set("name", e.target.value)} placeholder="nightly-cve-audit" /></div>
        <div><label className={lbl}>Purpose — the mission</label>
          <textarea className={`${inp} h-24`} value={f.purpose || ""} onChange={(e) => set("purpose", e.target.value)} placeholder="Audit the npm and pip dependencies of /workspace for known CVEs." /></div>
        <div><label className={lbl}>Expected deliverable</label>
          <textarea className={`${inp} h-16`} value={f.deliverable || ""} onChange={(e) => set("deliverable", e.target.value)} placeholder="A table: package, version, CVE, severity, fixed version, upgrade command." /></div>
        <div><label className={lbl}>Done when</label>
          <textarea className={`${inp} h-14`} value={f.done_criteria || ""} onChange={(e) => set("done_criteria", e.target.value)} placeholder="every direct dependency has been checked" /></div>
        <div className="grid grid-cols-2 gap-2">
          <div><label className={lbl}>Model</label>
            <select className={inp} value={meta.models.includes(f.model || "") || engines.some((e) => e.value === f.model) ? f.model : "__custom"} onChange={(e) => set("model", e.target.value === "__custom" ? (f.model || "") : e.target.value)}>
              <option value="">instance default</option>
              <option value="haiku">haiku — cheap, routine</option>
              <option value="sonnet">sonnet — balanced</option>
              <option value="opus">opus — hard reasoning</option>
              {engines.length > 0 && (
                <optgroup label="Connect your AI — engines">
                  {engines.map((e) => <option key={e.value} value={e.value}>{e.label}{e.model ? ` — ${e.model}` : ""}</option>)}
                </optgroup>
              )}
              {!meta.models.includes(f.model || "") && !engines.some((e) => e.value === f.model) && <option value="__custom">{f.model}</option>}
            </select></div>
          <div><label className={lbl}>Playbook (optional)</label>
            <select className={inp} value={f.playbook || ""} onChange={(e) => set("playbook", e.target.value)}>
              <option value="">none</option>
              {meta.playbooks.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select></div>
        </div>
        <div>
          <label className={lbl}>Trigger</label>
          <div className="flex flex-wrap gap-1.5">
            {(["manual", "once", "cron", "event"] as const).map((t) => (
              <button type="button" key={t} className={chip(f.trigger === t)} onClick={() => set("trigger", t)}>
                {t === "manual" ? "manual" : t === "once" ? "one-shot" : t === "cron" ? "recurring" : "on alert"}
              </button>
            ))}
          </div>
          {f.trigger === "cron" && (
            <div className="mt-2 grid grid-cols-[1fr_auto] gap-2">
              <input className={`${inp} font-mono`} value={f.schedule || ""} onChange={(e) => set("schedule", e.target.value)} placeholder="0 2 * * *" />
              <input className={`${inp} w-36`} value={f.timezone || ""} onChange={(e) => set("timezone", e.target.value)} />
              <div className="col-span-2 text-[11px] text-mut">= {cronWords(f.schedule || "")} · minute hour day month weekday</div>
            </div>
          )}
          {f.trigger === "once" && (
            <input type="datetime-local" className={`${inp} mt-2`} value={localInput(f.once_at)}
              onChange={(e) => set("once_at", e.target.value ? new Date(e.target.value).getTime() / 1000 : null)} />
          )}
          {f.trigger === "event" && (
            <div className="mt-2"><input className={inp} value={f.event || ""} onChange={(e) => set("event", e.target.value)} placeholder="alert  or  alert:Postgres*" />
              <div className="mt-1 text-[11px] text-mut">An alert received by Operate starts a run with the alert as context.</div></div>
          )}
        </div>
      </div>

      <div className="space-y-3">
        <div><label className={lbl}>Tools it may use — anything else is refused</label>
          <div className="flex flex-wrap gap-1.5">{meta.tools.map((t) => <button type="button" key={t} className={chip((f.tools || []).includes(t))} onClick={() => toggle("tools", t)}>{t}</button>)}</div></div>
        <div><label className={lbl}>Runs without asking (Claude Code rules)</label>
          <input className={`${inp} font-mono`} value={(f.auto_approve || []).join(", ")} onChange={(e) => set("auto_approve", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} placeholder="Bash(npm audit:*), Bash(git log:*)" />
          <div className="mt-1 text-[11px] text-mut">Every other mutating call waits for a human — you get pinged.</div>
          {f.trigger === "event" && (f.event || "").startsWith("alert") && alertWriteRules(f.auto_approve || []).length > 0 && (
            <div className="mt-1 rounded border border-red-500/40 bg-red-500/10 px-2 py-1 text-[11px] text-red-300">
              ⚠ Alert-triggered: {alertWriteRules(f.auto_approve || []).join(", ")} cannot run unasked — the alert payload is external input.
              Approval is refused unless an admin overrides it (journaled); in runs started by an alert these calls wait for a human.
            </div>)}</div>
        <div><label className={lbl}>MCP servers</label>
          <div className="flex flex-wrap gap-1.5">{meta.mcp.map((m) => (
            <button type="button" key={m} disabled={m === "sokkan-memory"} className={chip((f.mcp || []).includes(m) || m === "sokkan-memory")} onClick={() => toggle("mcp", m)}>
              {m === "sokkan-memory" ? "sokkan-memory · CortHeXis (always)" : m}
            </button>))}</div></div>
        <div><label className={lbl}>Secrets from the vault — by name, never the value</label>
          {secretNames.length === 0 ? <div className="text-[11.5px] text-mut">The vault is empty — an admin adds secrets in Profile → Secrets.</div> :
            <div className="flex flex-wrap gap-1.5">{secretNames.map((s) => <button type="button" key={s} className={chip((f.secrets || []).includes(s))} onClick={() => toggle("secrets", s)}>🔑 {s}</button>)}</div>}</div>
        <div className="grid grid-cols-2 gap-2">
          <div><label className={lbl}>Budget per run (USD)</label>
            <input type="number" min={0} step={0.1} className={inp} value={f.budget_usd ?? 0} onChange={(e) => set("budget_usd", Number(e.target.value))} /></div>
          <div><label className={lbl}>Time limit (min)</label>
            <input type="number" min={1} className={inp} value={f.max_minutes ?? 30} onChange={(e) => set("max_minutes", Number(e.target.value))} /></div>
        </div>
        {(() => {
          const m = (a && a.model === f.model ? a.metering : undefined) || (!f.model ? meta.metering : undefined);
          if (!m) return f.model && a && a.model !== f.model ? <div className="-mt-1 text-[11px] text-mut">Save to see how runs on {f.model} are metered.</div> : null;
          return m.basis === "sdk" ? null : (
            <div className={`-mt-1 rounded border px-2 py-1 text-[11px] ${m.price ? "border-line text-mut" : "border-amber-500/40 bg-amber-500/10 text-amber-200"}`}>
              {m.price ? "💱 " : "⚠ "}{m.note}
            </div>);
        })()}
        <div><label className={lbl}>Deliverable goes to</label>
          <div className="flex flex-wrap gap-1.5">{meta.outputs.map((o) => <button type="button" key={o} className={chip((f.outputs || []).includes(o))} onClick={() => toggle("outputs", o)}>
            {o === "card" ? "board card (Review)" : o === "memory" ? "memory note" : o === "file" ? "file" : "notification"}</button>)}</div></div>
        <div><label className={lbl}>Notify me on</label>
          <div className="flex flex-wrap gap-1.5">{meta.notify_on.map((o) => <button type="button" key={o} className={chip((f.notify_on || []).includes(o))} onClick={() => toggle("notify_on", o)}>{o}</button>)}</div></div>
      </div>

      {!readOnly && (
        <div className="flex items-center gap-3 border-t border-line pt-3 md:col-span-2">
          {!a && (meta.self_activation
            ? <label className="flex items-center gap-1.5 text-[12px] text-mut"><input type="checkbox" checked={activate} onChange={(e) => setActivate(e.target.checked)} /> activate now (you are the human gate)</label>
            : <span className="text-[12px] text-brass">🔒 approval mode « {meta.approval_mode} »: the card waits for {meta.approval_mode === "admin" ? "an admin" : "a second approver"} before it runs.</span>)}
          {a && !meta.self_activation && ["active", "paused"].includes(a.status) && <span className="text-[11.5px] text-brass">changes wait for {meta.approval_mode === "admin" ? "an admin" : "a second approver"}</span>}
          {a && dirty && <span className="text-[11.5px] text-brass">unsaved changes</span>}
          <button onClick={save} disabled={saving || (!!a && !dirty)} className="ml-auto rounded-md bg-brass/90 px-4 py-1.5 text-[12.5px] font-semibold text-ink hover:bg-brass disabled:opacity-40">
            {a ? "Save" : !meta.self_activation ? "Submit for approval" : activate ? "Create & arm" : "Create draft"}
          </button>
        </div>
      )}
    </fieldset>
  );
}
