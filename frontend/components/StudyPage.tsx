"use client";
// Feature `study_mode` — Setup › Study. One page, top to bottom: the study here (end date,
// ceilings), « What is recorded » in plain language, my consent, my tasks (scenario, reference
// time, the two answers) and, for the instance's admins, the correspondence table, the export
// package, the read-only tokens and the export of the validations journal.
import { useCallback, useEffect, useState } from "react";
import { useMe } from "@/lib/me";
import {
  STUDY_EXPORT_URL, journalUrl, refreshStudy, studyAdmin, studyConsent, studyFeedback, studyRefresh,
  studySessions, studySetContent, studyTag, studyTokenCreate, studyTokenRevoke, studyWithdraw, useStudy,
  type StudyAdmin, type StudyMe, type StudySession,
} from "@/lib/study";
import { TEXT, fill, fmtDate, fmtTokens as fmtTokensM, useLang, type Lang, type StudyText } from "@/lib/studyText";

const card = "rounded-xl border border-line bg-panel2/40 p-4";
const h3 = "text-[14.5px] font-semibold text-slate-100";
const inp = "ui-focus rounded-lg border border-line bg-[#0b0f16] px-2.5 py-1.5 text-[13px] text-slate-100 outline-none focus:border-sea/60";
const btnP = "ui-focus rounded-lg bg-brass px-3.5 py-2 text-[13px] font-semibold text-ink hover:brightness-110 disabled:opacity-40";
const btn = "ui-focus rounded-lg border border-line px-3 py-1.5 text-[13px] text-slate-200 hover:bg-panel2 disabled:opacity-40";

function Gauge({ label, used, limit, none, raw }: { label: string; used: number | null; limit: number | null; none: string; raw?: boolean }) {
  const fmtTokens = raw ? ((n: number | null | undefined) => (n === null || n === undefined ? "—" : String(n))) : fmtTokensM;
  const pct = limit && used !== null ? Math.min(100, Math.round((used / limit) * 100)) : null;
  const tone = pct === null ? "bg-line" : pct >= 90 ? "bg-red-400" : pct >= 80 ? "bg-amber-400" : "bg-brass";
  return (
    <div className="min-w-0 flex-1">
      <div className="flex items-baseline justify-between gap-2 text-[12.5px] text-mut">
        <span>{label}</span>
        <span className="tabular-nums text-slate-200">
          {limit ? `${fmtTokens(used)} / ${fmtTokens(limit)}${pct !== null ? ` · ${pct} %` : ""}` : `${fmtTokens(used)} · ${none}`}
        </span>
      </div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-line" role="progressbar" aria-label={label}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct ?? 0}>
        <div className={`h-full ${tone}`} style={{ width: `${pct === null ? 0 : Math.max(2, pct)}%` }} />
      </div>
    </div>
  );
}

function Status({ s, t, lang }: { s: StudyMe; t: StudyText; lang: Lang }) {
  if (!s.study_id) return <section className={card}><h3 className={h3}>{t.status_title}</h3><p className="mt-1.5 text-[13.5px] text-mut">{t.status_off}</p></section>;
  return (
    <section className={card} aria-label={t.status_title}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className={h3}>{s.active ? fill(t.status_active, { study: s.study_id }) : fill(t.status_expired, { study: s.study_id, date: fmtDate(s.expires_at, lang) })}</h3>
        {s.active && s.expires_at && <span className="rounded-full border border-line px-2 py-0.5 text-[12px] text-slate-300">{fill(t.status_until, { date: fmtDate(s.expires_at, lang) })}</span>}
        {s.active && s.days_left !== null && s.days_left !== undefined && <span className="rounded-full border border-line px-2 py-0.5 text-[12px] text-slate-300">{fill(t.status_days, { n: s.days_left })}</span>}
        {s.pseudonym && <span className="ml-auto rounded-full border border-brass/50 px-2.5 py-0.5 text-[12.5px] font-medium text-brass">{fill(t.status_you, { p: s.pseudonym })}</span>}
      </div>
      {s.active && s.limits && s.used && (
        <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:gap-6">
          <Gauge label={t.usage_day} used={s.used.tokens_day} limit={s.limits.tokens_day} none={t.usage_none} />
          <Gauge label={t.usage_month} used={s.used.tokens_month} limit={s.limits.tokens_month} none={t.usage_none} />
        </div>
      )}
      {s.active && s.limits && s.mine && (s.mine.tokens_day !== null || s.mine.deep_day !== null) && (
        <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:gap-6">
          <Gauge label={t.usage_mine} used={s.mine.tokens_day} limit={s.limits.user_tokens_day ?? null} none={t.usage_none} />
          <Gauge label={t.usage_mine_deep} used={s.mine.deep_day} limit={s.limits.deep_per_user_day} none={t.usage_none} raw />
        </div>
      )}
      <p className="mt-2.5 text-[12px] text-mut">
        {s.source === "gateway" ? t.status_source_gateway : s.source === "local" ? t.status_source_local : ""}
        {s.stale ? ` — ${t.status_stale}` : ""}
        {s.active && s.limits?.deep_per_user_day && !s.mine ? ` · ${fill(t.usage_deep, { n: s.limits.deep_per_user_day })}` : ""}
      </p>
    </section>
  );
}

function Recorded({ t }: { t: StudyText }) {
  const col = (title: string, items: string[], tone: string, mark: string) => (
    <div className={`min-w-0 rounded-lg border-l-[3px] bg-panel/60 py-2.5 pl-3.5 pr-3 ${tone}`}>
      <div className="text-[13px] font-semibold text-slate-100"><span aria-hidden className="mr-1.5">{mark}</span>{title}</div>
      <ul className="mt-1.5 space-y-1.5 text-[13px] leading-snug text-slate-300">
        {items.map((x) => <li key={x}>{x}</li>)}
      </ul>
    </div>
  );
  return (
    <section className={card} aria-label={t.recorded_title} id="recorded">
      <h3 className={h3}>{t.recorded_title}</h3>
      <p className="mt-1.5 max-w-[68ch] text-[13.5px] leading-relaxed text-slate-300">{t.recorded_lead}</p>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        {col(t.recorded_always_title, t.recorded_always, "border-emerald-400/70", "●")}
        {col(t.recorded_consent_title, t.recorded_consent, "border-brass/80", "◐")}
        {col(t.recorded_never_title, t.recorded_never, "border-slate-500", "○")}
      </div>
      <p className="mt-3 max-w-[76ch] text-[12.5px] leading-relaxed text-mut">{t.recorded_who}</p>
    </section>
  );
}

function Consent({ s, t, lang, onErr }: { s: StudyMe; t: StudyText; lang: Lang; onErr: (e: string) => void }) {
  const c = s.consent;
  const [picked, setPicked] = useState<string[]>(c?.content_scenarios || []);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setPicked(c?.content_scenarios || []); }, [c?.content_scenarios?.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!s.active || !c) return null;
  const run = (p: Promise<unknown>) => { setBusy(true); p.then(() => refreshStudy()).catch((e) => onErr(String(e.message || e))).finally(() => setBusy(false)); };
  const toggle = (x: string) => setPicked((cur) => (cur.includes(x) ? cur.filter((y) => y !== x) : [...cur, x].sort()));
  const dirty = picked.join(",") !== (c.content_scenarios || []).join(",");
  const state = c.given ? (c.current ? fill(t.consent_given, { date: fmtDate(c.at, lang) }) : t.consent_outdated)
    : c.withdrawn_at ? fill(t.consent_withdrawn, { date: fmtDate(c.withdrawn_at, lang) }) : t.consent_none;
  return (
    <section className={`${card} ${c.current ? "" : "border-brass/50"}`} aria-label={t.consent_title} id="consent">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className={h3}>{t.consent_title}</h3>
        <span className={`text-[13px] ${c.current ? "text-emerald-300" : "text-amber-200"}`}>{state}</span>
      </div>
      {s.audit_required && !c.current && <p className="mt-1.5 max-w-[72ch] text-[13px] text-slate-300">{t.consent_required}</p>}
      <fieldset className="mt-3">
        <legend className="text-[13px] text-slate-200">{t.consent_content_label}</legend>
        <div className="mt-2 flex flex-wrap gap-2">
          {(s.scenarios || []).map((x) => {
            const on = picked.includes(x);
            return (
              <label key={x} className={`ui-focus flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-1.5 text-[13px] ${on ? "border-brass/70 bg-brass/10 text-slate-100" : "border-line text-slate-300 hover:bg-panel2"}`}>
                <input type="checkbox" checked={on} onChange={() => toggle(x)} className="h-4 w-4 accent-[#D4A017]" />
                {x}
              </label>
            );
          })}
        </div>
      </fieldset>
      <div className="mt-3.5 flex flex-wrap items-center gap-2.5">
        {!c.current ? (
          <button disabled={busy} className={btnP} onClick={() => run(studyConsent(picked))}>{t.consent_give}</button>
        ) : (
          <button disabled={busy || !dirty} className={btnP} onClick={() => run(studySetContent(picked))}>{t.consent_update}</button>
        )}
        {c.given && (
          <button disabled={busy} className={btn}
            onClick={() => { if (window.confirm(`${t.consent_withdraw_confirm}${s.audit_required ? ` ${t.consent_required}` : ""}`)) run(studyWithdraw()); }}>
            {t.consent_withdraw}
          </button>
        )}
      </div>
      <p className="mt-2.5 text-[12.5px] text-mut">{t.consent_withdraw_note}</p>
    </section>
  );
}

function Task({ x, scenarios, t, onChange, onErr }: { x: StudySession; scenarios: string[]; t: StudyText; onChange: () => void; onErr: (e: string) => void }) {
  const [ref, setRef] = useState(x.reference_minutes === null ? "" : String(x.reference_minutes));
  const [useful, setUseful] = useState<number | null>(x.useful);
  const [verify, setVerify] = useState(x.verify_minutes === null ? "" : String(x.verify_minutes));
  const [editing, setEditing] = useState(x.feedback_at === null);
  const run = (p: Promise<unknown>) => p.then(onChange).catch((e) => onErr(String(e.message || e)));
  const saveRef = () => {
    const n = Number(ref);
    if (ref.trim() === "" || Number.isNaN(n) || n === x.reference_minutes) return;
    run(studyTag(x.session_id, { reference_minutes: n }));
  };
  const id = x.session_id.slice(0, 8);
  const canSend = useful !== null && verify.trim() !== "" && !Number.isNaN(Number(verify)) && Number(verify) >= 0;
  return (
    <li className="rounded-lg border border-line bg-panel/60 p-3">
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
        <div className="min-w-[12rem] flex-1">
          <div className="truncate text-[13.5px] font-medium text-slate-100" title={x.title}>{x.title || t.tasks_untitled}</div>
          <div className="text-[12px] text-mut">{fill(t.tasks_requests, { n: x.turns })}</div>
        </div>
        <label className="flex flex-col gap-1 text-[12px] text-mut">{t.tasks_scenario}
          <select value={x.scenario} onChange={(e) => run(studyTag(x.session_id, { scenario: e.target.value }))} className={inp} aria-label={`${t.tasks_scenario} ${id}`}>
            <option value="">{t.tasks_pick}</option>
            {scenarios.map((sc) => <option key={sc} value={sc}>{sc}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-[12px] text-mut" title={t.tasks_reference_help}>{t.tasks_reference}
          <input inputMode="decimal" value={ref} onChange={(e) => setRef(e.target.value)} onBlur={saveRef}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
            className={`${inp} w-28 tabular-nums`} aria-label={`${t.tasks_reference} ${id}`} />
        </label>
      </div>
      {x.turns > 0 && (editing ? (
        <div className="mt-3 border-t border-line/60 pt-3">
          <div className="text-[13px] font-medium text-slate-100">{t.fb_title}</div>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[13px] text-slate-300">
            <span id={`fbu-${id}`}>{t.fb_useful}</span>
            <span role="radiogroup" aria-labelledby={`fbu-${id}`} className="flex items-center gap-1.5">
              <span className="text-[12px] text-mut">{t.fb_useful_low}</span>
              {[1, 2, 3, 4, 5].map((n) => (
                <button key={n} role="radio" aria-checked={useful === n} onClick={() => setUseful(n)}
                  className={`ui-focus h-8 w-8 rounded-lg border text-[13px] tabular-nums ${useful === n ? "border-brass bg-brass font-semibold text-ink" : "border-line text-slate-200 hover:bg-panel2"}`}>{n}</button>
              ))}
              <span className="text-[12px] text-mut">{t.fb_useful_high}</span>
            </span>
          </div>
          <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[13px] text-slate-300">
            <label htmlFor={`fbv-${id}`}>{t.fb_verify}</label>
            <input id={`fbv-${id}`} inputMode="decimal" value={verify} onChange={(e) => setVerify(e.target.value)} className={`${inp} w-24 tabular-nums`} />
            <button disabled={!canSend} className={btnP}
              onClick={() => run(studyFeedback(x.session_id, useful as number, Number(verify)).then(() => setEditing(false)))}>{t.fb_send}</button>
          </div>
        </div>
      ) : (
        <div className="mt-2.5 flex items-center gap-2 border-t border-line/60 pt-2.5 text-[13px] text-emerald-300">
          <span aria-hidden>✓</span>{fill(t.fb_done, { u: x.useful ?? "—", m: x.verify_minutes ?? "—" })}
          <button className="ui-focus rounded text-[12.5px] text-mut underline hover:text-slate-200" onClick={() => setEditing(true)}>{t.fb_edit}</button>
        </div>
      ))}
    </li>
  );
}

function Tasks({ s, t, onErr }: { s: StudyMe; t: StudyText; onErr: (e: string) => void }) {
  const [rows, setRows] = useState<StudySession[] | null>(null);
  const load = useCallback(() => { studySessions().then((d) => setRows(d.sessions)).catch(() => setRows([])); refreshStudy(); }, []);
  useEffect(() => { load(); }, [load]);
  if (!s.consent?.given) return null;
  return (
    <section className={card} aria-label={t.tasks_title} id="tasks">
      <h3 className={h3}>{t.tasks_title}</h3>
      {rows === null ? <p className="mt-2 text-[13px] text-mut">…</p>
        : rows.length === 0 ? <p className="mt-2 text-[13.5px] text-mut">{t.tasks_none}</p>
        : <ul className="mt-3 space-y-2.5">{rows.map((x) => <Task key={`${x.session_id}:${x.feedback_at}`} x={x} scenarios={s.scenarios || []} t={t} onChange={load} onErr={onErr} />)}</ul>}
    </section>
  );
}

function Admin({ t, lang, onErr }: { t: StudyText; lang: Lang; onErr: (e: string) => void }) {
  const [d, setD] = useState<StudyAdmin | null>(null);
  const [label, setLabel] = useState("");
  const [days, setDays] = useState("30");
  const [fresh, setFresh] = useState("");
  const [f, setF] = useState({ project: "", actor: "", since: "", until: "" });
  const load = useCallback(() => { studyAdmin().then(setD).catch((e) => onErr(String(e.message || e))); }, [onErr]);
  useEffect(() => { load(); }, [load]);
  if (!d) return null;
  const run = (p: Promise<unknown>) => p.then(load).catch((e) => onErr(String(e.message || e)));
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const consentLabel = { given: t.admin_consent_given, withdrawn: t.admin_consent_withdrawn, none: t.admin_consent_none };
  const lastExport = d.exports[0];
  const th = "border-b border-line px-2 pb-1.5 text-left text-[12px] font-medium text-mut";
  const td = "border-b border-line/50 px-2 py-1.5 text-[13px]";
  return (
    <section className="space-y-4" aria-label={t.admin_title} id="admin">
      <div className="flex items-center gap-3 border-t border-line pt-4">
        <h3 className="text-[15px] font-semibold text-slate-100">{t.admin_title}</h3>
        <button className={`${btn} ml-auto`} onClick={() => run(studyRefresh().then(() => refreshStudy()))}>{t.admin_refresh}</button>
      </div>
      {d.single_identity && <div role="alert" className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[13px] text-amber-100">⚠ {t.admin_single}</div>}
      <div className={card}>
        <h4 className="text-[13.5px] font-semibold text-slate-100">{t.admin_people}</h4>
        {d.participants.length === 0 ? <p className="mt-1.5 text-[13px] text-mut">{t.admin_people_none}</p> : (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[30rem] border-separate border-spacing-0">
              <thead><tr><th className={th}>{t.admin_col_pseudo}</th><th className={th}>{t.admin_col_person}</th><th className={th}>{t.admin_col_consent}</th><th className={th}>{t.admin_col_content}</th></tr></thead>
              <tbody>
                {d.participants.map((p) => (
                  <tr key={p.pseudonym}>
                    <td className={`${td} font-semibold text-brass`}>{p.pseudonym}</td>
                    <td className={`${td} text-slate-200`}>{p.email}</td>
                    <td className={`${td} ${p.consent === "given" ? "text-emerald-300" : p.consent === "withdrawn" ? "text-amber-200" : "text-mut"}`}>
                      {consentLabel[p.consent]}{p.consent_at && p.consent === "given" ? ` · ${fmtDate(p.consent_at, lang)}` : ""}</td>
                    <td className={`${td} text-slate-300`}>{p.content_scenarios.join(" ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className={card}>
        <h4 className="text-[13.5px] font-semibold text-slate-100">{t.admin_export}</h4>
        <p className="mt-1 max-w-[72ch] text-[13px] text-slate-300">{t.admin_export_help}</p>
        <div className="mt-2.5 flex flex-wrap items-center gap-2.5">
          <a className={`${btnP} inline-block`} href={`${STUDY_EXPORT_URL}?format=zip`} download>{t.admin_export_zip}</a>
          <a className={`${btn} inline-block`} href={`${STUDY_EXPORT_URL}?format=json`} target="_blank" rel="noreferrer">{t.admin_export_json}</a>
          {lastExport && <span className="text-[12.5px] text-mut">{fill(t.admin_exports_last, { date: fmtDate(lastExport.at, lang), n: lastExport.counts.sessions ?? 0 })}</span>}
        </div>
        <h4 className="mt-4 text-[13.5px] font-semibold text-slate-100">{t.admin_tokens}</h4>
        <p className="mt-1 text-[13px] text-slate-300">{t.admin_tokens_help}</p>
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <input aria-label={t.admin_token_label} placeholder={t.admin_token_label} value={label} onChange={(e) => setLabel(e.target.value)} className={`${inp} min-w-[14rem] flex-1`} />
          <label className="flex items-center gap-1.5 text-[12.5px] text-mut">{t.admin_token_days}
            <input inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value)} className={`${inp} w-16 tabular-nums`} /></label>
          <button className={btn} disabled={!label.trim()}
            onClick={() => run(studyTokenCreate(label.trim(), Math.max(1, Number(days) || 30)).then((r) => { setFresh(r.token); setLabel(""); }))}>{t.admin_token_create}</button>
        </div>
        {fresh && (
          <div role="status" className="mt-2.5 rounded-lg border border-brass/50 bg-brass/10 p-2.5 text-[13px] text-amber-100">
            {t.admin_token_once}
            <code className="mt-1 block select-all break-all rounded bg-[#0b0f16] px-2 py-1.5 font-mono text-[12.5px] text-slate-100">{fresh}</code>
            <div className="mt-2 text-[12.5px] text-slate-300">{t.admin_curl}</div>
            <code className="mt-1 block select-all break-all rounded bg-[#0b0f16] px-2 py-1.5 font-mono text-[12px] text-slate-200">
              curl -H &quot;Authorization: Bearer {fresh}&quot; &quot;{origin}{STUDY_EXPORT_URL}?format=zip&quot; -o sokkan-study.zip</code>
          </div>
        )}
        <ul className="mt-2.5 space-y-1 text-[13px]">
          {d.tokens.length === 0 && <li className="text-mut">{t.admin_token_none}</li>}
          {d.tokens.map((k) => (
            <li key={k.id} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-line/50 pt-1.5">
              <span className="text-slate-100">{k.label || `#${k.id}`}</span>
              <code className="font-mono text-[12px] text-mut">skx_…{k.hint}</code>
              <span className="text-[12.5px] text-mut">{fill(t.admin_token_uses, { n: k.uses })}{k.expires_at ? ` · → ${fmtDate(k.expires_at, lang)}` : ""}</span>
              {k.revoked_at ? <span className="ml-auto text-[12.5px] text-mut">{t.admin_token_revoked}</span>
                : <button className="ui-focus ml-auto rounded text-[12.5px] text-red-300 underline hover:text-red-200" onClick={() => run(studyTokenRevoke(k.id))}>{t.admin_token_revoke}</button>}
            </li>
          ))}
        </ul>
      </div>
      <div className={card}>
        <h4 className="text-[13.5px] font-semibold text-slate-100">{t.admin_journal}</h4>
        <p className="mt-1 max-w-[72ch] text-[13px] text-slate-300">{t.admin_journal_help}</p>
        <div className="mt-2 flex flex-wrap items-end gap-2">
          {(["project", "actor"] as const).map((k) => (
            <input key={k} aria-label={k === "project" ? t.admin_journal_project : t.admin_journal_person}
              placeholder={k === "project" ? t.admin_journal_project : t.admin_journal_person}
              value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} className={`${inp} min-w-[10rem] flex-1`} />
          ))}
          {(["since", "until"] as const).map((k) => (
            <label key={k} className="flex items-center gap-1.5 text-[12.5px] text-mut">{k === "since" ? t.admin_journal_since : t.admin_journal_until}
              <input type="date" value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} className={inp} /></label>
          ))}
          <a className={`${btn} inline-block`} href={journalUrl({ ...f, format: "csv" })} download>CSV</a>
          <a className={`${btn} inline-block`} href={journalUrl({ ...f, format: "json" })} target="_blank" rel="noreferrer">JSON</a>
        </div>
      </div>
    </section>
  );
}

export default function StudyPage({ section }: { section?: string }) {
  const s = useStudy();
  const me = useMe();
  const [lang, setLang] = useLang();
  const [err, setErr] = useState("");
  const t = TEXT[lang];
  const instAdmin = ["admin", "owner"].includes(me?.instance_role || "");
  useEffect(() => {
    if (!section || !s) return;
    document.getElementById(section)?.scrollIntoView({ block: "start" });
  }, [section, s?.enabled]); // eslint-disable-line react-hooks/exhaustive-deps
  const onErr = useCallback((e: string) => setErr(e), []);
  if (!s) return <div className="text-[13px] text-mut">…</div>;
  return (
    <div className="space-y-4" lang={lang}>
      <div className="flex items-center gap-2">
        <h2 className="text-[17px] font-semibold text-slate-100">{t.title}</h2>
        <div role="group" aria-label="Language" className="ml-auto flex overflow-hidden rounded-lg border border-line text-[12.5px]">
          {(["fr", "en"] as Lang[]).map((l) => (
            <button key={l} aria-pressed={lang === l} onClick={() => setLang(l)}
              className={`ui-focus px-2.5 py-1 ${lang === l ? "bg-panel2 font-semibold text-slate-100" : "text-mut hover:text-slate-200"}`}>{l.toUpperCase()}</button>
          ))}
        </div>
      </div>
      {err && (
        <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-400/40 bg-red-500/10 px-3 py-2 text-[13px] text-red-200">
          <span className="min-w-0 flex-1">{fill(t.err, { e: err })}</span>
          <button aria-label="dismiss" className="ui-focus rounded px-1 hover:text-white" onClick={() => setErr("")}>✕</button>
        </div>
      )}
      <Status s={s} t={t} lang={lang} />
      <Recorded t={t} />
      <Consent s={s} t={t} lang={lang} onErr={onErr} />
      <Tasks s={s} t={t} onErr={onErr} />
      {instAdmin && <Admin t={t} lang={lang} onErr={onErr} />}
    </div>
  );
}
