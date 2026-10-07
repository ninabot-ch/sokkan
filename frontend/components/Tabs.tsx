"use client";
import { useEffect, useState } from "react";
import { useMe, useCan } from "@/lib/me";
import { useFeatures } from "@/lib/features";
import { llmStatus } from "@/lib/api";
import Wordmark from "./Wordmark";
import Profile from "./Profile";
import ProjectSelector from "./ProjectSelector";
import { currentProject } from "@/lib/project";
import { helmAccess } from "@/lib/helm";

const TABS = ["Board", "Sessions", "Crew", "Helm", "Preview", "CortHeXis", "Costs", "Magnitude", "Infra", "Operate", "Journal"] as const;
export type Tab = (typeof TABS)[number];

export default function Tabs({
  active,
  onChange,
}: {
  active: Tab;
  onChange: (t: Tab) => void;
}) {
  const feats = useFeatures();
  const canDev = useCan("dev");
  const me = useMe();
  // 3.3 Helm : the tab exists for the people who steer at least one project
  const [steers, setSteers] = useState(false);
  useEffect(() => {
    if (!feats.helm) { setSteers(false); return; }
    helmAccess().then((a) => setSteers(a.steers.length > 0)).catch(() => setSteers(false));
  }, [feats.helm]);
  const visible = TABS.filter(
    // 3.2 : Preview (dépôts de l'instance) et Infra n'existent que dans le projet par défaut / pour l'ops
    (t) => (t !== "Preview" || (feats.preview && currentProject() === "default"))
      && (t !== "Infra" || (feats.infra && (!me || me.ops !== false)))
      // 3.2 : Operate = l'équipe ops (groupe SSO) + les admins de l'instance
      && (t !== "Operate" || (feats.observe && (!me || me.ops !== false)))
      && (t !== "Magnitude" || feats.magnitude)
      && (t !== "Crew" || (feats.agents && (canDev || !!feats.agents_viewer_readonly)))
      && (t !== "Helm" || (!!feats.helm && steers))
  );
  return (
    <>
    {feats.demo && <DemoBanner onChange={onChange} crew={!!(feats.agents && feats.agents_viewer_readonly)} />}
    {me?.secrets_warning && (
      <div role="status" className="relative z-30 border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[12px] text-amber-100">
        ⚠ {me.secrets_warning}
      </div>
    )}
    <header className="relative z-30 flex h-[54px] shrink-0 items-center gap-1.5 overflow-x-auto border-b border-line bg-panel px-2 md:overflow-visible md:px-4">
      <Wordmark className="shrink-0 text-[30px] md:text-[42px]" />
      <ProjectSelector />
      <span className="mr-2 md:mr-6" />
      {visible.map((t) => {
        const enabled = true;
        return (
          <button
            key={t}
            disabled={!enabled}
            onClick={() => enabled && onChange(t)}
            className={`shrink-0 rounded-md px-2.5 py-1.5 text-[13px] font-medium md:px-4 md:text-[15px] ${
              active === t
                ? "bg-panel2 text-slate-100 ring-1 ring-line"
                : enabled
                ? "text-slate-300 hover:bg-panel2"
                : "cursor-not-allowed text-mut/50"
            }`}
            title={enabled ? "" : "coming soon (P2+)"}
          >
            {t}
          </button>
        );
      })}
      <MissionsPill enabled={feats.missions_link} />
      <Identity />
    </header>
    </>
  );
}

/** Guided banner for the public read-only demo (SOKKAN_DEMO_BANNER=1) : says
 *  where the visitor is, walks the 4 signature moves, links out. Dismissable
 *  per browser (localStorage) — never shown on regular instances. */
function DemoBanner({ onChange, crew }: { onChange: (t: Tab) => void; crew: boolean }) {
  const [hidden, setHidden] = useState(true);
  useEffect(() => {
    try { setHidden(localStorage.getItem("sokkan_demo_banner") === "off"); } catch { setHidden(false); }
  }, []);
  if (hidden) return null;
  const go = (t: Tab) => (e: React.MouseEvent) => { e.preventDefault(); onChange(t); };
  return (
    <div className="relative z-30 border-b border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[12px] leading-relaxed text-amber-100">
      <b>You're in the live SOKKAN demo</b> — a real cloud tenant, read-only ·
      <i> Vous êtes dans la démo publique, en lecture seule.</i>{" "}
      Try the tour: <a href="#" onClick={go("Sessions")} className="underline decoration-amber-400/60 hover:text-white">① open a session</a> (the memory recall sits at the top of each one) →{" "}
      <a href="#" onClick={go("Board")} className="underline decoration-amber-400/60 hover:text-white">② the board</a> (cards spawn sessions) →{" "}
      <a href="#" onClick={go("CortHeXis")} className="underline decoration-amber-400/60 hover:text-white">③ the memory graph</a> →{" "}
      <a href="#" onClick={go("Costs")} className="underline decoration-amber-400/60 hover:text-white">④ real costs</a>
      {crew && <> → <a href="#" onClick={go("Crew")} className="underline decoration-amber-400/60 hover:text-white">⑤ the agents (Crew)</a></>}.{" "}
      Want yours? <a href="https://app.sokkan.ch" target="_blank" rel="noopener" className="font-semibold underline decoration-amber-400 hover:text-white">14-day trial</a> ·{" "}
      <a href="https://sokkan.ch/install.sh" className="underline decoration-amber-400/60 hover:text-white">self-host free</a>
      <button onClick={() => { try { localStorage.setItem("sokkan_demo_banner", "off"); } catch { /* private mode */ } setHidden(true); }}
        className="absolute right-2 top-1.5 rounded px-1.5 text-amber-300/70 hover:text-white" title="hide">✕</button>
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

function ProfileMenuItem({ onOpen }: { onOpen: () => void }) {
  const [st, setSt] = useState<{ configured: boolean; mode: string } | null>(null);
  useEffect(() => { llmStatus().then(setSt).catch(() => {}); }, []);
  const warn = st && !st.configured;
  return (
    <button onClick={onOpen} className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12.5px] text-slate-200 hover:bg-panel2">
      Profile & organization
      {warn && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-amber-400" title="model not configured" />}
    </button>
  );
}

function Identity() {
  const me = useMe();
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState(false);
  const color: Record<string, string> = {
    owner: "text-brass", admin: "text-sea", dev: "text-emerald-400", viewer: "text-mut",
  };
  if (!me) return <span className="ml-auto text-[11px] text-mut">the helm, not the autopilot</span>;
  return (
    <div className="relative ml-auto flex items-center gap-2 text-[11px] text-mut">
      <span className="hidden sm:inline">the helm, not the autopilot</span>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 rounded-full border border-line bg-panel2 px-2 py-0.5 hover:bg-line"
      >
        {me.name} · <span className={color[me.role] || "text-mut"}>{me.role}</span>
        <span className="text-mut">▾</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-50 mt-1.5 w-52 overflow-hidden rounded-lg border border-line bg-panel shadow-xl">
            <div className="border-b border-line px-3 py-2">
              <div className="truncate text-[12px] text-slate-200">{me.name}</div>
              <div className="truncate text-[10.5px] text-mut">{me.email}</div>
            </div>
            <ProfileMenuItem onOpen={() => { setOpen(false); setSettings(true); }} />
          </div>
        </>
      )}
      {settings && <Profile onClose={() => setSettings(false)} />}
    </div>
  );
}
