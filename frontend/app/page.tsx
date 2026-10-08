"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Tabs from "@/components/Tabs";
import SessionRail from "@/components/SessionRail";
import ChatPane from "@/components/ChatPane";
import AgentChatPane from "@/components/AgentChatPane";
import Board from "@/components/Board";
import Preview from "@/components/Preview";
import Corthexis from "@/components/Corthexis";
import Infra from "@/components/Infra";
import Operate from "@/components/Operate";
import Crew from "@/components/Crew";
import Helm from "@/components/Helm";
import Journal from "@/components/Journal";
import Assistant from "@/components/Assistant";
import Costs from "@/components/Costs";
import Setup from "@/components/Setup";
import { MeProvider, useCan, useMe } from "@/lib/me";
import { FeaturesProvider, useFeatures } from "@/lib/features";
import { fetchSessions } from "@/lib/api";
import { currentProject, installProjectFetch, noteTab } from "@/lib/project";
import { helmAccess } from "@/lib/helm";
import { navLast, navRemember } from "@/lib/nav";
import {
  PLANES, PLANE_OF, href, landingPlane, pickTab, planeForKey, resolveTarget, visiblePlanes,
  type PlaneId, type SubTab, type Target,
} from "@/lib/planes";

// 3.2 : chaque appel /api porte le projet sélectionné (en-tête x-sokkan-project) —
// installé avant le premier fetch (identité, features…)
installProjectFetch();

const DENSITIES = [1, 2, 3, 4];

interface OpenPane {
  id: string;
  kind: "sdk" | "tmux";
  title?: string;
  tag?: string;
}

export default function Home() {
  return (
    <FeaturesProvider>
    <MeProvider>
      <Cockpit />
    </MeProvider>
    </FeaturesProvider>
  );
}

/** Where the person is: plane + sub-tab (+ an Organization section from a deep link). */
interface Place { plane: PlaneId | null; tab: SubTab | null; section?: string }

const TABS_KEY = "sokkan_plane_tabs";

function Cockpit() {
  const feats = useFeatures();
  const me = useMe();
  const canDev = useCan("dev");
  // the deep link asked for (?plane= / ?tab=, new or legacy) — read once, at mount
  const [asked] = useState<Target | null>(() =>
    typeof window === "undefined" ? null : resolveTarget(new URLSearchParams(window.location.search)));
  const [place, setPlace] = useState<Place>(() => asked ? { plane: asked.plane, tab: asked.tab, section: asked.section } : { plane: null, tab: null });
  // the last sub-tab of each plane (per browser): coming back to a plane opens it again
  const lastTabs = useRef<Partial<Record<PlaneId, SubTab>>>({});
  useEffect(() => {
    try { lastTabs.current = JSON.parse(localStorage.getItem(TABS_KEY) || "{}"); } catch { /* private mode */ }
  }, []);

  // 3.3 Helm : the sub-tab exists for the people who steer at least one project
  const [steers, setSteers] = useState(false);
  // 3.2.2 Captains demo: a member reads Helm without steering it
  const [helmRO, setHelmRO] = useState(false);
  const [helmFor, setHelmFor] = useState<boolean | null>(null);
  useEffect(() => {
    const h = !!feats.helm;
    if (!h) { setSteers(false); setHelmRO(false); setHelmFor(false); return; }
    helmAccess().then((a) => { setSteers(a.steers.length > 0); setHelmRO(!!a.read_only && (a.reads || []).length > 0); })
      .catch(() => { setSteers(false); setHelmRO(false); })
      .finally(() => setHelmFor(true));
  }, [feats.helm]);
  // the plane this person was on last (kept per user by the API)
  const [last, setLast] = useState<string | null | undefined>(undefined);
  useEffect(() => { navLast().then(setLast); }, []);

  const planes = visiblePlanes({ f: feats, project: currentProject(), canDev, ops: me?.ops, steers: steers || helmRO });
  // availability is known only once features, Helm access and the remembered plane answered —
  // never decide on the loading defaults
  const settled = !!feats.loaded && helmFor === !!feats.helm && last !== undefined;
  const instanceAdmin = ["admin", "owner"].includes(me?.instance_role || "");

  // land, or fall back when the place on screen does not exist here (project switch,
  // rights changed, feature off): same plane if it is there, else the landing plane
  useEffect(() => {
    if (!settled) return;
    const plane = place.plane && planes.some((p) => p.id === place.plane) ? place.plane
      : landingPlane({ instanceAdmin, projectRole: me?.project_role, steers, ops: me?.ops, canDev, last }, planes);
    const tab = pickTab(plane, place.plane === plane ? (place.tab ?? lastTabs.current[plane]) : lastTabs.current[plane], planes);
    if (plane !== place.plane || tab !== place.tab) {
      // a deep link to a place this person does not have: say so instead of switching silently
      if (asked?.tab && place.tab === asked.tab && tab !== asked.tab && !missed) {
        const label = PLANES.flatMap((x) => x.tabs).find((x) => x.id === asked.tab)?.label ?? asked.tab;
        setMissed(`${label} is not available to you here (role, team or feature) — showing what is.`);
      }
      setPlace((cur) => ({ ...cur, plane, tab }));
    }
  }); // eslint-disable-line react-hooks/exhaustive-deps
  const [missed, setMissed] = useState("");

  useEffect(() => {
    if (!place.plane || !place.tab) return;
    noteTab(place.plane, place.tab);
    lastTabs.current[place.plane] = place.tab;
    try { localStorage.setItem(TABS_KEY, JSON.stringify(lastTabs.current)); } catch { /* private mode */ }
  }, [place.plane, place.tab]);
  const remembered = useRef<string | null>(null);
  useEffect(() => {
    if (!settled || !place.plane || remembered.current === place.plane) return;
    // only an instance admin lands on their last plane: nobody else's choice is written
    // (and the public demo's visitor writes nothing — it was a 403 on every page)
    if (instanceAdmin && (remembered.current !== null || place.plane !== last)) navRemember(place.plane);
    remembered.current = place.plane;
  }, [settled, place.plane, last, instanceAdmin]);

  const goPlane = useCallback((p: PlaneId) => setPlace({ plane: p, tab: lastTabs.current[p] ?? null }), []);
  const goTab = useCallback((t: SubTab, section?: string) => setPlace({ plane: PLANE_OF[t], tab: t, section }), []);

  // keyboard: g then c/b/o/s = plane · 1–9 = sub-tab (never while typing)
  const gPending = useRef(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      if (Date.now() - gPending.current < 1500) {
        gPending.current = 0;
        const p = planeForKey(e.key, planes);
        if (p) { e.preventDefault(); goPlane(p); }
        return;
      }
      if (e.key === "g") { gPending.current = Date.now(); return; }
      if (/^[1-9]$/.test(e.key)) {
        const cur = planes.find((p) => p.id === place.plane);
        const t = cur?.tabs[Number(e.key) - 1];
        if (t) { e.preventDefault(); setPlace({ plane: cur!.id, tab: t.id }); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [planes, place.plane, goPlane]);

  // nothing is mounted before the person's rights are known (a panel they do not have would
  // fetch, get a 403, then vanish)
  const tab = settled && place.tab && planes.some((x) => x.tabs.some((t) => t.id === place.tab)) ? place.tab : null;
  const [open, setOpen] = useState<OpenPane[]>([]);
  const [cols, setCols] = useState(2);

  // liens Operate ⇄ Crew (3.1.1) : l'URL porte la cible, le composant la lit au montage
  const goDeep = (t: SubTab, params: Record<string, string>) => {
    try { window.history.pushState(null, "", href(t, params)); } catch { /* no history API */ }
    goTab(t);
  };
  const openAgent = (agentId: number, runId?: number) =>
    goDeep("crew", { agent: String(agentId), ...(runId ? { run: String(runId) } : {}) });
  const openIncident = (id: number) => goDeep("incidents", { incident: String(id) });

  const close = (id: string) => setOpen((cur) => cur.filter((x) => x.id !== id));

  // ouvre un pane ; si le kind n'est pas connu (ex. « ouvrir » depuis une vieille
  // carte du board), on le résout via /api/sessions
  const openSession = async (s: { session_id: string; kind?: "sdk" | "tmux"; title?: string; tag?: string }) => {
    let { kind, title, tag } = s;
    if (!kind) {
      try {
        const all = await fetchSessions();
        const found = all.find((x) => x.session_id === s.session_id);
        kind = (found?.kind as "sdk" | "tmux") ?? "tmux";
        title = title ?? found?.title;
        tag = tag ?? found?.tag;
      } catch { kind = "tmux"; }
    }
    setOpen((cur) => (cur.some((x) => x.id === s.session_id)
      ? cur
      : [...cur, { id: s.session_id, kind: kind!, title, tag }]));
    goTab("sessions");
  };

  const toggle = (s: { session_id: string; kind?: "sdk" | "tmux"; title?: string; tag?: string }) => {
    if (open.some((x) => x.id === s.session_id)) close(s.session_id);
    else openSession(s);
  };

  return (
    <>
    <Assistant tab={tab ?? ""} />
    <div className="flex h-screen flex-col">
      <Tabs planes={planes} plane={place.plane} tab={tab} onPlane={goPlane} onTab={(t) => goTab(t)} onGo={goTab} />
      <div id="cockpit-panel" role="tabpanel" aria-labelledby={tab ? `tab-${tab}` : undefined} className="flex min-h-0 flex-1 flex-col">
      {missed && (
        <div role="status" className="flex items-center gap-2 border-b border-line bg-panel2/60 px-4 py-1.5 text-[12px] text-mut">
          <span aria-hidden>ⓘ</span>{missed}
          <button onClick={() => setMissed("")} aria-label="dismiss" className="ui-focus ml-auto flex h-6 w-6 items-center justify-center rounded hover:text-slate-200">✕</button>
        </div>
      )}
      {!tab ? (
        <div className="flex flex-1 items-center justify-center text-[13px] text-mut">…</div>
      ) : tab === "board" ? (
        <Board onOpenSession={(sid) => openSession({ session_id: sid })} />
      ) : tab === "preview" ? (
        <Preview />
      ) : tab === "corthexis" ? (
        <Corthexis onOpenSession={(sid) => openSession({ session_id: sid })} />
      ) : tab === "infra" ? (
        <Infra />
      ) : tab === "helm" ? (
        <Helm onOpenSession={(sid) => openSession({ session_id: sid })} readOnly={helmRO && !steers} />
      ) : tab === "crew" ? (
        <Crew onOpenSession={(sid) => openSession({ session_id: sid })} onOpenIncident={openIncident} />
      ) : tab === "incidents" ? (
        <Operate onOpenSession={(sid) => openSession({ session_id: sid })} onOpenAgent={openAgent} />
      ) : tab === "journal" ? (
        <Journal />
      ) : tab === "costs" ? (
        <Costs />
      ) : PLANE_SETUP.includes(tab) ? (
        <Setup key={tab} tab={tab} section={place.section} />
      ) : (
        <div className="flex min-h-0 flex-1">
          {/* mobile : rail plein écran tant qu'aucun pane n'est ouvert, masqué sinon */}
          <div className={open.length ? "hidden md:contents" : "contents"}>
            <SessionRail
              open={open.map((x) => x.id)}
              onOpen={toggle}
              onDelete={close}
            />
          </div>
          <main className={`min-h-0 flex-1 flex-col ${open.length === 0 ? "hidden md:flex" : "flex"}`}>
            <div className="flex items-center border-b border-line bg-panel/60 px-3 py-2 md:hidden">
              <button onClick={() => setOpen([])} className="text-[13px] font-medium text-slate-200">
                ← sessions
              </button>
            </div>
            <div className="hidden items-center gap-2 border-b border-line bg-panel/60 px-3 py-1.5 text-[11px] text-mut md:flex">
              <span>{open.length} window(s)</span>
              <span className="ml-auto">density</span>
              {DENSITIES.map((d) => (
                <button
                  key={d}
                  onClick={() => setCols(d)}
                  className={`rounded px-1.5 ${cols === d ? "bg-panel2 text-slate-200 ring-1 ring-line" : "hover:bg-panel2"}`}
                >{d}</button>
              ))}
            </div>
            {open.length === 0 ? (
              <div className="flex flex-1 items-center justify-center text-[13px] text-mut">
                ← pick a session in the rail (or spawn a task from the Board)
              </div>
            ) : (
              <div
                className="grid min-h-0 flex-1 gap-2 overflow-auto p-2 max-md:!grid-cols-1"
                style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
              >
                {open.map((p) =>
                  p.kind === "sdk" ? (
                    <AgentChatPane key={p.id} sid={p.id} title={p.title} tag={p.tag} onClose={close} />
                  ) : (
                    <ChatPane key={p.id} id={p.id} onClose={(id) => close(id)} />
                  )
                )}
              </div>
            )}
          </main>
        </div>
      )}
      </div>
    </div>
    </>
  );
}

const PLANE_SETUP: SubTab[] = ["organization", "engines", "magnitude", "secrets", "account", "notifications"];
