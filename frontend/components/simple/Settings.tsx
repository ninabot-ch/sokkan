"use client";
// Simple profile — settings a person owns: language, appearance, and the profile itself
// (simple / technical). The approval mode is deliberately NOT here.
import { useEffect, useRef, useState } from "react";
import { fetchClaudeSettings, saveClaudeDefault, saveClaudeKey, savePrefs, type ClaudeSettings, type Prefs } from "@/lib/simple/api";
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
  const claudeOn = useFeatureOn("claude_tier");
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
        {claudeOn && instAdmin && <ClaudeAdmin />}
        <div className="sk-small sk-muted">
          <div>{t("set.signed", { who: me?.email || "" })}</div>
          <div>{t("set.project")} : {project}</div>
        </div>
        <div><a className="sk-btn" href="/api/auth/logout"><Icon name="logout" />{t("set.signout")}</a></div>
      </div>
    </div>
  );
}

/** 3.6.5 — the organisation's Anthropic key and the model offered first (« Write with Claude »).
 *  The key is never shown back: only its last four characters and the result of its test. */
function ClaudeAdmin() {
  const t = useT();
  const [st, setSt] = useState<ClaudeSettings | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  useEffect(() => { fetchClaudeSettings().then(setSt).catch(() => setSt(null)); }, []);
  if (!st) return null;
  const save = async () => {
    setBusy(true); setMsg("");
    try {
      const r = await saveClaudeKey(key.trim());
      setSt(r); setKey("");
      setMsg(t("set.claude.saved", { masked: r.key?.masked || "", detail: r.test?.ok ? t("set.claude.valid") : r.test?.detail || "" }));
    } catch { setMsg(t("set.claude.failed")); } finally { setBusy(false); }
  };
  return (
    <fieldset className="sk-claude-set">
      <legend className="sk-legend">{t("set.claude")}</legend>
      <p className="sk-small sk-muted">{t("set.claude.hint")}</p>
      {st.key && <div className="sk-small">{t("set.claude.current", { masked: st.key.masked })}{st.key.test_ok ? ` · ${t("set.claude.valid")}` : st.key.test_detail ? ` · ${st.key.test_detail}` : ""}</div>}
      <label className="sk-small" htmlFor="sk-claude-key">{t("set.claude.key")}</label>
      <input id="sk-claude-key" type="password" autoComplete="off" spellCheck={false} value={key}
        placeholder="sk-ant-…" onChange={(e) => setKey(e.target.value)} />
      <div><button className="sk-btn" disabled={busy || key.trim().length < 8} onClick={save}>
        {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="check" />}{t("set.claude.save")}</button></div>
      {msg && <div className="sk-small" role="status">{msg}</div>}
      {st.key && (
        <>
          <label className="sk-small" htmlFor="sk-claude-default">{t("set.claude.default")}</label>
          <select id="sk-claude-default" value={st.default_model}
            onChange={async (e) => { try { setSt(await saveClaudeDefault(e.target.value)); } catch { /* kept */ } }}>
            {st.models.filter((m) => m.available).map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
        </>
      )}
    </fieldset>
  );
}
