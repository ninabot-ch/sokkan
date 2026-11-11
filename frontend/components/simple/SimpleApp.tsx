"use client";
// Simple profile — the shell: a short drawer (Home / My work / Documents / Memory / My agent crew +
// recent works with the last sentence + today's usage), the current view, Nina and Settings.
// Structure borrowed from Nokker (drawer + thread + ONE composer); the brand is SOKKAN's.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, fetchHome, newRequestId, savePrefs, startWork, type HomeData, type Prefs, type Recent, type TaskTemplate } from "@/lib/simple/api";
import { SimpleUIProvider, useT, useUI, type LangPref, type ThemePref } from "@/lib/simple/i18n";
import { useFeatures } from "@/lib/features";
import { useMe } from "@/lib/me";
import { currentProject } from "@/lib/project";
import BannerSlot from "./BannerSlot";
import IngestReview from "./IngestReview";
import Memory from "./Memory";
import { openStudyConsent } from "./StudyConsent";
import { ingestStatus } from "@/lib/simple/ingest";
import Composer from "./Composer";
import Icon, { templateIcon, type IconName } from "./icons";
import Nina from "./Nina";
import { Documents, Team, Works } from "./Pages";
import Settings from "./Settings";
import Thread from "./Thread";

type View = { name: "home" | "works" | "documents" | "memory" | "team" | "review" } | { name: "work"; sid: string };

function readView(): View {
  if (typeof window === "undefined") return { name: "home" };
  const q = new URLSearchParams(window.location.search);
  const w = q.get("w");
  if (w && /^[0-9a-f]{8,64}$/.test(w)) return { name: "work", sid: w };
  const v = q.get("v");
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
  const save = useCallback((p: { lang?: LangPref; theme?: ThemePref }) => { savePrefs(p).then(onPrefs).catch(() => {}); }, [onPrefs]);
  return (
    <SimpleUIProvider initialLang={prefs.lang} initialTheme={prefs.theme} onChange={save}>
      <Shell prefs={prefs} onPrefs={onPrefs} />
    </SimpleUIProvider>
  );
}

function Shell({ prefs, onPrefs }: { prefs: Prefs; onPrefs: (p: Prefs) => void }) {
  const t = useT();
  const { lang } = useUI();
  const me = useMe();
  const feats = useFeatures();
  const [view, setView] = useState<View>(readView);
  const [home, setHome] = useState<HomeData | null>(null);
  const [homeFailed, setHomeFailed] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [settings, setSettings] = useState(false);
  const [nina, setNina] = useState(false);
  const main = useRef<HTMLElement>(null);
  // « Notes to review » (feature `doc_ingest`): there for the people who may review the
  // batches of this project — the API says so (403 / 404 otherwise), with how many wait
  const ingestOn = !!feats.registry?.items.find((i) => i.id === "doc_ingest")?.enabled;
  const [review, setReview] = useState<number | null>(null);
  useEffect(() => {
    if (!ingestOn) { setReview(null); return; }
    ingestStatus().then((s) => setReview(s.pending_batches)).catch(() => setReview(null));
  }, [ingestOn]);

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
    const work = view.name === "work" ? home?.recents.find((r) => r.sid === view.sid)?.title : "";
    if (view.name === "work" && !work) return;   // named once the work is known, never « My work » meanwhile
    document.title = `${view.name === "work" ? work : t(titles[view.name])} — SOKKAN`;
  }, [view, home, t]);
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
  const name = (me?.name && !me.name.includes("@") ? me.name : "") || me?.email || "";

  return (
    <div className="sk">
      <a href="#sk-main" className="sk-skip">{t("skip")}</a>
      {drawer && <div className="sk-drawer-scrim" onClick={() => setDrawer(false)} aria-hidden />}
      <nav className={`sk-drawer${drawer ? " is-open" : ""}`} aria-label={t("nav.main")}>
        <div className="sk-brand">
          <Brand />
          <button className="sk-btn sk-btn-quiet sk-btn-icon sk-push sk-only-narrow" onClick={() => setDrawer(false)} aria-label={t("nav.close")}><Icon name="close" /></button>
        </div>
        <div className="sk-nav">
          {nav.map((n) => (
            <button key={n.id} className="sk-nav-item" aria-current={view.name === n.id ? "page" : undefined} onClick={() => go({ name: n.id })}>
              <Icon name={n.icon} />{t(`nav.${n.id}`)}
              {n.count ? <span className="sk-nav-count">{n.count}</span> : null}
            </button>
          ))}
        </div>
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
        {view.name === "home" && <Home home={home} failed={homeFailed} retry={loadHome}
          onStarted={(sid) => { loadHome(); go({ name: "work", sid }); }} />}
        {view.name === "works" && <Works onOpen={(sid) => go({ name: "work", sid })} onHome={() => go({ name: "home" })} />}
        {view.name === "documents" && <Documents canAdd={!!home?.can_start} />}
        {view.name === "memory" && <Memory />}
        {view.name === "team" && <Team enabled={!!feats.agents} />}
        {view.name === "review" && <IngestReview onCount={setReview} />}
        {view.name === "work" && <Thread key={view.sid} sid={view.sid} limits={home?.limits ?? null}
          onBack={() => go({ name: "works" })} onChanged={loadHome} />}
        {nina && <Nina onClose={() => setNina(false)} />}
      </main>

      {settings && <Settings prefs={prefs} onPrefs={onPrefs} onClose={() => setSettings(false)} project={home?.project || currentProject()} />}
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

function Home({ home, failed, retry, onStarted }: {
  home: HomeData | null; failed: boolean; retry: () => void; onStarted: (sid: string) => void;
}) {
  const t = useT();
  const { lang } = useUI();
  const [tpl, setTpl] = useState<TaskTemplate | null>(null);
  const [focusKey, setFocusKey] = useState<string | undefined>(undefined);
  // one id per work asked: kept while the same request is tried again, so a second click
  // after a lost answer opens the work that did start instead of a twin
  const pending = useRef<{ sig: string; id: string } | null>(null);
  if (!home) {
    return (
      <div className="sk-scroll"><div className="sk-page">
        {failed ? <div className="sk-empty" role="alert"><Icon name="alert" /><p>{t("home.failed")}</p><button className="sk-btn" onClick={retry}>{t("retry")}</button></div>
          : <><div className="sk-skel" aria-label={t("loading")} /><div className="sk-skel" /></>}
      </div></div>
    );
  }
  const can = home.can_start;
  return (
    <div className="sk-scroll"><div className="sk-page">
      <h1 className="sk-h1">{home.first_name ? t("home.hello", { name: home.first_name }) : t("home.hello.anon")}</h1>
      {!can && <div className="sk-notice" role="status"><Icon name="info" /><span>{t("home.viewer")}</span></div>}
      <Composer autoFocus disabled={!can} canAttach={!!home.limits} limits={home.limits}
        placeholder={tpl ? tpl.hint : t("home.placeholder")} submitLabel={t("home.launch")} busyLabel={t("home.launching")}
        allowEmptyWithDocs={!!tpl} emptyHint={t("home.need")} focusKey={focusKey}
        header={tpl ? (
          <div className="sk-template-on"><Icon name={templateIcon(tpl.id)} small />{t("home.template.on", { label: tpl.label })}
            <button className="sk-chip-x" style={{ marginRight: 0 }} onClick={() => setTpl(null)} aria-label={t("home.template.off")}><Icon name="close" small /></button></div>
        ) : undefined}
        onSubmit={async (text, docs) => {
          const sig = JSON.stringify([text, tpl?.id || "", docs.map((d) => d.id)]);
          if (pending.current?.sig !== sig) pending.current = { sig, id: newRequestId() };
          try {
            const r = await startWork(text, tpl?.id || "", docs.map((d) => d.id), lang, pending.current.id);
            pending.current = null;
            setTpl(null);
            onStarted(r.session_id);
          } catch (e) {
            if (e instanceof ApiError && e.status < 500) pending.current = null;   // refused: nothing was started
            if (e instanceof ApiError && e.code === "study_consent") {
              // study mode: no work before the consent — open « What is recorded » right away
              openStudyConsent();
              throw new Error(t("home.need.consent"));
            }
            throw new Error(e instanceof ApiError && e.code === "empty" ? t("home.need")
              : e instanceof ApiError && e.code === "classified" ? t("up.err.classified", { name: "" }) : t("home.failed"));
          }
        }} />
      {home.limits && <p className="sk-composer-hint">{t("home.formats", { mb: Math.round(home.limits.max_bytes / 1024 / 1024) })}</p>}
      <section aria-labelledby="sk-tpl">
        <h2 className="sk-drawer-label" id="sk-tpl" style={{ padding: 0, marginBottom: "var(--sk-s3)" }}>{t("home.templates")}</h2>
        <div className="sk-tasks">
          {home.playbooks.map((p) => (
            <button key={p.id} className="sk-task" disabled={!can} aria-pressed={tpl?.id === p.id}
              onClick={() => { setTpl(tpl?.id === p.id ? null : p); setFocusKey(`${p.id}-${Date.now()}`); }}>
              <span className="sk-task-ico"><Icon name={templateIcon(p.id)} /></span>
              <b>{p.label}</b><small>{p.description}</small>
            </button>
          ))}
        </div>
      </section>
    </div></div>
  );
}
