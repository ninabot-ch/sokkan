"use client";
// Simple profile — the shell: a short drawer (Home / My work / Documents / Memory / My agent crew +
// recent works with the last sentence + today's usage), the current view, Nina and Settings.
// Structure borrowed from Nokker (drawer + thread + ONE composer); the brand is SOKKAN's.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, fetchHome, fetchMyProjects, newRequestId, savePrefs, startWork, type ClaudeGate, type ProjectRow, type HomeData, type Prefs, type Recent, type TaskTemplate } from "@/lib/simple/api";
import { SimpleUIProvider, useT, useUI, type LangPref, type ThemePref } from "@/lib/simple/i18n";
import { useFeatures } from "@/lib/features";
import { useMe } from "@/lib/me";
import { currentProject, switchProject } from "@/lib/project";
import { saveDemoPrefs } from "@/lib/simple/demo";
import BannerSlot from "./BannerSlot";
import { DemoHome } from "./DemoBand";
import IngestReview from "./IngestReview";
import Memory from "./Memory";
import { openStudyConsent } from "./StudyConsent";
import { ingestStatus, quarantineList } from "@/lib/simple/ingest";
import Composer from "./Composer";
import Icon, { templateIcon, type IconName } from "./icons";
import Nina from "./Nina";
import { RolePicker, TemplateDetail } from "./Roles";
import { groupTemplates, localOnly } from "@/lib/simple/roles";
import { Documents, Team, Works } from "./Pages";
import Settings from "./Settings";
import Thread from "./Thread";
import SetupWizard from "./onboarding/SetupWizard";
import Welcome from "./onboarding/Welcome";
import { TeamPage, UsagePage, ValidationsPage } from "./onboarding/AdminPages";
import { fetchOnboarding, progress, type Onboarding } from "@/lib/simple/onboarding";
import { useOT } from "@/lib/simple/dictOnboarding";

// 4.0: « setup » (the administrator's first launch) and the administration pages
type AdminView = "setup" | "admin_team" | "admin_usage" | "admin_validations";
const ADMIN_VIEWS: AdminView[] = ["setup", "admin_team", "admin_usage", "admin_validations"];
type View = { name: "home" | "works" | "documents" | "memory" | "team" | "review" | AdminView } | { name: "work"; sid: string };

function readView(): View {
  if (typeof window === "undefined") return { name: "home" };
  const q = new URLSearchParams(window.location.search);
  const w = q.get("w");
  if (w && /^[0-9a-f]{8,64}$/.test(w)) return { name: "work", sid: w };
  const v = q.get("v");
  if (ADMIN_VIEWS.includes(v as AdminView)) return { name: v as AdminView };
  return v === "works" || v === "documents" || v === "memory" || v === "team" || v === "review" ? { name: v } : { name: "home" };
}

function hrefOf(v: View): string {
  const q = new URLSearchParams(window.location.search);
  q.delete("v"); q.delete("w");
  if (v.name === "work") q.set("w", v.sid); else if (v.name !== "home") q.set("v", v.name);
  const s = q.toString();
  return s ? `/?${s}` : "/";
}

export default function SimpleApp({ prefs, onPrefs }: { prefs: Prefs; onPrefs: (p: Prefs) => void }) {
  const demo = !!useFeatures().demo_atelier;
  // 3.6.7 public demo: one identity for every visitor — their language and theme stay in their browser
  const save = useCallback((p: { lang?: LangPref; theme?: ThemePref }) => {
    if (demo) { saveDemoPrefs(p); return; }
    savePrefs(p).then(onPrefs).catch(() => {});
  }, [onPrefs, demo]);
  return (
    <SimpleUIProvider initialLang={prefs.lang} initialTheme={prefs.theme} onChange={save}>
      <Shell prefs={prefs} onPrefs={onPrefs} />
    </SimpleUIProvider>
  );
}

function Shell({ prefs, onPrefs }: { prefs: Prefs; onPrefs: (p: Prefs) => void }) {
  const t = useT();
  const ot = useOT();
  const { lang, setLangPref } = useUI();
  const me = useMe();
  const feats = useFeatures();
  const [view, setView] = useState<View>(readView);
  // 4.0 — first launch (administrator) and first sign-in (person): backend/onboarding.py
  const [ob, setOb] = useState<Onboarding | null>(null);
  const [welcome, setWelcome] = useState(false);
  const autoSetup = useRef(false);
  const [obTried, setObTried] = useState(false);
  const [home, setHome] = useState<HomeData | null>(null);
  const [homeFailed, setHomeFailed] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [settings, setSettings] = useState(false);
  const [nina, setNina] = useState(false);
  // 4.0 job kits: the picker, offered once at the first sign-in, then from Settings
  const [picker, setPicker] = useState<"first" | "change" | null>(null);
  const offered = useRef(false);
  const main = useRef<HTMLElement>(null);
  // « Notes to review » (feature `doc_ingest`): there for the people who may review the
  // batches of this project — the API says so (403 / 404 otherwise), with how many wait
  const ingestOn = !!feats.registry?.items.find((i) => i.id === "doc_ingest")?.enabled;
  const [review, setReview] = useState<number | null>(null);
  const demo = !!feats.demo_atelier;
  // a viewer reviews nothing: not even asked (it answered 403 on every screen — 3.6.7 demo audit)
  const reader = me?.project_role === "viewer";
  // 3.6.7: the screen also holds the notes a work asked to keep (quarantine) — there for a
  // contributor even when the ingestion is off; the count adds both
  useEffect(() => {
    if (!me || reader) { setReview(null); return; }
    const batches = ingestOn ? ingestStatus().then((s) => s.pending_batches) : Promise.reject(new Error("off"));
    Promise.allSettled([batches, quarantineList()]).then(([b, q]) => {
      if (b.status === "rejected" && q.status === "rejected") { setReview(null); return; }
      setReview((b.status === "fulfilled" ? b.value : 0) + (q.status === "fulfilled" ? q.value.length : 0));
    });
  }, [ingestOn, home?.project, me, reader]);

  const loadOb = useCallback(async () => {
    try { setOb(await fetchOnboarding(lang)); } catch { /* the Atelier works without it */ }
    setObTried(true);
  }, [lang]);
  // ONE choice of job at the first sign-in: the onboarding's (the welcome of a person, the first
  // work of the setup) when it will be shown — it writes the job kits' record —, else the job
  // kits' picker (an instance already set up, or the onboarding unreachable)
  const onboardingAsks = !!ob && ((ob.admin && !!ob.setup?.needed) || (!ob.admin && ob.me.needs_welcome));
  useEffect(() => {
    if (home?.ask_roles && !demo && home.can_start && obTried && !onboardingAsks && !welcome && !ob?.me.role_id
      && !offered.current) { offered.current = true; setPicker("first"); }
  }, [home?.ask_roles, home?.can_start, demo, obTried, onboardingAsks, welcome, ob?.me.role_id]);
  useEffect(() => { if (!demo) loadOb(); }, [loadOb, demo]);
  useEffect(() => {
    if (!ob || autoSetup.current) return;
    autoSetup.current = true;
    const named = new URLSearchParams(window.location.search);
    // an administrator whose setup is not finished lands on it — unless the address names a page
    if (ob.admin && ob.setup?.needed && !named.has("v") && !named.has("w")) setView({ name: "setup" });
    if (!ob.admin && ob.me.needs_welcome && !named.has("w")) setWelcome(true);
  }, [ob]);

  const loadHome = useCallback(() => {
    fetchHome(lang).then((h) => { setHome(h); setHomeFailed(false); }).catch(() => setHomeFailed(true));
  }, [lang]);
  useEffect(() => { loadHome(); const id = window.setInterval(loadHome, 10000); return () => window.clearInterval(id); }, [loadHome]);

  const go = useCallback((v: View, replace = false) => {
    setView(v); setDrawer(false);
    try { window.history[replace ? "replaceState" : "pushState"](null, "", hrefOf(v)); } catch { /* no history API */ }
    main.current?.focus();
  }, []);
  useEffect(() => {
    const onPop = () => setView(readView());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  useEffect(() => {
    const titles = { home: "nav.home", works: "nav.works", documents: "nav.documents", memory: "nav.memory", team: "nav.team", review: "nav.review" } as const;
    const admin = { setup: "ob.nav.setup", admin_team: "ob.nav.team", admin_usage: "ob.nav.usage", admin_validations: "ob.nav.validations" } as const;
    const work = view.name === "work" ? home?.recents.find((r) => r.sid === view.sid)?.title : "";
    if (view.name === "work" && !work) return;   // named once the work is known, never « My work » meanwhile
    document.title = `${view.name === "work" ? work : view.name in admin ? ot(admin[view.name as AdminView])
      : t(titles[view.name as keyof typeof titles])} — SOKKAN`;
  }, [view, home, t, ot]);
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setDrawer(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);

  const nav: { id: "home" | "works" | "documents" | "memory" | "team" | "review"; icon: IconName; count?: number }[] = [
    { id: "home", icon: "home" }, { id: "works", icon: "works", count: home?.works_total || undefined },
    { id: "documents", icon: "page" }, { id: "memory", icon: "memory" }, { id: "team", icon: "crew" },
    ...(review !== null ? [{ id: "review" as const, icon: "seal" as const, count: review || undefined }] : []),
  ];
  const pct = home?.usage.pct;
  const name = demo ? t("demo.visitor") : (me?.name && !me.name.includes("@") ? me.name : "") || me?.email || "";

  return (
    <div className="sk">
      <a href="#sk-main" className="sk-skip">{t("skip")}</a>
      {drawer && <div className="sk-drawer-scrim" onClick={() => setDrawer(false)} aria-hidden />}
      <nav className={`sk-drawer${drawer ? " is-open" : ""}`} aria-label={t("nav.main")}>
        <div className="sk-brand">
          <Brand />
          <button className="sk-btn sk-btn-quiet sk-btn-icon sk-push sk-only-narrow" onClick={() => setDrawer(false)} aria-label={t("nav.close")}><Icon name="close" /></button>
        </div>
        <ProjectPicker last={prefs.project || ""} only={demo ? feats.demo_atelier_projects || [] : null} />
        <div className="sk-nav">
          {nav.map((n) => (
            <button key={n.id} className="sk-nav-item" aria-current={view.name === n.id ? "page" : undefined} onClick={() => go({ name: n.id })}>
              <Icon name={n.icon} />{t(`nav.${n.id}`)}
              {n.count ? <span className="sk-nav-count">{n.count}</span> : null}
            </button>
          ))}
        </div>
        {ob?.admin && !demo && <AdminNav ob={ob} view={view.name} go={(v) => go({ name: v })} />}
        {home && home.recents.length > 0 && (
          <>
            <div className="sk-drawer-label" id="sk-recents">{t("nav.recents")}</div>
            <div className="sk-recents" role="list" aria-labelledby="sk-recents">
              {home.recents.map((r) => (
                <div role="listitem" key={r.sid}>
                  <button className="sk-recent" aria-current={view.name === "work" && view.sid === r.sid ? "page" : undefined}
                    onClick={() => go({ name: "work", sid: r.sid })}>
                    <span className="sk-recent-title">{r.title}</span>
                    <RecentLine r={r} />
                  </button>
                </div>
              ))}
            </div>
          </>
        )}
        <div className="sk-drawer-foot">
          {typeof pct === "number" && (
            <div className="sk-gauge-box">
              <span id="sk-gauge-l">{t(pct >= 80 ? "gauge.high" : "gauge.label", { pct: Math.round(pct) })}</span>
              <div className={`sk-gauge${pct >= 80 ? " is-high" : ""}`} role="progressbar" aria-labelledby="sk-gauge-l"
                aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}><i style={{ width: `${Math.max(2, pct)}%` }} /></div>
            </div>
          )}
          {home?.nina && (
            <button className="sk-nav-item" onClick={() => { setNina(true); setDrawer(false); }}><Icon name="chat" />{t("nina.open")}</button>
          )}
          <button className="sk-user" onClick={() => { setSettings(true); setDrawer(false); }} aria-haspopup="dialog">
            <span className="sk-avatar" aria-hidden>{(name[0] || "?").toUpperCase()}</span>
            <span className="sk-user-name sk-grow">{name}</span>
            <Icon name="gear" label={t("settings.open")} />
          </button>
        </div>
      </nav>

      <main className="sk-main" id="sk-main" ref={main} tabIndex={-1}>
        <div className="sk-topbar">
          <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={() => setDrawer(true)} aria-label={t("nav.menu")} aria-expanded={drawer}><Icon name="menu" /></button>
          <Brand />
          {home?.nina && <button className="sk-btn sk-btn-quiet sk-btn-icon sk-push" onClick={() => setNina(true)} aria-label={t("nina.open")}><Icon name="chat" /></button>}
        </div>
        <BannerSlot />
        {view.name === "home" && (demo && home && !home.can_start
          ? <DemoHome home={home} onOpen={(sid) => go({ name: "work", sid })} />
          : <Home home={home} failed={homeFailed} retry={loadHome}
              top={ob?.admin && ob.setup?.needed ? <ResumeSetup ob={ob} onResume={() => go({ name: "setup" })} /> : null}
              onStarted={(sid) => { loadHome(); go({ name: "work", sid }); }} />)}
        {view.name === "setup" && ob?.admin && <SetupWizard ob={ob} reload={loadOb} setLangPref={setLangPref}
          onClose={() => { loadOb(); go({ name: "home" }); }} />}
        {view.name === "admin_team" && ob?.admin && <TeamPage />}
        {view.name === "admin_usage" && ob?.admin && <UsagePage />}
        {view.name === "admin_validations" && ob?.admin && <ValidationsPage />}
        {welcome && ob && <Welcome ob={ob} name={home?.first_name || ""} canStart={!!home?.can_start}
          onClose={() => { setWelcome(false); loadOb(); loadHome(); }}
          onStarted={(sid) => { setWelcome(false); loadHome(); go({ name: "work", sid }); }} />}
        {view.name === "works" && <Works onOpen={(sid) => go({ name: "work", sid })} onHome={() => go({ name: "home" })} />}
        {view.name === "documents" && <Documents canAdd={!!home?.can_start} />}
        {view.name === "memory" && <Memory />}
        {view.name === "team" && <Team enabled={!!feats.agents} demo={demo} hiddenForRole={reader && !feats.agents_viewer_readonly} />}
        {view.name === "review" && <IngestReview quarantine batchesOn={ingestOn} onCount={setReview} />}
        {view.name === "work" && <Thread key={view.sid} sid={view.sid} limits={home?.limits ?? null}
          onBack={() => go({ name: "works" })} onChanged={loadHome} />}
        {nina && <Nina onClose={() => setNina(false)} />}
      </main>

      {settings && <Settings prefs={prefs} onPrefs={onPrefs} onClose={() => setSettings(false)} project={home?.project || currentProject()} projectName={home?.project_name}
        jobs={demo ? null : home?.roles || []} onRoles={() => { setSettings(false); setPicker("change"); }} />}
      {picker && <RolePicker first={picker === "first"} onClose={() => setPicker(null)}
        onSaved={(p) => { onPrefs(p); loadHome(); }} />}
    </div>
  );
}

function Brand() {
  // the SOKKAN wordmark as everywhere else: S, the rudder for the O, K, the gradient K, AN
  return (
    <span className="sk-wordmark" role="img" aria-label="SOKKAN">
      <span className="g" aria-hidden>S</span>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/sokkan-icon.svg" alt="" />
      <span className="g" aria-hidden>K</span><span className="k" aria-hidden>K</span><span className="g" aria-hidden>AN</span>
    </span>
  );
}

function RecentLine({ r }: { r: Recent }) {
  const t = useT();
  if (r.state === "waiting") return <span className="sk-recent-line is-wait">{t("state.waiting")}</span>;
  if (r.state === "working") return <span className="sk-recent-line is-work">{t("th.working")}</span>;
  const line = r.last_line || (r.deliverables ? (r.validated ? t("state.validated") : t("state.done")) : "");
  return line ? <span className="sk-recent-line">{line}</span> : null;
}

function Home({ home, failed, retry, onStarted, top }: {
  home: HomeData | null; failed: boolean; retry: () => void; onStarted: (sid: string) => void; top?: React.ReactNode;
}) {
  const t = useT();
  const { lang } = useUI();
  const [tpl, setTpl] = useState<TaskTemplate | null>(null);
  const [focusKey, setFocusKey] = useState<string | undefined>(undefined);
  // one id per work asked: kept while the same request is tried again, so a second click
  // after a lost answer opens the work that did start instead of a twin
  const pending = useRef<{ sig: string; id: string } | null>(null);
  // 3.6.5: who writes this work — the model of the machine (default) or Claude, chosen here
  const [engine, setEngine] = useState<"local" | "claude">("local");
  const [cmodel, setCmodel] = useState("");
  if (!home) {
    return (
      <div className="sk-scroll"><div className="sk-page">
        {failed ? <div className="sk-empty" role="alert"><Icon name="alert" /><p>{t("home.failed")}</p><button className="sk-btn" onClick={retry}>{t("retry")}</button></div>
          : <><div className="sk-skel" aria-label={t("loading")} /><div className="sk-skel" /></>}
      </div></div>
    );
  }
  const can = home.can_start;
  // 4.0 job kits: a job that keeps its data on the machine never offers Claude
  const local = localOnly(tpl, home.project_role, home.roles);
  const gate = local ? null : home.claude || null;
  const groups = groupTemplates(home.playbooks, home.roles);
  const claudeOn = engine === "claude" && !!gate?.allowed;
  const model = cmodel || gate?.default_model || "";
  return (
    <div className="sk-scroll"><div className="sk-page">
      {top}
      <h1 className="sk-h1">{home.first_name ? t("home.hello", { name: home.first_name }) : t("home.hello.anon")}</h1>
      {!can && <div className="sk-notice" role="status"><Icon name="info" /><span>{t("home.viewer")}</span></div>}
      <Composer autoFocus disabled={!can} canAttach={!!home.limits} limits={home.limits}
        placeholder={tpl ? tpl.hint : t("home.placeholder")} submitLabel={t("home.launch")} busyLabel={t("home.launching")}
        allowEmptyWithDocs={!!tpl} emptyHint={t("home.need")} focusKey={focusKey}
        footer={gate ? <EngineChoice gate={gate} engine={engine} model={model} disabled={!can}
          onEngine={setEngine} onModel={setCmodel} /> : undefined}
        header={tpl ? (
          <>
            <div className="sk-template-on"><Icon name={templateIcon(tpl.id)} small />{t("home.template.on", { label: tpl.label })}
              <button className="sk-chip-x" style={{ marginRight: 0 }} onClick={() => setTpl(null)} aria-label={t("home.template.off")}><Icon name="close" small /></button></div>
            <TemplateDetail tpl={tpl} />
          </>
        ) : undefined}
        onSubmit={async (text, docs) => {
          const sig = JSON.stringify([text, tpl?.id || "", docs.map((d) => d.id), claudeOn ? model : ""]);
          if (pending.current?.sig !== sig) pending.current = { sig, id: newRequestId() };
          try {
            const r = await startWork(text, tpl?.id || "", docs.map((d) => d.id), lang, pending.current.id,
              claudeOn ? { model } : undefined);
            pending.current = null;
            setTpl(null);
            setEngine("local");
            onStarted(r.session_id);
          } catch (e) {
            if (e instanceof ApiError && e.status < 500) pending.current = null;   // refused: nothing was started
            if (e instanceof ApiError && e.code === "study_consent") {
              // study mode: no work before the consent — open « What is recorded » right away
              openStudyConsent();
              throw new Error(t("home.need.consent"));
            }
            if (e instanceof ApiError && e.code.startsWith("claude_")) throw new Error(e.message);
            throw new Error(e instanceof ApiError && e.code === "empty" ? t("home.need")
              : e instanceof ApiError && e.code === "classified" ? t("up.err.classified", { name: "" }) : t("home.failed"));
          }
        }} />
      {home.limits && <p className="sk-composer-hint">{t("home.formats", { mb: Math.round(home.limits.max_bytes / 1024 / 1024) })}</p>}
      {local && <p className="sk-role-guard sk-small"><Icon name="shield" small />{t("roles.local")}</p>}
      {groups.map((g, gi) => (
        <section key={g.role?.id || "general"} aria-labelledby={`sk-tpl-${gi}`}>
          <h2 className="sk-drawer-label" id={`sk-tpl-${gi}`} style={{ padding: 0, marginBottom: "var(--sk-s3)" }}>
            {g.role ? t("roles.templates.of", { role: g.role.label }) : groups.length > 1 ? t("roles.templates.general") : t("home.templates")}</h2>
          <div className="sk-tasks">
            {g.templates.map((p) => (
              <button key={p.id} className="sk-task" disabled={!can} aria-pressed={tpl?.id === p.id}
                onClick={() => { setTpl(tpl?.id === p.id ? null : p); setFocusKey(`${p.id}-${Date.now()}`); }}>
                <span className="sk-task-ico"><Icon name={templateIcon(p.id)} /></span>
                <b>{p.label}</b><small>{p.description}</small>
                {p.format && <span className="sk-task-fmt">{p.format === "xlsx" ? "Excel" : p.format === "md" ? t("roles.fmt.md") : "Word"}</span>}
              </button>
            ))}
          </div>
        </section>
      ))}
    </div></div>
  );
}

/** 3.6.5 — « Write with Claude »: an explicit choice at the moment of sending, with where the
 *  request goes; absent reasons are said, never hidden (greyed out with the reason). */
function EngineChoice({ gate, engine, model, disabled, onEngine, onModel }: {
  gate: ClaudeGate; engine: "local" | "claude"; model: string; disabled: boolean;
  onEngine: (e: "local" | "claude") => void; onModel: (m: string) => void;
}) {
  const t = useT();
  const claude = engine === "claude" && gate.allowed;
  return (
    <div className="sk-engine" role="group" aria-label={t("eng.choose")}>
      <div className="sk-xs sk-muted">{t("eng.choose")}</div>
      <div className="sk-engine-opts">
        <button type="button" className="sk-engine-opt" aria-pressed={!claude} disabled={disabled} onClick={() => onEngine("local")}>
          <Tick on={!claude} /><span><b>{t("eng.local.opt")}</b><small>{t("eng.local.sub")}</small></span>
        </button>
        <button type="button" className="sk-engine-opt" aria-pressed={claude} disabled={disabled || !gate.allowed}
          aria-describedby={!gate.allowed ? "sk-claude-why" : undefined} onClick={() => onEngine("claude")}>
          <Tick on={claude} /><span><b>{t("eng.claude.opt")}</b>
            <small id={!gate.allowed ? "sk-claude-why" : undefined}>{gate.allowed ? t("eng.claude.sub") : t("eng.claude.off", { reason: gate.reason })}</small></span>
        </button>
      </div>
      {claude && (
        <>
          <div className="sk-engine-model">
            <label className="sk-small" htmlFor="sk-claude-model">{t("eng.claude.model")}</label>
            <select id="sk-claude-model" value={model} onChange={(e) => onModel(e.target.value)}>
              {gate.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
          </div>
          <div className="sk-engine-where" role="note"><Icon name="alert" small />
            <span className="sk-small">{t("eng.claude.where", { dest: gate.destination })}</span></div>
        </>
      )}
    </div>
  );
}

/** the mark of the chosen option: a tick in a ring when chosen, an empty ring otherwise */
function Tick({ on }: { on: boolean }) {
  return <span className={`sk-tick${on ? " is-on" : ""}`} aria-hidden>{on && <Icon name="check" small />}</span>;
}

/** 3.6.5 — the project the person works in, when they have more than one (the cockpit's
 *  selector, in plain words): switching reloads the page in that project. */
function ProjectPicker({ last, only }: { last: string; only: string[] | null }) {
  const t = useT();
  const [list, setList] = useState<ProjectRow[] | null>(null);
  const demo = only !== null;
  const keep = (only || []).join(",");
  useEffect(() => {
    fetchMyProjects().then((r) => {
      // 3.6.7 public demo: the Atelier's own projects only (the instance's others hold no work of it)
      const work = r.projects.filter((p) => !p.shared && (only === null || only.includes(p.slug)));
      setList(work);
      // 3.6.7: the project THIS person last worked in, unless the address names one — the
      // browser's memory is the last person's (a shared computer signed in someone else)
      const named = new URLSearchParams(window.location.search).has("project");
      if (!named && last && last !== currentProject() && work.some((p) => p.slug === last)) { switchProject(last); return; }
      // a person who has no role in the project the browser remembers lands in one of theirs
      if (work.length && !work.some((p) => p.slug === currentProject())) switchProject(work[0].slug);
    }).catch(() => setList(null));
  }, [last, keep]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!list || list.length < 2) return null;
  return (
    <label className="sk-project-pick">
      <span className="sk-xs sk-muted">{t("nav.project")}</span>
      <select value={currentProject()} onChange={(e) => {
        const slug = e.target.value;
        if (demo) { switchProject(slug); return; }   // the demo identity's last project is nobody's
        savePrefs({ project: slug }).catch(() => {}).finally(() => switchProject(slug));
      }}>
        {list.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
      </select>
    </label>
  );
}

/** 4.0 — the administration part of the drawer (instance administrators only). */
function AdminNav({ ob, view, go }: { ob: Onboarding; view: string; go: (v: AdminView) => void }) {
  const t = useOT();
  const p = ob.setup ? progress(ob.setup.steps) : null;
  const items: { id: AdminView; icon: IconName; label: string }[] = [
    ...(ob.setup && !ob.setup.finished ? [{ id: "setup" as const, icon: "steps" as const,
      label: t("ob.nav.setup.n", { n: p?.settled || 0, total: p?.total || 6 }) }] : []),
    { id: "admin_team", icon: "crew", label: t("ob.nav.team") },
    { id: "admin_usage", icon: "gauge", label: t("ob.nav.usage") },
    { id: "admin_validations", icon: "seal", label: t("ob.nav.validations") },
  ];
  return (
    <>
      <div className="sk-drawer-label" id="sk-admin-nav">{t("ob.nav.admin")}</div>
      <div className="sk-nav" role="group" aria-labelledby="sk-admin-nav">
        {items.map((n) => (
          <button key={n.id} className="sk-nav-item" aria-current={view === n.id ? "page" : undefined} onClick={() => go(n.id)}>
            <Icon name={n.icon} />{n.label}
          </button>
        ))}
      </div>
    </>
  );
}

/** 4.0 — on the home page, while the first launch is not finished: where it stands, and « Resume ». */
function ResumeSetup({ ob, onResume }: { ob: Onboarding; onResume: () => void }) {
  const t = useOT();
  const p = progress(ob.setup?.steps || []);
  return (
    <div className="sk-notice is-info sk-ob-resume" role="status">
        <Icon name="steps" />
        <div className="sk-grow"><b>{t("ob.resume.title", { n: p.settled, total: p.total })}</b>
          <div className="sk-small">{t("ob.resume.text")}</div></div>
        <button className="sk-btn sk-btn-primary" onClick={onResume}><Icon name="chevron" />{t("ob.resume.cta")}</button>
    </div>
  );
}
