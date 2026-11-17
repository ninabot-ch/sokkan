"use client";
// 4.0 — the administrator's everyday pages in the Atelier: Team (people, projects, access),
// Usage (per person and per project, budget and alert), Validations (the journal of human
// decisions, readable, exportable). No identifier or path on screen; every list has its
// loading, empty and error state.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  disablePerson, enablePerson, fetchTeam, fetchUsage, fetchValidations, money, newCode, ObError, patchPerson, saveBudget,
  validationsCsvUrl, type Added, type Person, type Usage, type Validation,
} from "@/lib/simple/onboarding";
import { useOT, type OKey } from "@/lib/simple/dictOnboarding";
import { useT, useUI, when } from "@/lib/simple/i18n";
import Icon from "../icons";
import { CopyBox, Loading } from "./parts";
import { Handover, PersonForm } from "./SetupWizard";

function Failed({ retry, text }: { retry: () => void; text?: string }) {
  const t = useT();
  return <div className="sk-empty" role="alert"><Icon name="alert" /><p>{text || t("home.failed")}</p>
    <button className="sk-btn" onClick={retry}>{t("retry")}</button></div>;
}

// ---- Team -------------------------------------------------------------------------------------
type TeamData = Awaited<ReturnType<typeof fetchTeam>>;

export function TeamPage() {
  const t = useOT();
  const [d, setD] = useState<TeamData | null>(null);
  const [failed, setFailed] = useState(false);
  const [adding, setAdding] = useState(false);
  const [last, setLast] = useState<{ a: Added; name: string } | null>(null);
  const [q, setQ] = useState("");
  const load = useCallback(() => { setFailed(false); fetchTeam().then(setD).catch(() => setFailed(true)); }, []);
  useEffect(load, [load]);
  const rows = useMemo(() => (d?.people || []).filter((p) => !q.trim() ||
    `${p.name} ${p.email} ${p.projects.map((x) => x.name).join(" ")}`.toLowerCase().includes(q.trim().toLowerCase())), [d, q]);
  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div className="sk-row sk-ob-wrap">
        <div className="sk-grow"><h1 className="sk-h1">{t("tm.title")}</h1><p className="sk-lede">{t("tm.lede")}</p></div>
        <button className="sk-btn sk-btn-primary" onClick={() => setAdding(!adding)} aria-expanded={adding}><Icon name="plus" />{t("tm.add")}</button>
      </div>
      {adding && d && <PersonForm ob={{ auth: d.auth }} projects={d.projects} onAdded={(a, name) => { setLast({ a, name }); setAdding(false); load(); }} />}
      {last && <Handover a={last.a} name={last.name} />}
      {failed ? <Failed retry={load} /> : !d ? <Loading /> : (
        <>
          {d.people.length > 6 && (
            <input className="sk-input" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("tm.search")} aria-label={t("tm.search")} />
          )}
          {rows.length === 0 ? <p className="sk-muted">{t("tm.empty")}</p> : (
            <ul className="sk-plain-list sk-list">
              {rows.map((p) => <PersonRow key={p.email} p={p} d={d} onChanged={load} />)}
            </ul>
          )}
        </>
      )}
    </div></div>
  );
}

function PersonRow({ p, d, onChanged }: { p: Person; d: TeamData; onChanged: () => void }) {
  const t = useOT();
  const tt = useT();
  const { lang } = useUI();
  const [edit, setEdit] = useState(false);
  const [roles, setRoles] = useState<Record<string, string>>({});
  const [admin, setAdmin] = useState(p.admin);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [secret, setSecret] = useState<{ code?: string; link?: string } | null>(null);
  const shown = p.name || p.email;
  const open = () => {
    setRoles(Object.fromEntries(p.projects.map((x) => [x.slug, x.role])));
    setAdmin(p.admin); setEdit(true); setErr("");
  };
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setErr("");
    try { await fn(); onChanged(); } catch (x) { setErr(x instanceof ObError ? x.message : t("ob.failed.generic")); } finally { setBusy(false); }
  };
  const access = p.disabled ? t("tm.disabled") : p.invite_pending ? t("tm.invited")
    : d.auth.sso ? t("tm.sso") : p.last_login ? t("tm.last", { when: when(tt, p.last_login, lang) }) : p.owner ? "" : t("tm.never");
  return (
    <li className={`sk-card sk-ob-person${p.disabled ? " is-off" : ""}`}>
      <div className="sk-row sk-ob-wrap">
        <span className="sk-avatar" aria-hidden>{shown[0].toUpperCase()}</span>
        <div className="sk-grow sk-ob-who">
          <b>{shown}</b>{p.name && <span className="sk-small sk-muted">{p.email}</span>}
          {p.position && <span className="sk-small sk-muted">{t("tm.position", { p: p.position })}</span>}
        </div>
        <span className={`sk-pill ${p.disabled ? "sk-pill-wait" : p.admin || p.owner ? "sk-pill-work" : "sk-pill-idle"}`}>
          {p.owner ? t("tm.owner") : p.admin ? t("tm.admin") : t("tm.member")}</span>
      </div>
      <div className="sk-small sk-muted">
        {p.projects.length ? p.projects.map((x) => `${x.name} (${t(`tm.role.${x.role}` as OKey)})`).join(" · ") : ""}
        {access && <>{p.projects.length ? " — " : ""}{access}</>}
      </div>
      {!p.owner && !edit && (
        <div className="sk-row sk-ob-wrap">
          <button className="sk-btn sk-btn-quiet" onClick={open} disabled={busy}><Icon name="pen" />{t("tm.edit")}</button>
          {d.auth.codes && !p.disabled && (
            <>
              <button className="sk-btn sk-btn-quiet" disabled={busy} onClick={() => window.confirm(t("tm.reset.confirm", { name: shown }))
                && act(async () => setSecret(await newCode(p.email, "code")))}><Icon name="shield" />{t("tm.reset")}</button>
              <button className="sk-btn sk-btn-quiet" disabled={busy} onClick={() => window.confirm(t("tm.reset.confirm", { name: shown }))
                && act(async () => setSecret(await newCode(p.email, "link")))}><Icon name="send" />{t("tm.relink")}</button>
            </>
          )}
          {p.disabled
            ? <button className="sk-btn sk-btn-quiet" disabled={busy} onClick={() => act(() => enablePerson(p.email))}><Icon name="check" />{t("tm.enable")}</button>
            : <button className="sk-btn sk-btn-quiet sk-ob-danger" disabled={busy} onClick={() => window.confirm(t("tm.disable.confirm", { name: shown }))
              && act(() => disablePerson(p.email))}><Icon name="stop" />{t("tm.disable")}</button>}
        </div>
      )}
      {secret?.code && <CopyBox label={t("ob.ppl.code")} value={secret.code} />}
      {secret?.link && <CopyBox label={t("ob.ppl.link", { when: "" }).replace(/\s*\(.*\)/, "")} value={secret.link} />}
      {edit && (
        <div className="sk-stack sk-ob-edit">
          <label className={`sk-check${admin ? " is-on" : ""}`}><input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />{t("ob.ppl.admin")}</label>
          <div className="sk-ob-grants">
            {d.projects.map((x) => (
              <label key={x.slug} className="sk-row">
                <span className="sk-grow">{x.name}</span>
                <select className="sk-input sk-select" value={roles[x.slug] || ""} onChange={(e) => setRoles({ ...roles, [x.slug]: e.target.value })}>
                  <option value="">{t("tm.role.none")}</option>
                  {d.roles.map((r) => <option key={r} value={r}>{t(`tm.role.${r}` as OKey)}</option>)}
                </select>
              </label>
            ))}
          </div>
          <div className="sk-row">
            <button className="sk-btn sk-btn-primary" disabled={busy} onClick={() => act(async () => {
              await patchPerson(p.email, { admin, projects: Object.fromEntries(d.projects.map((x) => [x.slug, roles[x.slug] || ""])) });
              setEdit(false);
            })}><Icon name="check" />{t("tm.save")}</button>
            <button className="sk-btn sk-btn-quiet" onClick={() => setEdit(false)}>{t("tm.cancel")}</button>
          </div>
        </div>
      )}
      {err && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{err}</span></div>}
    </li>
  );
}

// ---- Usage ------------------------------------------------------------------------------------
export function UsagePage() {
  const t = useOT();
  const [days, setDays] = useState(30);
  const [u, setU] = useState<Usage | null>(null);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const load = useCallback(() => { setFailed(false); fetchUsage(days).then(setU).catch(() => setFailed(true)); }, [days]);
  useEffect(load, [load]);
  const cur = "USD";
  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div><h1 className="sk-h1">{t("us.title")}</h1><p className="sk-lede">{t("us.lede")}</p></div>
      <fieldset className="sk-field"><legend className="sk-legend">{t("us.period")}</legend>
        <div className="sk-seg">{[7, 30, 90].map((n) => <button key={n} aria-pressed={days === n} onClick={() => setDays(n)}>{t("us.days", { n })}</button>)}</div>
      </fieldset>
      {failed ? <Failed retry={load} /> : !u ? <Loading /> : (
        <>
          {u.alerts.map((a, i) => (
            <div key={i} className={`sk-notice${a.state === "stop" ? "" : " is-info"}`} role="alert"><Icon name="alert" />
              <span>{a.project ? t(a.state === "stop" ? "us.alert.stop" : "us.alert.warn", { name: a.name })
                : t(a.state === "stop" ? "us.alert.inst.stop" : "us.alert.inst.warn", { pct: Math.round(a.pct_day || 0) })}</span></div>
          ))}
          <div className="sk-stats">
            <div className="sk-stat"><b>{money(u.total.cost, cur)}</b><span>{t("us.total")}</span></div>
            <div className="sk-stat"><b>{money(u.today_total, cur)}</b><span>{t("us.today")}</span></div>
            <div className="sk-stat"><b>{u.total.turns}</b><span>{t("us.works")}</span></div>
          </div>
          {u.total.turns > 0 && u.total.cost === 0 && <div className="sk-notice is-info"><Icon name="info" /><span>{t("us.local")}</span></div>}
          <h2 className="sk-h3">{t("us.by.person")}</h2>
          {u.people.length === 0 ? <p className="sk-muted">{t("us.nobody")}</p> : (
            <div className="sk-ob-tablewrap"><table className="sk-ob-table">
              <thead><tr><th scope="col">{t("tm.col.person")}</th><th scope="col">{t("us.col.cost")}</th><th scope="col">{t("us.col.today")}</th><th scope="col">{t("us.col.turns")}</th></tr></thead>
              <tbody>{u.people.map((p) => (
                <tr key={p.email}><th scope="row">{p.email === "—" ? t("us.unknown") : p.name || p.email}</th>
                  <td>{money(p.cost, cur)}</td><td>{money(p.today, cur)}</td><td>{p.turns}</td></tr>
              ))}</tbody>
            </table></div>
          )}
          <h2 className="sk-h3">{t("us.by.project")}</h2>
          <div className="sk-ob-tablewrap"><table className="sk-ob-table">
            <thead><tr><th scope="col">{t("ob.ppl.projects")}</th><th scope="col">{t("us.col.cost")}</th><th scope="col">{t("us.col.turns")}</th>
              {u.project_budgets && <th scope="col">{t("us.col.budget")}</th>}</tr></thead>
            <tbody>{u.projects.map((p) => (
              <tr key={p.slug}><th scope="row">{p.name}</th><td>{money(p.cost, cur)}</td><td>{p.turns}</td>
                {u.project_budgets && <td>
                  <BudgetCell b={p.budget} />
                  <button className="sk-btn sk-btn-quiet" onClick={() => setEditing(editing === p.slug ? null : p.slug)}
                    aria-expanded={editing === p.slug}><Icon name="pen" small />{t("us.budget.set")}</button>
                </td>}</tr>
            ))}</tbody>
          </table></div>
          {editing && u.project_budgets && <BudgetForm project={editing} name={u.projects.find((p) => p.slug === editing)?.name || editing}
            b={u.projects.find((p) => p.slug === editing)?.budget || null} onSaved={(x) => { setU(x); setEditing(null); }} />}
          {!u.project_budgets && <InstanceBudget value={u.instance_budget.budget_day_usd} onSaved={setU} />}
        </>
      )}
    </div></div>
  );
}

function BudgetCell({ b }: { b: Usage["projects"][number]["budget"] }) {
  const t = useOT();
  if (!b || (!b.day && !b.month)) return <span className="sk-small sk-muted">{t("us.budget.none")} </span>;
  const pct = Math.max(b.pct_day || 0, b.pct_month || 0);
  return (
    <span className="sk-ob-budget">
      <span className="sk-small">{b.month ? `${b.spent_month.toFixed(2)} / ${b.month} ${b.currency} · ${t("us.budget.month").toLowerCase()}`
        : `${b.spent_day.toFixed(2)} / ${b.day} ${b.currency} · ${t("us.budget.day").toLowerCase()}`}</span>
      <span className={`sk-gauge${pct >= 80 ? " is-high" : ""}`} role="progressbar" aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={Math.round(Math.min(pct, 100))} aria-label={t("us.col.budget")}><i style={{ width: `${Math.max(2, Math.min(pct, 100))}%` }} /></span>
    </span>
  );
}

function BudgetForm({ project, name, b, onSaved }: { project: string; name: string; b: Usage["projects"][number]["budget"]; onSaved: (u: Usage) => void }) {
  const t = useOT();
  const [day, setDay] = useState(String(b?.day || ""));
  const [month, setMonth] = useState(String(b?.month || ""));
  const [currency, setCurrency] = useState(b?.currency || "CHF");
  const [err, setErr] = useState("");
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try { onSaved(await saveBudget({ project, day: Number(day) || 0, month: Number(month) || 0, currency })); }
    catch { setErr(t("ob.failed.generic")); }
  };
  return (
    <form className="sk-card sk-stack" onSubmit={save}>
      <b>{t("us.budget.set")} — {name}</b>
      <div className="sk-ob-2col">
        <label className="sk-field"><span>{t("us.budget.day")}</span><input className="sk-input" inputMode="decimal" value={day} onChange={(e) => setDay(e.target.value)} /></label>
        <label className="sk-field"><span>{t("us.budget.month")}</span><input className="sk-input" inputMode="decimal" value={month} onChange={(e) => setMonth(e.target.value)} /></label>
      </div>
      <fieldset className="sk-field"><legend className="sk-legend">{t("us.budget.currency")}</legend>
        <div className="sk-seg">{["CHF", "USD"].map((c) => <button type="button" key={c} aria-pressed={currency === c} onClick={() => setCurrency(c)}>{c}</button>)}</div></fieldset>
      <p className="sk-small sk-muted">{t("us.budget.hint")}</p>
      {err && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{err}</span></div>}
      <div><button className="sk-btn sk-btn-primary"><Icon name="check" />{t("us.budget.save")}</button></div>
    </form>
  );
}

function InstanceBudget({ value, onSaved }: { value: number; onSaved: (u: Usage) => void }) {
  const t = useOT();
  const [v, setV] = useState(value ? String(value) : "");
  const [saved, setSaved] = useState(false);
  return (
    <form className="sk-card sk-stack" onSubmit={async (e) => {
      e.preventDefault();
      try { onSaved(await saveBudget({ project: "", day: Number(v) || 0 })); setSaved(true); } catch { setSaved(false); }
    }}>
      <p className="sk-small sk-muted">{t("us.budget.inst.hint")}</p>
      <label className="sk-field"><span>{t("us.budget.inst")}</span>
        <input className="sk-input sk-input-short" inputMode="decimal" value={v} onChange={(e) => { setV(e.target.value); setSaved(false); }} /></label>
      <p className="sk-small sk-muted">{t("us.budget.hint")}</p>
      <div className="sk-row"><button className="sk-btn sk-btn-primary"><Icon name="check" />{t("us.budget.save")}</button>
        {saved && <span className="sk-small" role="status">{t("ob.saved")}</span>}</div>
    </form>
  );
}

// ---- Validations ------------------------------------------------------------------------------
const PAGE = 50;

export function ValidationsPage() {
  const t = useOT();
  const { lang } = useUI();
  const [rows, setRows] = useState<Validation[] | null>(null);
  const [failed, setFailed] = useState<"" | "error" | "forbidden">("");
  const [period, setPeriod] = useState(30);
  const [project, setProject] = useState("");
  const [actor, setActor] = useState("");
  const [decision, setDecision] = useState("");
  const [shown, setShown] = useState(PAGE);
  const [projects, setProjects] = useState<{ slug: string; name: string }[]>([]);
  const [people, setPeople] = useState<{ email: string; name: string }[]>([]);
  const since = useMemo(() => new Date(Date.now() - period * 86400000).toISOString().slice(0, 10), [period]);
  useEffect(() => { fetchTeam().then((d) => { setProjects(d.projects); setPeople(d.people.map((p) => ({ email: p.email, name: p.name }))); }).catch(() => {}); }, []);
  const load = useCallback(() => {
    setFailed(""); setRows(null);
    fetchValidations({ project, actor, since, decision, limit: 2000 })
      .then((r) => { setRows(r.entries.filter((e) => e.decision !== "pending" || !decision)); setShown(PAGE); })
      .catch((e) => setFailed(e instanceof ObError && e.status === 403 ? "forbidden" : "error"));
  }, [project, actor, since, decision]);
  useEffect(load, [load]);
  const nameOf = (e: string) => people.find((p) => p.email === e)?.name || e;
  const pname = (s: string) => projects.find((p) => p.slug === s)?.name || s;
  const fmt = (ts?: number) => ts ? new Date(ts * 1000).toLocaleString(lang === "fr" ? "fr-CH" : "en-GB",
    { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  const dlabel = (v: Validation) => v.edited ? t("va.d.edit") : t(`va.d.${["allow", "deny", "answer", "pending"].includes(v.decision) ? v.decision : "allow"}` as OKey);
  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div className="sk-row sk-ob-wrap">
        <div className="sk-grow"><h1 className="sk-h1">{t("va.title")}</h1><p className="sk-lede">{t("va.lede")}</p></div>
        <a className="sk-btn" href={validationsCsvUrl({ project, actor, since })} download><Icon name="download" />{t("va.export")}</a>
      </div>
      <div className="sk-ob-filters">
        <label className="sk-field"><span>{t("va.filter.period")}</span>
          <select className="sk-input sk-select" value={period} onChange={(e) => setPeriod(Number(e.target.value))}>
            {[7, 30, 90, 365].map((n) => <option key={n} value={n}>{t("us.days", { n })}</option>)}</select></label>
        <label className="sk-field"><span>{t("va.filter.project")}</span>
          <select className="sk-input sk-select" value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">{t("va.all")}</option>{projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}</select></label>
        <label className="sk-field"><span>{t("va.filter.person")}</span>
          <select className="sk-input sk-select" value={actor} onChange={(e) => setActor(e.target.value)}>
            <option value="">{t("va.all")}</option>{people.map((p) => <option key={p.email} value={p.email}>{p.name || p.email}</option>)}</select></label>
        <label className="sk-field"><span>{t("va.filter.decision")}</span>
          <select className="sk-input sk-select" value={decision} onChange={(e) => setDecision(e.target.value)}>
            <option value="">{t("va.all")}</option>{["allow", "deny", "answer"].map((d) => <option key={d} value={d}>{t(`va.d.${d}` as OKey)}</option>)}</select></label>
      </div>
      {failed === "forbidden" ? <p className="sk-muted">{t("va.forbidden")}</p> : failed ? <Failed retry={load} /> : rows === null ? <Loading /> :
        rows.length === 0 ? <p className="sk-muted">{t("va.empty")}</p> : (
          <>
            <div className="sk-small sk-muted" role="status">{t("va.count", { n: rows.length })}</div>
            <ul className="sk-plain-list sk-ob-journal">
              {rows.slice(0, shown).map((v, i) => (
                <li key={`${v.id ?? i}`} className="sk-ob-jrow">
                  <span className={`sk-pill ${v.decision === "deny" ? "sk-pill-wait" : v.decision === "pending" ? "sk-pill-idle" : "sk-pill-ok"}`}>
                    <Icon name={v.decision === "deny" ? "close" : v.decision === "pending" ? "info" : "check"} small />{dlabel(v)}</span>
                  <div className="sk-grow sk-ob-who">
                    <b>{String(v.title || v.tool || v.kind || "")}</b>
                    <span className="sk-small sk-muted">
                      {v.auto ? t("va.auto") : nameOf(String(v.actor || ""))}
                      {v.owner && v.owner !== v.actor ? ` · ${t("va.on", { owner: nameOf(String(v.owner)) })}` : ""}
                      {v.project ? ` · ${pname(String(v.project))}` : ""}
                    </span>
                  </div>
                  <span className="sk-small sk-muted sk-ob-when">{fmt(Number(v.decided_at || v.requested_at) || undefined)}</span>
                </li>
              ))}
            </ul>
            {rows.length > shown && <div><button className="sk-btn" onClick={() => setShown(shown + PAGE)}>{t("va.more")}</button></div>}
          </>
        )}
    </div></div>
  );
}
