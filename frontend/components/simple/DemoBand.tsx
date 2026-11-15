"use client";
// 3.6.7 — SOKKAN Atelier on the public read-only demo (feature `demo_atelier`).
// The band says where the visitor is, what they can look at, and offers the operator's call to
// action (book a demo / install — URLs from the instance's environment, a button only when one is
// set). The home of the demo replaces the composer the visitor could not use (a greyed-out text area
// and four greyed-out templates read as broken) by the work already done, to open.
import { useFeatures } from "@/lib/features";
import type { HomeData } from "@/lib/simple/api";
import { switchDemoInterface } from "@/lib/simple/demo";
import { useT } from "@/lib/simple/i18n";
import Icon, { templateIcon } from "./icons";
import { StatePill } from "./Pages";

export function DemoCta({ compact = false }: { compact?: boolean }) {
  const t = useT();
  const cta = useFeatures().demo_cta;
  if (!cta || (!cta.book_url && !cta.install_url)) return null;
  return (
    <span className={`sk-demo-cta${compact ? " is-compact" : ""}`}>
      {cta.book_url && <a className="sk-btn sk-btn-primary" href={cta.book_url} target="_blank" rel="noopener">{t("demo.cta.book")}</a>}
      {cta.install_url && <a className="sk-btn" href={cta.install_url} target="_blank" rel="noopener">{t("demo.cta.install")}</a>}
    </span>
  );
}

export default function DemoBand() {
  const t = useT();
  if (!useFeatures().demo_atelier) return null;
  return (
    <div className="sk-band is-demo" role="note" aria-label={t("demo.band.label")}>
      <Icon name="eye" small />
      <span className="sk-band-strong">{t("demo.band.title")}</span>
      <span className="sk-hide-narrow">{t("demo.band.text")}</span>
      <button className="sk-band-link" onClick={() => switchDemoInterface("technical")}>{t("demo.band.cockpit")}</button>
      <DemoCta compact />
    </div>
  );
}

/** The home of the demo: what the Atelier is for, the work already done here, what it can do. */
export function DemoHome({ home, onOpen }: { home: HomeData; onOpen: (sid: string) => void }) {
  const t = useT();
  return (
    <div className="sk-scroll"><div className="sk-page">
      <div>
        <h1 className="sk-h1">{t("demo.home.title")}</h1>
        <p className="sk-lede">{t("demo.home.lede")}</p>
      </div>
      <div className="sk-notice is-info" role="status"><Icon name="info" /><span>{t("demo.home.notice")}</span></div>
      <section aria-labelledby="sk-demo-works" className="sk-list">
        <h2 className="sk-drawer-label" id="sk-demo-works" style={{ padding: 0 }}>{t("demo.home.works", { project: home.project_name || home.project })}</h2>
        {home.recents.length === 0 ? <p className="sk-muted">{t("demo.home.none")}</p> : (
          <ul className="sk-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {home.recents.map((r) => (
              <li key={r.sid}>
                <button className="sk-card sk-card-btn" onClick={() => onOpen(r.sid)}>
                  <span className="sk-row"><span className="sk-card-title sk-grow">{r.title}</span><StatePill r={r} /></span>
                  {r.last_line && <span className="sk-muted">{r.last_line}</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="sk-demo-shows">
        <h2 className="sk-drawer-label" id="sk-demo-shows" style={{ padding: 0, marginBottom: "var(--sk-s3)" }}>{t("demo.home.shows")}</h2>
        <ul className="sk-demo-points">
          <li><Icon name="memory" /><span><b>{t("demo.point.memory")}</b> {t("demo.point.memory.text")}</span></li>
          <li><Icon name="page" /><span><b>{t("demo.point.deliverable")}</b> {t("demo.point.deliverable.text")}</span></li>
          <li><Icon name="seal" /><span><b>{t("demo.point.validation")}</b> {t("demo.point.validation.text")}</span></li>
        </ul>
      </section>
      <section aria-labelledby="sk-demo-tpl">
        <h2 className="sk-drawer-label" id="sk-demo-tpl" style={{ padding: 0, marginBottom: "var(--sk-s3)" }}>{t("demo.home.templates")}</h2>
        <div className="sk-tasks">
          {home.playbooks.map((p) => (
            <div key={p.id} className="sk-task is-static">
              <span className="sk-task-ico"><Icon name={templateIcon(p.id)} /></span>
              <b>{p.label}</b><small>{p.description}</small>
            </div>
          ))}
        </div>
      </section>
      <div className="sk-demo-foot"><DemoCta /></div>
    </div></div>
  );
}
