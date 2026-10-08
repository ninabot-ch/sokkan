"use client";
// 3.2.2 — the cockpit bar by PLANES (lib/planes.ts): row 1 = wordmark, project, the four
// planes, identity; row 2 = the sub-tabs of the plane on screen. A sub-tab that is not there
// disappears; a plane without a visible sub-tab disappears.
import { useEffect, useRef, useState } from "react";
import { useMe } from "@/lib/me";
import { useFeatures } from "@/lib/features";
import { llmStatus } from "@/lib/api";
import Wordmark from "./Wordmark";
import ProjectSelector from "./ProjectSelector";
import { SHORTCUTS_HELP, type PlaneDef, type PlaneId, type SubTab } from "@/lib/planes";

/** ←/→/Home/End inside a tablist: move the focus and activate (WAI-ARIA tabs pattern). */
function arrowNav<T>(e: React.KeyboardEvent, prefix: string, ids: T[], cur: T, go: (t: T) => void) {
  const i = ids.indexOf(cur);
  const n = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1
    : e.key === "Home" ? 0 : e.key === "End" ? ids.length - 1 : null;
  if (n === null || !ids.length) return;
  e.preventDefault();
  const t = ids[(n + ids.length) % ids.length];
  go(t);
  requestAnimationFrame(() => (document.getElementById(`${prefix}${String(t)}`) as HTMLElement | null)?.focus());
}

export default function Tabs({
  planes, plane, tab, onPlane, onTab, onGo,
}: {
  planes: PlaneDef[];
  plane: PlaneId | null;
  tab: SubTab | null;
  onPlane: (p: PlaneId) => void;
  onTab: (t: SubTab) => void;
  onGo: (t: SubTab, section?: string) => void;
}) {
  const feats = useFeatures();
  const me = useMe();
  const cur = planes.find((p) => p.id === plane);
  return (
    <>
    {feats.demo && <DemoBanner onGo={onGo} crew={!!(feats.agents && feats.agents_viewer_readonly)} captains={!!feats.demo_captains} />}
    {me?.secrets_warning && (
      <div role="status" className="relative z-30 border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[12px] text-amber-100">
        ⚠ {me.secrets_warning}
      </div>
    )}
    <header className="relative z-30 shrink-0 border-b border-line bg-panel">
      {/* < md: row 1 = wordmark · project · identity, the four planes get a full-width row of their
          own (they were hidden behind a sideways scroll at 390 px); ≥ md: one row, as before */}
      <div className="flex flex-wrap items-center gap-x-1.5 px-2 pt-1.5 md:h-[54px] md:flex-nowrap md:px-4 md:pt-0">
        <Wordmark className="shrink-0 text-[30px] md:text-[42px]" />
        <ProjectSelector />
        <span className="hidden md:inline md:mr-4" />
        <div role="tablist" aria-label="Planes" className="order-last mt-1 grid w-full grid-cols-4 gap-0.5 pb-1 md:order-none md:mt-0 md:flex md:w-auto md:shrink-0 md:items-center md:pb-0">
          {planes.map((p) => {
            const on = p.id === plane;
            return (
              <button key={p.id} id={`plane-${p.id}`} role="tab" aria-selected={on} aria-controls="cockpit-subtabs"
                tabIndex={on || (!plane && p === planes[0]) ? 0 : -1}
                onClick={() => onPlane(p.id)}
                onKeyDown={(e) => arrowNav(e, "plane-", planes.map((x) => x.id), p.id, onPlane)}
                title={`${p.label} — ${p.blurb} (g ${p.key})`}
                className={`ui-focus shrink-0 rounded-md border-b-2 px-1 py-2 text-center text-[13.5px] md:px-4 md:py-1.5 md:text-[15px] ${on
                  ? "border-sea bg-panel2 font-semibold text-slate-100"
                  : "border-transparent font-medium text-slate-300 hover:bg-panel2"}`}>
                {p.label}
              </button>
            );
          })}
        </div>
        <span tabIndex={0} role="note" aria-label={SHORTCUTS_HELP} title={SHORTCUTS_HELP}
          className="ui-focus ml-1 hidden h-6 shrink-0 cursor-help items-center rounded border border-line px-1.5 text-[11px] text-mut lg:inline-flex">
          <span aria-hidden>⌨</span></span>
        <MissionsPill enabled={feats.missions_link} />
        <Identity onGo={onGo} />
      </div>
      {cur && (
        <div id="cockpit-subtabs" role="tablist" aria-label={`${cur.label} sections`}
          className="flex flex-wrap items-center gap-0.5 border-t border-line/60 bg-panel/70 px-2 md:h-9 md:flex-nowrap md:overflow-x-auto md:px-4">
          {cur.tabs.map((t, i) => {
            const on = t.id === tab;
            return (
              <button key={t.id} id={`tab-${t.id}`} role="tab" aria-selected={on} aria-controls="cockpit-panel"
                tabIndex={on ? 0 : -1}
                onClick={() => onTab(t.id)}
                onKeyDown={(e) => arrowNav(e, "tab-", cur.tabs.map((x) => x.id), t.id, onTab)}
                title={i < 9 ? `${t.label} (${i + 1})` : t.label}
                className={`ui-focus h-9 shrink-0 border-b-2 px-3 text-[12.5px] md:h-full md:text-[13px] ${on
                  ? "border-sea font-semibold text-slate-100"
                  : "border-transparent text-mut hover:text-slate-200"}`}>
                {t.label}
              </button>
            );
          })}
        </div>
      )}
    </header>
    </>
  );
}

/** Guided banner for the public read-only demo (SOKKAN_DEMO_BANNER=1) : says
 *  where the visitor is, walks the 4 signature moves, links out. Dismissable
 *  per browser (localStorage) — never shown on regular instances. */
function DemoBanner({ onGo, crew, captains }: { onGo: (t: SubTab) => void; crew: boolean; captains: boolean }) {
  const [hidden, setHidden] = useState(true);
  useEffect(() => {
    try { setHidden(localStorage.getItem("sokkan_demo_banner") === "off"); } catch { setHidden(false); }
  }, []);
  if (hidden) return null;
  const go = (t: SubTab) => (e: React.MouseEvent) => { e.preventDefault(); onGo(t); };
  const tour = (<>
      <a href="/?plane=build&tab=sessions" onClick={go("sessions")} className="underline decoration-amber-400/60 hover:text-white">① open a session</a> (Build › Sessions — the memory recall sits at the top of each one) →{" "}
      <a href="/?plane=control&tab=board" onClick={go("board")} className="underline decoration-amber-400/60 hover:text-white">② the board</a> (Control › Board — cards spawn sessions) →{" "}
      <a href="/?plane=control&tab=corthexis" onClick={go("corthexis")} className="underline decoration-amber-400/60 hover:text-white">③ the memory graph</a> (Control › CortHeXis) →{" "}
      <a href="/?plane=operate&tab=costs" onClick={go("costs")} className="underline decoration-amber-400/60 hover:text-white">④ real costs</a> (Operate › Costs)
      {crew && <> → <a href="/?plane=build&tab=crew" onClick={go("crew")} className="underline decoration-amber-400/60 hover:text-white">⑤ the agents</a> (Build › Crew)
        {" "}→ <a href="/?plane=build&tab=crew" onClick={go("crew")} className="underline decoration-amber-400/60 hover:text-white">⑥ who approves what</a> (open a card « waiting for approval »: it names who may approve)</>}
      {captains && <> → <a href="/?plane=control&tab=helm" onClick={go("helm")} className="underline decoration-amber-400/60 hover:text-white">⑦ several teams, several projects</a> (Control › Helm, the project selector, Setup › Organization)</>}.{" "}
  </>);
  return (
    <div className="relative z-30 border-b border-amber-500/30 bg-amber-500/10 py-2 pl-3 pr-10 text-[12px] leading-relaxed text-amber-100">
      <b>You're in the live SOKKAN demo</b> — a real cloud tenant, read-only ·
      <i> Vous êtes dans la démo publique, en lecture seule.</i>{" "}
      {/* 3.2.2: on a phone the 7-stop tour took a third of the screen — folded there */}
      <span className="hidden md:inline">Try the tour: {tour}</span>
      <details className="my-0.5 md:hidden"><summary className="ui-focus cursor-pointer underline decoration-amber-400/60">Take the tour (Sessions, Board, memory, costs, agents{captains ? ", projects" : ""})</summary>
        {tour}</details>
      Want yours? <a href="https://app.sokkan.ch" target="_blank" rel="noopener" className="font-semibold underline decoration-amber-400 hover:text-white">14-day trial</a> ·{" "}
      <a href="https://sokkan.ch/install.sh" className="underline decoration-amber-400/60 hover:text-white">self-host free</a>
      <button onClick={() => { try { localStorage.setItem("sokkan_demo_banner", "off"); } catch { /* private mode */ } setHidden(true); }}
        className="ui-focus absolute right-2 top-1.5 flex h-7 w-7 items-center justify-center rounded text-amber-300/70 hover:text-white" title="hide" aria-label="hide the demo banner">✕</button>
    </div>
  );
}

/** Discreet link to SOKKAN Missions — deliver client projects, get paid.
 *  The counter comes from this instance's own API, which fetches it upstream at
 *  most once every six hours: your browser never talks to sokkan.ch for it.
 *  Opt out per instance: SOKKAN_FEATURE_MISSIONS_LINK=0. */
function MissionsPill({ enabled }: { enabled: boolean }) {
  const [open, setOpenCount] = useState<number | null>(null);
  useEffect(() => {
    if (!enabled) return;
    fetch("/api/missions/stats", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => { if (s && typeof s.open === "number") setOpenCount(s.open); })
      .catch(() => {});
  }, [enabled]);
  if (!enabled || open === null || open === 0) return null;
  return (
    <a
      href="https://sokkan.ch/missions/devs/?utm_source=cockpit"
      target="_blank"
      rel="noopener"
      className="ml-2 hidden shrink-0 items-center gap-1.5 rounded-full border border-brass/40 bg-brass/10 px-2.5 py-1 text-[11.5px] font-medium text-brass hover:bg-brass/20 sm:inline-flex"
      title="Want to deliver client projects and get paid? SOKKAN Missions — fixed-price missions, environment provided, live right now."
    >
      ⚓ {open} open mission{open > 1 ? "s" : ""} · get paid
    </a>
  );
}

function Identity({ onGo }: { onGo: (t: SubTab, section?: string) => void }) {
  const me = useMe();
  const [open, setOpen] = useState(false);
  const [st, setSt] = useState<{ configured: boolean; mode: string } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  useEffect(() => { llmStatus().then(setSt).catch(() => {}); }, []);
  const color: Record<string, string> = {
    owner: "text-brass", admin: "text-sea", dev: "text-emerald-400", viewer: "text-mut",
  };
  const motto = "hidden shrink-0 whitespace-nowrap min-[1440px]:inline";
  if (!me) return <span className={`ml-auto text-[11px] text-mut ${motto}`}>the helm, not the autopilot</span>;
  const warn = st && !st.configured;
  const go = (t: SubTab, section?: string) => { setOpen(false); onGo(t, section); };
  const item = "ui-focus flex w-full items-center gap-2 px-3 py-2 text-left text-[12.5px] text-slate-200 hover:bg-panel2";
  return (
    <div className="relative ml-auto flex shrink-0 items-center gap-2 text-[11px] text-mut">
      {/* 3.2.2 : the motto only where there is room (it wrapped on 3 lines at 1280 px) */}
      <span className={motto}>the helm, not the autopilot</span>
      <button ref={btn} aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen((o) => !o)} title={`${me.name} — ${me.email} · ${me.role}`}
        className="ui-focus flex min-h-[26px] max-w-[15rem] items-center gap-1 whitespace-nowrap rounded-full border border-line bg-panel2 px-2 py-0.5 hover:bg-line">
        <span className="hidden min-w-0 max-w-[9rem] truncate text-slate-200 sm:inline">{me.name}</span>
        <span aria-hidden className="hidden sm:inline">·</span>
        <span className={`shrink-0 ${color[me.role] || "text-mut"}`}>{me.role}</span>
        {warn && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" aria-label="model not configured" />}
        <span className="text-mut" aria-hidden>▾</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div role="menu" className="absolute right-0 top-full z-50 mt-1.5 w-56 overflow-hidden rounded-lg border border-line bg-panel shadow-xl">
            <div className="border-b border-line px-3 py-2">
              <div className="truncate text-[12px] text-slate-200">{me.name}</div>
              <div className="truncate text-[10.5px] text-mut">{me.email}</div>
            </div>
            <button role="menuitem" className={item} onClick={() => go("account")}>My account</button>
            <button role="menuitem" className={item} onClick={() => go("engines")}>Engines
              {warn && <span className="ml-auto text-[10.5px] text-amber-300">⚠ not configured</span>}</button>
            <button role="menuitem" className={item} onClick={() => go("organization")}>Organization</button>
            <a role="menuitem" href="/api/auth/logout" className={item}>Sign out →</a>
          </div>
        </>
      )}
    </div>
  );
}
