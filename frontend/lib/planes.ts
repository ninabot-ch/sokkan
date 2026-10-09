// 3.2.2 — the cockpit navigation by PLANES. One bar of four planes, each with its
// sub-tabs; a sub-tab that is not there (feature off, role too low) disappears, and a plane
// with no visible sub-tab disappears. Pure module (no React, no import): the tab bar, the
// page and tests/nav/planes_test.ts all use it.
//
// Deep links: new form `/?plane=build&tab=crew`; EVERY older `/?tab=<name>` (notifications,
// Crew ⇄ Operate links, the demo banner, Nina's cards, old Profile sections) still opens
// the right place through LEGACY below.

export type PlaneId = "control" | "build" | "operate" | "setup";
export type SubTab =
  | "helm" | "board" | "corthexis"
  | "sessions" | "crew" | "preview"
  | "incidents" | "alerts" | "infra" | "costs" | "journal"
  | "organization" | "engines" | "magnitude" | "secrets" | "account" | "notifications";

export interface PlaneDef {
  id: PlaneId;
  label: string;
  /** second key of the `g <key>` shortcut */
  key: string;
  blurb: string;
  tabs: { id: SubTab; label: string }[];
}

export const PLANES: PlaneDef[] = [
  { id: "control", label: "Control", key: "c", blurb: "steer: Helm, the board, the memory",
    tabs: [{ id: "helm", label: "Helm" }, { id: "board", label: "Board" }, { id: "corthexis", label: "CortHeXis" }] },
  { id: "build", label: "Build", key: "b", blurb: "do: sessions, agents, previews",
    tabs: [{ id: "sessions", label: "Sessions" }, { id: "crew", label: "Crew" }, { id: "preview", label: "Preview" }] },
  { id: "operate", label: "Operate", key: "o", blurb: "run: incidents and alerts, infra, costs, journal",
    tabs: [{ id: "incidents", label: "Incidents" }, { id: "alerts", label: "Alerts" }, { id: "infra", label: "Infra" }, { id: "costs", label: "Costs" },
      { id: "journal", label: "Journal" }] },
  { id: "setup", label: "Setup", key: "s", blurb: "configure: organization, engines, GPUs, secrets, you",
    tabs: [{ id: "organization", label: "Organization" }, { id: "engines", label: "Engines" },
      { id: "magnitude", label: "Magnitude" }, { id: "secrets", label: "Secrets" },
      { id: "account", label: "My account" }, { id: "notifications", label: "Notifications" }] },
];

export const PLANE_OF: Record<SubTab, PlaneId> = Object.fromEntries(
  PLANES.flatMap((p) => p.tabs.map((t) => [t.id, p.id])),
) as Record<SubTab, PlaneId>;

/** Sections of Setup › Organization (the former Profile sections). */
export type OrgSection = "org" | "members" | "projects" | "classification" | "teams" | "features";

/** Every `?tab=` value that ever existed (11 tabs + the Profile sections) → where it lives now. */
export const LEGACY: Record<string, { tab: SubTab; section?: string }> = {
  // the 11 tabs of 3.2.1
  board: { tab: "board" }, sessions: { tab: "sessions" }, crew: { tab: "crew" }, helm: { tab: "helm" },
  preview: { tab: "preview" }, corthexis: { tab: "corthexis" }, costs: { tab: "costs" },
  magnitude: { tab: "magnitude" }, infra: { tab: "infra" }, incidents: { tab: "incidents" },
  // 3.2.2: the sub-tab « Operate › Operate » became « Operate › Incidents »; old links keep working
  operate: { tab: "incidents" }, incident: { tab: "incidents" },
  // 3.5: Operate › Alerts (the rules engine) — `?tab=alerts` pointed at Incidents before it existed
  alerts: { tab: "alerts" }, alert: { tab: "alerts" }, alerting: { tab: "alerts" }, rules: { tab: "alerts" },
  journal: { tab: "journal" },
  // the Profile & organization dialog of 3.2.1
  profile: { tab: "account" }, account: { tab: "account" }, linked: { tab: "account", section: "linked" },
  org: { tab: "organization", section: "org" }, organization: { tab: "organization", section: "org" },
  organisation: { tab: "organization", section: "org" }, members: { tab: "organization", section: "members" },
  projects: { tab: "organization", section: "projects" }, teams: { tab: "organization", section: "teams" },
  classification: { tab: "organization", section: "classification" },
  features: { tab: "organization", section: "features" },
  model: { tab: "engines" }, keys: { tab: "engines" }, "model-keys": { tab: "engines" },
  "connect-ai": { tab: "engines" }, connect: { tab: "engines" }, engines: { tab: "engines" },
  secrets: { tab: "secrets" }, vault: { tab: "secrets" },
  notify: { tab: "notifications" }, notifications: { tab: "notifications" },
};

export interface Target { plane: PlaneId; tab: SubTab | null; section?: string }

const isPlane = (x: string): x is PlaneId => PLANES.some((p) => p.id === x);

/** Reads a deep link. `?plane=` (new) and/or `?tab=` (new or legacy), case-insensitive;
 *  `?forge=` (back from the GitLab consent) opens Setup › My account › Linked accounts.
 *  null = nothing asked (the person lands on their plane). */
export function resolveTarget(q: { get(k: string): string | null; has?(k: string): boolean }): Target | null {
  const plane = (q.get("plane") || "").toLowerCase();
  const tab = (q.get("tab") || "").toLowerCase();
  const section = (q.get("section") || "").toLowerCase() || undefined;
  const hit = LEGACY[tab];
  if (hit) {
    const p = PLANE_OF[hit.tab];
    // ?plane=X&tab=Y where Y lives elsewhere: the tab wins (it is the precise part)
    return { plane: p, tab: hit.tab, section: section || hit.section };
  }
  if (isPlane(plane)) return { plane, tab: null, section };
  if (q.get("forge") !== null && q.get("forge") !== undefined) return { plane: "setup", tab: "account", section: "linked" };
  return null;
}

/** The query string of a place (used by goDeep, switchProject, the links Nina writes). */
export function href(tab: SubTab, params: Record<string, string> = {}): string {
  return `/?${new URLSearchParams({ plane: PLANE_OF[tab], tab, ...params })}`;
}

export interface VisCtx {
  /** feature flags as /api/features serves them */
  f: { preview?: boolean; infra?: boolean; observe?: boolean; magnitude?: boolean; agents?: boolean;
    agents_viewer_readonly?: boolean; helm?: boolean; alerting?: boolean };
  /** selected project slug */
  project: string;
  /** dev or more in the selected project */
  canDev: boolean;
  /** member of the ops team (or instance admin); undefined = not known yet → shown */
  ops?: boolean;
  /** steers at least one project (Helm) */
  steers: boolean;
}

/** Is this sub-tab there for this person, here? (the rules of 3.2.1, unchanged) */
export function tabVisible(t: SubTab, c: VisCtx): boolean {
  const opsOk = c.ops !== false;
  switch (t) {
    case "preview": return !!c.f.preview && c.project === "default";
    case "infra": return !!c.f.infra && opsOk;
    case "incidents": return !!c.f.observe && opsOk;
    // 3.5: the alert rules work without any observability stack (SOKKAN's own figures, Elasticsearch…)
    // and belong to the PROJECT (viewer reads, dev writes its rules — the API enforces it): a team
    // that owns a service watches it without being in the instance's ops team
    case "alerts": return !!c.f.alerting;
    case "magnitude": return !!c.f.magnitude;
    case "crew": return !!c.f.agents && (c.canDev || !!c.f.agents_viewer_readonly);
    case "helm": return !!c.f.helm && c.steers;
    default: return true;
  }
}

/** The planes this person sees, each with its visible sub-tabs; empty planes removed. */
export function visiblePlanes(c: VisCtx): PlaneDef[] {
  return PLANES.map((p) => ({ ...p, tabs: p.tabs.filter((t) => tabVisible(t.id, c)) }))
    .filter((p) => p.tabs.length > 0);
}

export interface LandCtx {
  /** instance admin or owner */
  instanceAdmin: boolean;
  /** role in the selected project (viewer | dev | maintainer | admin) */
  projectRole?: string | null;
  steers: boolean;
  ops?: boolean;
  canDev: boolean;
  /** the plane this person was on last time (kept per user by the API) */
  last?: string | null;
}

/** Landing plane by role: admin → their last plane · manager/maintainer → Control ·
 *  ops team → Operate · dev → Build · otherwise Build; always a plane that is visible. */
export function landingPlane(c: LandCtx, visible: PlaneDef[]): PlaneId {
  const has = (p: string): p is PlaneId => visible.some((v) => v.id === p);
  const want: string[] = [];
  if (c.instanceAdmin) want.push(c.last || "", "control");
  if (c.steers || c.projectRole === "maintainer" || c.projectRole === "admin") want.push("control");
  if (c.ops === true) want.push("operate");
  if (c.canDev) want.push("build");
  want.push("build", "control");
  return (want.find(has) as PlaneId | undefined) ?? visible[0]?.id ?? "build";
}

/** The sub-tab to show in a plane: the asked one if visible, else the plane's first. */
export function pickTab(plane: PlaneId, want: SubTab | null | undefined, visible: PlaneDef[]): SubTab | null {
  const p = visible.find((x) => x.id === plane);
  if (!p) return null;
  return want && p.tabs.some((t) => t.id === want) ? want : p.tabs[0].id;
}

/** Keyboard: `g` then a plane letter; `1`…`9` = the n-th sub-tab of the plane on screen. */
export function planeForKey(k: string, visible: PlaneDef[]): PlaneId | null {
  return visible.find((p) => p.key === k.toLowerCase())?.id ?? null;
}

export const SHORTCUTS_HELP =
  "Keyboard: g then c / b / o / s = Control / Build / Operate / Setup · 1–9 = sub-tab of the current plane";
