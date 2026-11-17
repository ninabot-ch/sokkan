"use client";
// 4.0 — small pieces shared by the first launch, the first sign-in and the administration
// pages: a value shown once with its copy button, the role picker (a list from the server),
// the classification picker with its plain-language explanation.
import { useEffect, useState } from "react";
import { fetchRoles, fetchSample, LEVELS, claudePossible, type Role } from "@/lib/simple/onboarding";
import { useOT, type OKey } from "@/lib/simple/dictOnboarding";
import { useUI } from "@/lib/simple/i18n";
import Icon from "../icons";

/** A secret or a link shown ONCE: selectable text, a copy button that says it worked. */
export function CopyBox({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  const t = useOT();
  const [done, setDone] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); setDone(true); window.setTimeout(() => setDone(false), 2000); }
    catch { /* clipboard refused: the text stays selectable */ }
  };
  return (
    <div className="sk-ob-copy">
      <div className="sk-small">{label}</div>
      <div className="sk-ob-copy-row">
        <code className={mono ? "sk-ob-secret" : "sk-ob-secret is-text"} tabIndex={0}>{value}</code>
        <button className="sk-btn" onClick={copy} aria-live="polite">
          <Icon name={done ? "check" : "clip"} />{t(done ? "ob.ppl.copied" : "ob.ppl.copy")}
        </button>
      </div>
    </div>
  );
}

/** The positions a person picks from. It reads a LIST from the server (the catalogue of
 *  positions when it is installed, else the starter list) and knows nothing else of it. */
export function RolePicker({ value, onPick, roles: given }: { value: string; onPick: (r: Role) => void; roles?: Role[] }) {
  const t = useOT();
  const { lang } = useUI();
  const [roles, setRoles] = useState<Role[] | null | undefined>(given);
  useEffect(() => {
    if (given) { setRoles(given); return; }
    fetchRoles(lang).then((r) => setRoles(r.roles)).catch(() => setRoles(null));
  }, [lang, given]);
  if (roles === undefined) return <div className="sk-list" aria-busy="true"><div className="sk-skel" /></div>;
  if (roles === null || roles.length === 0) return <div className="sk-notice" role="status"><Icon name="info" /><span>{t("wl.role.none")}</span></div>;
  return (
    <div className="sk-ob-roles" role="radiogroup" aria-label={t("wl.role")}>
      {roles.map((r) => (
        <button key={r.id} type="button" role="radio" aria-checked={value === r.id} className="sk-engine-opt sk-ob-role"
          onClick={() => onPick(r)}>
          <span className={`sk-tick${value === r.id ? " is-on" : ""}`} aria-hidden>{value === r.id && <Icon name="check" small />}</span>
          <span><b>{r.label}</b><small>{r.description}</small></span>
        </button>
      ))}
    </div>
  );
}

/** The five classification levels in plain words, with what it means for Claude. */
export function LevelPicker({ value, onChange, maxClaude = "team", name = "sk-ob-level" }: {
  value: string; onChange: (v: string) => void; maxClaude?: string; name?: string;
}) {
  const t = useOT();
  return (
    <div className="sk-ob-levels" role="radiogroup" aria-label={t("ob.proj.level")}>
      {LEVELS.map((l) => (
        <label key={l} className={`sk-ob-level${value === l ? " is-on" : ""}`}>
          <input type="radio" name={name} value={l} checked={value === l} onChange={() => onChange(l)} />
          <span className="sk-grow sk-ob-level-text">
            <span><b>{t(`ob.lv.${l}` as OKey)}</b> <small>{t(`ob.lv.${l}.sub` as OKey)}</small></span>
            <span className={`sk-ob-claude${claudePossible(l, maxClaude) ? "" : " is-never"}`}>
              <Icon name={claudePossible(l, maxClaude) ? "globe" : "shield"} small />
              {t(claudePossible(l, maxClaude) ? "ob.lv.claude.yes" : "ob.lv.claude.no")}
            </span>
          </span>
        </label>
      ))}
    </div>
  );
}

/** A loading / failed line for the admin pages. */
export function Loading() {
  return <div className="sk-list" aria-busy="true"><div className="sk-skel" /><div className="sk-skel" /></div>;
}

/** What the first work will ask, and its example document (added to the project on demand). */
export function Preview({ role, project, sample, setSample }: {
  role: Role; project?: string; sample: { ask: string; document: { id: string; name: string } | null } | null;
  setSample: (s: { ask: string; document: { id: string; name: string } | null }) => void;
}) {
  const t = useOT();
  const { lang } = useUI();
  useEffect(() => {
    if (sample) return;
    fetchSample(role.id, lang, project).then(setSample).catch(() => {});
  }, [role.id, lang, project, sample, setSample]);
  if (!sample) return <div className="sk-skel" />;
  return (
    <div className="sk-card sk-stack">
      <div className="sk-xs sk-muted">{t("ob.first.ask")}</div>
      <p className="sk-ob-ask">{sample.ask}</p>
      {sample.document && <div className="sk-chip"><Icon name="page" small />{t("ob.first.doc", { name: sample.document.name })}</div>}
    </div>
  );
}

