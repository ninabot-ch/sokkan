"use client";
// Simple profile — settings a person owns: language, appearance, and the profile itself
// (simple / technical). The approval mode is deliberately NOT here.
import { useEffect, useRef, useState } from "react";
import { savePrefs, type Prefs } from "@/lib/simple/api";
import { useT, useUI, type LangPref, type ThemePref } from "@/lib/simple/i18n";
import { useMe } from "@/lib/me";
import { useFeatureOn } from "@/lib/uifeatures";
import { downloadWorkExport, type WorkExportResult } from "@/lib/workExport";
import Icon from "./icons";

export default function Settings({ prefs, onPrefs, onClose, project }: {
  prefs: Prefs; onPrefs: (p: Prefs) => void; onClose: () => void; project: string;
}) {
  const t = useT();
  const { langPref, themePref, setLangPref, setThemePref } = useUI();
  const me = useMe();
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    box.current?.querySelector<HTMLElement>("button")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key !== "Tab" || !box.current) return;
      const f = Array.from(box.current.querySelectorAll<HTMLElement>("button, a[href]")).filter((x) => !x.hasAttribute("disabled"));
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); prev?.focus(); };
  }, [onClose]);

  const langs: { id: LangPref; label: string }[] = [
    { id: "auto", label: t("set.lang.auto") }, { id: "fr", label: "Français" }, { id: "en", label: "English" }];
  const themes: { id: ThemePref; label: string }[] = [
    { id: "light", label: t("set.theme.light") }, { id: "dark", label: t("set.theme.dark") }, { id: "auto", label: t("set.theme.auto") }];
  const instAdmin = ["admin", "owner"].includes(me?.instance_role || "");
  const canSwitch = !prefs.locked || instAdmin;
  const exportOn = useFeatureOn("work_export");
  // 3.6.3: the export says what happens — preparing, downloaded (and what is not in it), failed
  const [exp, setExp] = useState<"idle" | "busy" | "failed" | WorkExportResult>("idle");
  const runExport = async () => {
    setExp("busy");
    try { setExp(await downloadWorkExport()); } catch { setExp("failed"); }
  };

  return (
    <div className="sk-scrim" onClick={onClose}>
      <div className="sk-dialog" role="dialog" aria-modal="true" aria-labelledby="sk-set" ref={box} onClick={(e) => e.stopPropagation()}>
        <div className="sk-row">
          <h2 className="sk-h2 sk-grow" id="sk-set">{t("set.title")}</h2>
          <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={onClose} aria-label={t("close")}><Icon name="close" /></button>
        </div>
        <fieldset>
          <legend className="sk-legend">{t("set.lang")}</legend>
          <div className="sk-seg">{langs.map((l) => <button key={l.id} aria-pressed={langPref === l.id} onClick={() => setLangPref(l.id)}>{l.label}</button>)}</div>
        </fieldset>
        <fieldset>
          <legend className="sk-legend">{t("set.theme")}</legend>
          <div className="sk-seg">{themes.map((x) => <button key={x.id} aria-pressed={themePref === x.id} onClick={() => setThemePref(x.id)}>{x.label}</button>)}</div>
        </fieldset>
        <fieldset>
          <legend className="sk-legend">{t("set.profile")}</legend>
          <div className="sk-seg">
            <button aria-pressed>{t("set.profile.simple")}</button>
            <button aria-pressed={false} disabled={!canSwitch || busy} onClick={async () => {
              setBusy(true);
              try { onPrefs(await savePrefs({ profile: "technical" })); } catch { setBusy(false); }
            }}>{t("set.profile.technical")}</button>
          </div>
          <p className="sk-small sk-muted">{canSwitch ? t("set.profile.hint") : t("set.profile.locked")}</p>
        </fieldset>
        {exportOn && instAdmin && (
          <fieldset>
            <legend className="sk-legend">{t("set.export")}</legend>
            <p className="sk-small sk-muted">{t("set.export.hint")}</p>
            <div>
              <button className="sk-btn" onClick={runExport} disabled={exp === "busy"}>
                {exp === "busy" ? <span className="sk-spin" aria-hidden /> : <Icon name="download" />}
                {t(exp === "busy" ? "set.export.preparing" : "set.export.button")}
              </button>
            </div>
            <div role="status" aria-live="polite" className="sk-list">
              {typeof exp === "object" && (
                <div className="sk-export-done"><Icon name="check" small />
                  <span>{t(exp.projects === 1 ? "set.export.done" : "set.export.done.n", { file: exp.filename, n: exp.projects })}</span></div>
              )}
              {typeof exp === "object" && exp.notIncluded > 0 && (
                <div className="sk-notice"><Icon name="info" />
                  <span>{t(exp.notIncluded === 1 ? "set.export.missing" : "set.export.missing.n", { n: exp.notIncluded })}</span></div>
              )}
              {exp === "failed" && <div className="sk-error"><Icon name="alert" small /><span>{t("set.export.failed")}</span></div>}
            </div>
          </fieldset>
        )}
        <div className="sk-small sk-muted">
          <div>{t("set.signed", { who: me?.email || "" })}</div>
          <div>{t("set.project")} : {project}</div>
        </div>
        <div><a className="sk-btn" href="/api/auth/logout"><Icon name="logout" />{t("set.signout")}</a></div>
      </div>
    </div>
  );
}
