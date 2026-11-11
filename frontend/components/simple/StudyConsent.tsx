"use client";
// Simple profile — study mode (feature `study_mode`): the band above the content and the
// dialog « What is recorded » with the person's consent. The sentences about what is recorded
// are the study's own (lib/studyText.ts, the same as Setup › Study): one text, two screens.
// Silent without the feature: `useStudy()` answers `enabled: false` and nothing is rendered.
import { useEffect, useRef, useState } from "react";
import { refreshStudy, studyConsent, studySetContent, studyWithdraw, useStudy, type StudyMe } from "@/lib/study";
import { TEXT } from "@/lib/studyText";
import { fullDate, useT, useUI } from "@/lib/simple/i18n";
import Icon from "./icons";

const OPEN = "sokkan:study-consent";
/** Ask the shell to open « What is recorded » (the home does, when a start is refused). */
export function openStudyConsent() { try { window.dispatchEvent(new Event(OPEN)); } catch { /* no window */ } }

const day = (iso: string | undefined, lang: string) => {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime())
    ? d.toLocaleDateString(lang === "fr" ? "fr-CH" : "en-GB", { day: "numeric", month: "long", year: "numeric" }) : "";
};

export default function StudyBand() {
  const t = useT();
  const { lang } = useUI();
  const s = useStudy();
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const on = () => setOpen(true);
    window.addEventListener(OPEN, on);
    return () => window.removeEventListener(OPEN, on);
  }, []);
  if (!s || !s.enabled || !s.active) return null;
  const consented = !!s.consent?.current;
  const pending = s.pending_feedback || 0;
  const until = day(s.expires_at, lang);
  return (
    <>
      <div className={`sk-band${consented ? "" : " is-ask"}`} role="status" data-study-band>
        <span className="sk-dot" aria-hidden />
        <span className="sk-band-strong">{t("sb.on", { study: s.study_id || "" })}{until ? ` ${t("sb.until", { date: until })}` : ""}</span>
        <span>{consented ? t("sb.as", { p: s.pseudonym || "" }) : t("sb.ask")}</span>
        {s.notice && typeof s.days_left === "number" && <span className="sk-pill sk-pill-wait">{t("sb.notice", { n: s.days_left })}</span>}
        {consented && pending > 0 && <span className="sk-pill sk-pill-wait">{pending === 1 ? t("sb.pending.one") : t("sb.pending.many", { n: pending })}</span>}
        <button className="sk-band-link sk-push" onClick={() => setOpen(true)} aria-haspopup="dialog">{consented ? t("sb.link") : t("sb.decide")}</button>
      </div>
      {open && <Consent s={s} onClose={() => setOpen(false)} />}
    </>
  );
}

function Consent({ s, onClose }: { s: StudyMe; onClose: () => void }) {
  const t = useT();
  const { lang } = useUI();
  const x = TEXT[lang];
  const c = s.consent;
  const [picked, setPicked] = useState<string[]>(c?.content_scenarios || []);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [sure, setSure] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    box.current?.querySelector<HTMLElement>("h2")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key !== "Tab" || !box.current) return;
      const f = Array.from(box.current.querySelectorAll<HTMLElement>("button, input, a[href]")).filter((el) => !el.hasAttribute("disabled"));
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); prev?.focus(); };
  }, [onClose]);

  const run = async (fn: () => Promise<unknown>, close: boolean) => {
    setBusy(true); setFailed(false);
    try { await fn(); await refreshStudy(); if (close) onClose(); } catch { setFailed(true); } finally { setBusy(false); }
  };
  const toggle = (v: string) => setPicked((cur) => (cur.includes(v) ? cur.filter((y) => y !== v) : [...cur, v].sort()));
  const dirty = picked.join(",") !== (c?.content_scenarios || []).join(",");
  const state = !c ? "" : c.given ? (c.current ? t("sc.state.given", { date: c.at ? fullDate(c.at, lang) : "" }) : t("sc.state.outdated"))
    : c.withdrawn_at ? t("sc.state.withdrawn", { date: fullDate(c.withdrawn_at, lang) }) : t("sc.state.none");
  const col = (title: string, items: string[], kind: string) => (
    <div className={`sk-col ${kind}`}><div className="sk-legend">{title}</div><ul>{items.map((i) => <li key={i}>{i}</li>)}</ul></div>
  );
  return (
    <div className="sk-scrim" onClick={onClose}>
      <div className="sk-dialog sk-dialog-wide" role="dialog" aria-modal="true" aria-labelledby="sk-sc-title" ref={box} onClick={(e) => e.stopPropagation()}>
        <div className="sk-row">
          <h2 className="sk-h2 sk-grow" id="sk-sc-title" tabIndex={-1}>{x.recorded_title}</h2>
          <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={onClose} aria-label={t("close")}><Icon name="close" /></button>
        </div>
        <p>{x.recorded_lead}</p>
        <div className="sk-cols">
          {col(x.recorded_always_title, x.recorded_always, "is-always")}
          {col(x.recorded_consent_title, x.recorded_consent, "is-consent")}
          {col(x.recorded_never_title, x.recorded_never, "is-never")}
        </div>
        <p className="sk-small sk-muted">{x.recorded_who}</p>
        {c && (
          <>
            <div>
              <h3 className="sk-h3">{x.consent_title}</h3>
              <p className={c.current ? "sk-validated" : "sk-changed"} role="status">{state}</p>
              {s.audit_required && !c.current && <p className="sk-small">{x.consent_required}</p>}
            </div>
            <fieldset>
              <legend className="sk-legend">{x.consent_content_label}</legend>
              <div className="sk-row">
                {(s.scenarios || []).map((v) => (
                  <label key={v} className={`sk-check${picked.includes(v) ? " is-on" : ""}`}>
                    <input type="checkbox" checked={picked.includes(v)} onChange={() => toggle(v)} />{v}
                  </label>
                ))}
              </div>
            </fieldset>
            {failed && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{t("sc.failed")}</span></div>}
            <div className="sk-actions">
              {!c.current
                ? <button className="sk-btn sk-btn-primary" disabled={busy} onClick={() => run(() => studyConsent(picked), true)}><Icon name="check" />{x.consent_give}</button>
                : <button className="sk-btn sk-btn-primary" disabled={busy || !dirty} onClick={() => run(() => studySetContent(picked), false)}>{x.consent_update}</button>}
              {!c.current && <button className="sk-btn sk-btn-quiet" disabled={busy} onClick={onClose}>{t("sc.later")}</button>}
              {c.given && !sure && <button className="sk-btn sk-btn-danger sk-push" disabled={busy} onClick={() => setSure(true)}>{x.consent_withdraw}</button>}
              {c.given && sure && <button className="sk-btn sk-btn-danger sk-push" disabled={busy} onClick={() => run(() => studyWithdraw(), false).then(() => setSure(false))}>{t("sc.withdraw.sure")}</button>}
            </div>
            {sure && <p className="sk-small" role="status">{x.consent_withdraw_confirm}{s.audit_required ? ` ${x.consent_required}` : ""}</p>}
            <p className="sk-xs sk-muted">{x.consent_withdraw_note}</p>
          </>
        )}
      </div>
    </div>
  );
}
