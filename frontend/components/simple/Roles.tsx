"use client";
// SOKKAN Atelier 4.0 — job kits (« Postes »): the picker (first sign-in and Settings), what a
// job template asks for and gives back, and the two Settings sections (my jobs, an admin's
// default job per person). Data: backend/roles/*.json through backend/roles_api.py.
import { useEffect, useRef, useState } from "react";
import { fetchRolePeople, fetchRoles, savePrefs, saveRolesOf, setupRole,
  type Job, type Prefs, type RolesCatalog, type TaskTemplate } from "@/lib/simple/api";
import { setupPlan, toggle } from "@/lib/simple/roles";
import { useT, useUI } from "@/lib/simple/i18n";
import { switchProject } from "@/lib/project";
import Icon from "./icons";

/** The choice of one's jobs. `first`: offered once at the first sign-in (« Later » = never
 *  asked again, the jobs stay in Settings). */
export function RolePicker({ first, onClose, onSaved }: {
  first: boolean; onClose: () => void; onSaved: (p: Prefs) => void;
}) {
  const t = useT();
  const { lang } = useUI();
  const [cat, setCat] = useState<RolesCatalog | null>(null);
  const [failed, setFailed] = useState(false);
  const [sel, setSel] = useState<string[]>([]);
  const [create, setCreate] = useState(true);
  const [here, setHere] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    fetchRoles(lang).then((c) => { setCat(c); setSel(c.mine || []); }).catch(() => setFailed(true));
  }, [lang]);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); prev?.focus(); };
  }, [onClose, busy]);
  useEffect(() => { if (cat) box.current?.querySelector<HTMLElement>(".sk-role-card")?.focus(); }, [cat]);

  const save = async (roles: string[]) => {
    if (!cat) return;
    setBusy(true); setMsg("");
    try {
      const p = await savePrefs({ roles });
      const plan = setupPlan(roles, { create, canCreate: cat.can_create_project, here, canSetupHere: cat.can_setup_here,
        project: cat.project, already: cat.mine || [] });
      let open = "";
      for (const step of plan) {
        const r = await setupRole(step.rid, step.project, lang);
        open = open || r.project;
      }
      onSaved(p);
      if (open && open !== cat.project) { switchProject(open); return; }
      onClose();
    } catch {
      setMsg(t("roles.failed"));
      setBusy(false);
    }
  };

  return (
    <div className="sk-scrim" onClick={() => !busy && onClose()}>
      <div className="sk-dialog sk-dialog-wide" role="dialog" aria-modal="true" aria-labelledby="sk-roles-h" ref={box}
        onClick={(e) => e.stopPropagation()}>
        <div className="sk-row">
          <h2 className="sk-h2 sk-grow" id="sk-roles-h">{t(first ? "roles.first.title" : "roles.title")}</h2>
          <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={onClose} disabled={busy} aria-label={t("close")}><Icon name="close" /></button>
        </div>
        <p className="sk-lede">{t("roles.lede")}</p>
        {failed ? <div className="sk-error" role="alert"><Icon name="alert" small /><span>{t("roles.failed")}</span></div>
          : !cat ? <div className="sk-skel" aria-label={t("loading")} /> : (
          <>
            <div className="sk-roles" role="group" aria-label={t("roles.title")}>
              {cat.roles.map((r) => (
                <button key={r.id} className="sk-role-card" aria-pressed={sel.includes(r.id)} onClick={() => setSel(toggle(sel, r.id))}>
                  <span className="sk-row"><b className="sk-grow">{r.label}</b>{sel.includes(r.id) && <Icon name="check" small />}</span>
                  <small>{r.description}</small>
                  <span className="sk-xs sk-muted">{t("roles.count", { n: r.templates_count })}
                    {!r.claude && <> · <Icon name="shield" small /> {t("roles.local.short")}</>}</span>
                </button>
              ))}
            </div>
            {cat.can_create_project ? (
              <label className="sk-check sk-small"><input type="checkbox" checked={create} onChange={(e) => setCreate(e.target.checked)} />
                <span>{t("roles.create")}</span></label>
            ) : cat.can_setup_here && sel.length === 1 && !cat.project_role ? (
              <label className="sk-check sk-small"><input type="checkbox" checked={here} onChange={(e) => setHere(e.target.checked)} />
                <span>{t("roles.here")}</span></label>
            ) : (
              <p className="sk-small sk-muted">{t("roles.ask_admin")}</p>
            )}
            {msg && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{msg}</span></div>}
            <div className="sk-actions">
              <button className="sk-btn sk-btn-primary" disabled={busy || (!first && !sel.length && !(cat.mine || []).length)} onClick={() => save(sel)}>
                {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="check" />}{t(sel.length ? "roles.save" : "roles.save.none")}</button>
              {first && <button className="sk-btn" disabled={busy} onClick={() => save([])}>{t("roles.later")}</button>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** What a job template asks for and gives back — under the composer once it is picked. */
export function TemplateDetail({ tpl }: { tpl: TaskTemplate }) {
  const t = useT();
  if (!tpl.attach && !tpl.produces) return null;
  const fmt = tpl.format === "xlsx" ? t("roles.fmt.xlsx") : tpl.format === "md" ? t("roles.fmt.md") : t("roles.fmt.docx");
  return (
    <dl className="sk-tpl-detail sk-small">
      {tpl.attach && <><dt>{t("roles.attach")}</dt><dd>{tpl.attach}</dd></>}
      {tpl.produces && <><dt>{t("roles.produces")}</dt><dd>{tpl.produces} <span className="sk-muted">({fmt})</span></dd></>}
      {tpl.checks?.length ? <><dt>{t("roles.checks")}</dt><dd>{tpl.checks.join(" · ")}</dd></> : null}
    </dl>
  );
}

/** Settings › My jobs. */
export function MyRoles({ jobs, onChange }: { jobs: Job[]; onChange: () => void }) {
  const t = useT();
  return (
    <fieldset>
      <legend className="sk-legend">{t("roles.mine")}</legend>
      <p className="sk-small sk-muted">{jobs.length ? jobs.map((j) => j.label).join(" · ") : t("roles.mine.none")}</p>
      <div><button className="sk-btn" onClick={onChange}><Icon name="crew" />{t("roles.change")}</button></div>
    </fieldset>
  );
}

/** Settings › Jobs of the people (instance admin): a default job per person. */
export function RolesAdmin() {
  const t = useT();
  const { lang } = useUI();
  const [people, setPeople] = useState<{ email: string; name: string; roles: string[] | null }[] | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [msg, setMsg] = useState("");
  useEffect(() => {
    fetchRolePeople().then((r) => setPeople(r.people)).catch(() => setPeople(null));
    fetchRoles(lang).then((c) => setJobs(c.roles)).catch(() => setJobs([]));
  }, [lang]);
  if (!people || !jobs.length) return null;
  return (
    <fieldset className="sk-claude-set">
      <legend className="sk-legend">{t("roles.admin")}</legend>
      <p className="sk-small sk-muted">{t("roles.admin.hint")}</p>
      <div className="sk-list">
        {people.map((p) => (
          <label key={p.email} className="sk-role-person sk-small">
            <span className="sk-grow">{p.name ? `${p.name} · ${p.email}` : p.email}</span>
            <select value={(p.roles || [])[0] || ""} onChange={async (e) => {
              const v = e.target.value;
              const next = v ? [v, ...(p.roles || []).filter((x) => x !== v)] : [];
              try {
                await saveRolesOf(p.email, next);
                setPeople(people.map((x) => x.email === p.email ? { ...x, roles: next } : x));
                setMsg(t("roles.admin.saved", { who: p.name || p.email }));
              } catch { setMsg(t("roles.failed")); }
            }}>
              <option value="">{t("roles.admin.none")}</option>
              {jobs.map((j) => <option key={j.id} value={j.id}>{j.label}</option>)}
            </select>
          </label>
        ))}
      </div>
      {msg && <div className="sk-small" role="status">{msg}</div>}
    </fieldset>
  );
}
