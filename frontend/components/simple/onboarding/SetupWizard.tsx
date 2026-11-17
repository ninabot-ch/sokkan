"use client";
// 4.0 — the administrator's first launch: organisation, model (with a connection test),
// projects (classification in plain words), people (code, link or SSO), first documents,
// first work. Every step can be skipped; the state lives on the server (backend/onboarding.py),
// so closing the page and coming back resumes at the first step still to do.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  addPerson, createSetupProject, currentStep, fetchSample, fetchSetupProjects, fetchTeam, finishSetup, markStep, saveMe,
  nextAfter, ObError, openWorkIn, progress, saveModel, saveOrg, startIn, STEPS, testModel, uploadTo, looksLikeEmail,
  duration, type Added, type ModelKind, type Onboarding, type Person, type ProjectRow, type Role, type StepId, type TestResult,
} from "@/lib/simple/onboarding";
import { newRequestId } from "@/lib/simple/api";
import { useOT, type OKey } from "@/lib/simple/dictOnboarding";
import { useUI, type LangPref } from "@/lib/simple/i18n";
import { currentProject } from "@/lib/project";
import Icon from "../icons";
import { CopyBox, LevelPicker, Preview, RolePicker } from "./parts";

export default function SetupWizard({ ob, reload, onClose, setLangPref }: {
  ob: Onboarding; reload: () => Promise<void> | void; onClose: () => void; setLangPref: (l: LangPref) => void;
}) {
  const t = useOT();
  const steps = ob.setup?.steps || STEPS.map((id) => ({ id, state: "todo" as const, at: null }));
  const [step, setStep] = useState<StepId | "done">(() => ob.setup?.finished ? "done" : currentStep(steps));
  const head = useRef<HTMLHeadingElement>(null);
  useEffect(() => { head.current?.focus(); }, [step]);
  const p = progress(steps);
  const idx = step === "done" ? STEPS.length : STEPS.indexOf(step);

  const settle = useCallback(async (id: StepId, state: "done" | "skipped") => {
    try {
      const st = await markStep(id, state);
      await reload();
      setStep(nextAfter(st.steps, id) || "done");
    } catch { setStep((s) => s); }
  }, [reload]);

  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide sk-ob">
      <div>
        <h1 className="sk-h1">{t("ob.title")}</h1>
        <p className="sk-lede">{t("ob.lede")}</p>
      </div>
      <div className="sk-progress" role="progressbar" aria-label={t("ob.steps")} aria-valuemin={0} aria-valuemax={p.total}
        aria-valuenow={p.settled}><i style={{ width: `${Math.max(4, (100 * p.settled) / p.total)}%` }} /></div>
      <div className="sk-ob-grid">
        <nav className="sk-ob-rail" aria-label={t("ob.steps")}>
          <ol>
            {STEPS.map((id, i) => {
              const st = steps.find((s) => s.id === id)?.state || "todo";
              return (
                <li key={id}>
                  <button className="sk-ob-railbtn" aria-current={step === id ? "step" : undefined} onClick={() => setStep(id)}>
                    <span className={`sk-ob-num is-${st}`} aria-hidden>{st === "done" ? <Icon name="check" small /> : i + 1}</span>
                    <span className="sk-grow">{t(`ob.s.${id}` as OKey)}</span>
                    <span className="sk-xs sk-muted">{t(`ob.state.${st}` as OKey)}</span>
                  </button>
                </li>
              );
            })}
          </ol>
          <button className="sk-btn sk-btn-quiet" onClick={onClose}><Icon name="back" />{t("ob.later")}</button>
        </nav>
        <section className="sk-ob-body" aria-labelledby="sk-ob-h">
          {step !== "done" && <div className="sk-xs sk-muted">{t("ob.step", { n: idx + 1, total: STEPS.length })}</div>}
          <h2 className="sk-h2" id="sk-ob-h" tabIndex={-1} ref={head}>
            {step === "done" ? t("ob.done.title") : t(`ob.s.${step}` as OKey)}
          </h2>
          {step === "org" && <OrgStep ob={ob} onDone={() => settle("org", "done")} setLangPref={setLangPref} />}
          {step === "model" && <ModelStep ob={ob} onDone={() => settle("model", "done")} />}
          {step === "projects" && <ProjectsStep onDone={() => settle("projects", "done")} />}
          {step === "people" && <PeopleStep ob={ob} onDone={() => settle("people", "done")} />}
          {step === "documents" && <DocumentsStep ob={ob} onDone={() => settle("documents", "done")} />}
          {step === "first_work" && <FirstWorkStep ob={ob} onStarted={() => markStep("first_work", "done").catch(() => {})} />}
          {step === "done" && <Finished ob={ob} reload={reload} onClose={onClose} />}
          {step !== "done" && (
            <div className="sk-ob-foot">
              {idx > 0 && <button className="sk-btn sk-btn-quiet" onClick={() => setStep(STEPS[idx - 1])}><Icon name="back" />{t("ob.back")}</button>}
              <span className="sk-grow" />
              <button className="sk-btn sk-btn-quiet" onClick={() => settle(step, "skipped")}>{t("ob.skip")}</button>
            </div>
          )}
        </section>
      </div>
    </div></div>
  );
}

function Err({ text }: { text: string }) {
  return text ? <div className="sk-error" role="alert"><Icon name="alert" small /><span>{text}</span></div> : null;
}

// ---- 1. organisation --------------------------------------------------------------------------
function OrgStep({ ob, onDone, setLangPref }: { ob: Onboarding; onDone: () => void; setLangPref: (l: LangPref) => void }) {
  const t = useOT();
  const { lang } = useUI();
  const [name, setName] = useState(ob.org_name);
  const [lng, setLng] = useState<"fr" | "en">((ob.org_lang as "fr" | "en") || lang);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try { await saveOrg(name.trim(), lng); setLangPref(lng); onDone(); }
    catch { setErr(t("ob.failed.generic")); } finally { setBusy(false); }
  };
  return (
    <form className="sk-stack" onSubmit={save}>
      <p className="sk-muted">{t("ob.org.lede")}</p>
      <label className="sk-field"><span>{t("ob.org.name")}</span>
        <input className="sk-input" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("ob.org.name.ph")}
          autoComplete="organization" maxLength={80} autoFocus /></label>
      <fieldset className="sk-field"><legend className="sk-legend">{t("ob.org.lang")}</legend>
        <div className="sk-seg">
          <button type="button" aria-pressed={lng === "fr"} onClick={() => setLng("fr")}>Français</button>
          <button type="button" aria-pressed={lng === "en"} onClick={() => setLng("en")}>English</button>
        </div>
      </fieldset>
      <Err text={err} />
      <div><button className="sk-btn sk-btn-primary" disabled={busy || name.trim().length < 2}>
        {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="check" />}{t("ob.next")}</button></div>
    </form>
  );
}

// ---- 2. model ---------------------------------------------------------------------------------
function ModelStep({ ob, onDone }: { ob: Onboarding; onDone: () => void }) {
  const t = useOT();
  const { lang } = useUI();
  const m = ob.model;
  const [kind, setKind] = useState<ModelKind>(m?.kind && m.kind !== "none" ? (m.kind === "custom" ? "key" : m.kind)
    : m?.local_available ? "local" : "sokkan");
  const [key, setKey] = useState("");
  const [adv, setAdv] = useState(m?.kind === "custom");
  const [url, setUrl] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState<"" | "test" | "save">("");
  const [res, setRes] = useState<TestResult | null>(null);
  useEffect(() => { setRes(null); }, [kind, key, url, model]);
  if (m?.operator_managed) {
    return (<><div className="sk-notice is-info"><Icon name="info" /><span>{t("ob.model.managed")}</span></div>
      <div><button className="sk-btn sk-btn-primary" onClick={onDone}><Icon name="check" />{t("ob.next")}</button></div></>);
  }
  const sendKind: ModelKind = kind === "key" && adv ? "custom" : kind;
  const unchanged = m?.configured && kind === m.kind && !key && kind === "local";
  const test = async () => {
    setBusy("test");
    try { setRes(await testModel(sendKind, key, adv ? url : "", adv ? model : "", lang)); }
    catch { setRes({ ok: false, code: "error", title: t("ob.failed.generic"), message: "", latency_ms: null, detail: "" }); }
    finally { setBusy(""); }
  };
  const save = async () => {
    setBusy("save");
    try {
      const r = await saveModel(sendKind, key, adv ? url : "", adv ? model : "", lang);
      setRes(r.test);
      if (r.saved) { setKey(""); onDone(); }
    } catch { setRes({ ok: false, code: "error", title: t("ob.failed.generic"), message: "", latency_ms: null, detail: "" }); }
    finally { setBusy(""); }
  };
  const opts: { id: ModelKind; title: OKey; sub: OKey; disabled?: boolean }[] = [
    { id: "local", title: "ob.model.local", sub: m?.local_available ? "ob.model.local.sub" : "ob.model.local.none", disabled: !m?.local_available },
    { id: "sokkan", title: "ob.model.sokkan", sub: "ob.model.sokkan.sub" },
    { id: "key", title: "ob.model.key", sub: "ob.model.key.sub" },
  ];
  const needsKey = kind !== "local";
  return (
    <div className="sk-stack">
      <p className="sk-muted">{t("ob.model.lede")}</p>
      <div className={`sk-notice${m?.configured ? " is-info" : ""}`} role="status"><Icon name="info" />
        <span>{m?.configured ? t("ob.model.current", { what: t(`ob.model.kind.${m.kind}` as OKey) }) : t("ob.model.current.none")}</span></div>
      <div className="sk-engine-opts" role="radiogroup" aria-label={t("ob.s.model")}>
        {opts.map((o) => (
          <button key={o.id} type="button" role="radio" aria-checked={kind === o.id} className="sk-engine-opt sk-ob-role"
            disabled={o.disabled} onClick={() => setKind(o.id)}>
            <span className={`sk-tick${kind === o.id ? " is-on" : ""}`} aria-hidden>{kind === o.id && <Icon name="check" small />}</span>
            <span><b>{t(o.title)}</b><small>{t(o.sub)}</small></span>
          </button>
        ))}
      </div>
      {needsKey && (
        <label className="sk-field"><span>{t(kind === "sokkan" ? "ob.model.token" : "ob.model.apikey")}</span>
          <input className="sk-input" type="password" autoComplete="off" spellCheck={false} value={key}
            placeholder={kind === "sokkan" ? "sik_…" : "sk-ant-…"} onChange={(e) => setKey(e.target.value)} /></label>
      )}
      {kind === "key" && (
        <label className="sk-check sk-ob-adv"><input type="checkbox" checked={adv} onChange={(e) => setAdv(e.target.checked)} />{t("ob.model.advanced")}</label>
      )}
      {kind === "key" && adv && (
        <div className="sk-ob-2col">
          <label className="sk-field"><span>{t("ob.model.url")}</span>
            <input className="sk-input" value={url} placeholder="https://…" onChange={(e) => setUrl(e.target.value)} /></label>
          <label className="sk-field"><span>{t("ob.model.name")}</span>
            <input className="sk-input" value={model} onChange={(e) => setModel(e.target.value)} /></label>
        </div>
      )}
      <div className="sk-row">
        <button className="sk-btn" onClick={test} disabled={!!busy || (needsKey && key.trim().length < 8)}>
          {busy === "test" ? <span className="sk-spin" aria-hidden /> : <Icon name="shield" />}{t(busy === "test" ? "ob.model.testing" : "ob.model.test")}</button>
        {unchanged
          ? <button className="sk-btn sk-btn-primary" onClick={onDone} disabled={!!busy}><Icon name="check" />{t("ob.model.keep")}</button>
          : <button className="sk-btn sk-btn-primary" onClick={save} disabled={!!busy || (needsKey && key.trim().length < 8) || (res !== null && !res.ok)}>
            {busy === "save" ? <span className="sk-spin" aria-hidden /> : <Icon name="check" />}{t("ob.model.save")}</button>}
      </div>
      <div aria-live="polite">
        {res && (
          <div className={`sk-notice ${res.ok ? "is-ok" : ""}`} role={res.ok ? "status" : "alert"}>
            <Icon name={res.ok ? "check" : "alert"} />
            <div><b>{res.title}</b><div className="sk-small">{res.message}
              {res.ok && res.latency_ms !== null ? ` ${t("ob.model.latency", { ms: res.latency_ms })}` : ""}</div></div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---- 3. projects ------------------------------------------------------------------------------
function ProjectsStep({ onDone }: { onDone: () => void }) {
  const t = useOT();
  const [rows, setRows] = useState<ProjectRow[] | null>(null);
  const [multi, setMulti] = useState(true);
  const [name, setName] = useState("");
  const [level, setLevel] = useState("team");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const load = useCallback(() => fetchSetupProjects().then((r) => { setRows(r.projects); setMulti(r.multi); }).catch(() => setRows([])), []);
  useEffect(() => { load(); }, [load]);
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(""); setMsg("");
    try { const p = await createSetupProject(name.trim(), level); setMsg(t("ob.proj.created", { name: p.name })); setName(""); load(); }
    catch (x) { setErr(x instanceof ObError && x.code === "multi_project_off" ? t("ob.proj.off") : t("ob.failed.generic")); }
    finally { setBusy(false); }
  };
  return (
    <div className="sk-stack">
      <p className="sk-muted">{t("ob.proj.lede")}</p>
      {!multi ? <div className="sk-notice is-info"><Icon name="info" /><span>{t("ob.proj.off")}</span></div> : (
        <form className="sk-stack sk-card" onSubmit={create}>
          <label className="sk-field"><span>{t("ob.proj.name")}</span>
            <input className="sk-input" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("ob.proj.name.ph")} maxLength={80} /></label>
          <fieldset className="sk-field"><legend className="sk-legend">{t("ob.proj.level")}</legend>
            <LevelPicker value={level} onChange={setLevel} /></fieldset>
          <Err text={err} />
          {msg && <div className="sk-notice is-ok" role="status"><Icon name="check" /><span>{msg}</span></div>}
          <div><button className="sk-btn" disabled={busy || name.trim().length < 2}>
            {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="plus" />}{t("ob.proj.create")}</button></div>
        </form>
      )}
      <h3 className="sk-h3">{t("ob.proj.list")}</h3>
      {rows === null ? <div className="sk-skel" /> : rows.length === 0 ? <p className="sk-muted">{t("ob.proj.none")}</p> : (
        <ul className="sk-plain-list sk-ob-list">
          {rows.map((r) => (
            <li key={r.slug} className="sk-row"><Icon name="works" small /><b className="sk-grow">{r.name}</b>
              <span className="sk-pill sk-pill-idle">{t(`ob.lv.${r.level}` as OKey)}</span>
              <span className="sk-small sk-muted">{t("ob.proj.people", { n: r.people })}</span></li>
          ))}
        </ul>
      )}
      <div><button className="sk-btn sk-btn-primary" onClick={onDone}><Icon name="check" />{t("ob.next")}</button></div>
    </div>
  );
}

// ---- 4. people --------------------------------------------------------------------------------
export function PersonForm({ ob, projects, onAdded, compact }: {
  ob: Pick<Onboarding, "auth">; projects: { slug: string; name: string }[]; onAdded: (a: Added, name: string) => void; compact?: boolean;
}) {
  const t = useOT();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [picked, setPicked] = useState<Set<string>>(() => new Set(projects.length === 1 ? [projects[0].slug] : []));
  const [admin, setAdmin] = useState(false);
  const [via, setVia] = useState<"link" | "code">("link");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    if (!looksLikeEmail(email)) { setErr(t("ob.ppl.need.email")); return; }
    if (!admin && picked.size === 0) { setErr(t("ob.ppl.need.project")); return; }
    setBusy(true);
    try {
      const a = await addPerson({ email: email.trim(), name: name.trim(), admin, projects: Array.from(picked), via });
      onAdded(a, name.trim() || email.trim());
      setName(""); setEmail(""); setAdmin(false);
    } catch (x) {
      setErr(x instanceof ObError && x.code === "projects" ? t("ob.ppl.need.project")
        : x instanceof ObError && x.code === "email" ? t("ob.ppl.need.email") : t("ob.failed.generic"));
    } finally { setBusy(false); }
  };
  return (
    <form className={`sk-stack${compact ? "" : " sk-card"}`} onSubmit={submit} noValidate>
      <div className="sk-ob-2col">
        <label className="sk-field"><span>{t("ob.ppl.name")}</span>
          <input className="sk-input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" maxLength={120} /></label>
        <label className="sk-field"><span>{t("ob.ppl.email")}</span>
          <input className="sk-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" /></label>
      </div>
      {projects.length > 0 && (
        <fieldset className="sk-field"><legend className="sk-legend">{t("ob.ppl.projects")}</legend>
          <div className="sk-row sk-ob-wrap">
            {projects.map((p) => (
              <label key={p.slug} className={`sk-check${picked.has(p.slug) ? " is-on" : ""}`}>
                <input type="checkbox" checked={picked.has(p.slug)} onChange={(e) => {
                  const n = new Set(picked); if (e.target.checked) n.add(p.slug); else n.delete(p.slug); setPicked(n);
                }} />{p.name}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      <label className={`sk-check${admin ? " is-on" : ""}`}><input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />{t("ob.ppl.admin")}</label>
      {ob.auth.codes && (
        <fieldset className="sk-field"><legend className="sk-legend">{t("ob.ppl.via")}</legend>
          <div className="sk-engine-opts" role="radiogroup" aria-label={t("ob.ppl.via")}>
            {(["link", "code"] as const).map((v) => (
              <button key={v} type="button" role="radio" aria-checked={via === v} className="sk-engine-opt sk-ob-role" onClick={() => setVia(v)}>
                <span className={`sk-tick${via === v ? " is-on" : ""}`} aria-hidden>{via === v && <Icon name="check" small />}</span>
                <span><b>{t(`ob.ppl.via.${v}` as OKey)}</b><small>{t(`ob.ppl.via.${v}.sub` as OKey)}</small></span>
              </button>
            ))}
          </div>
        </fieldset>
      )}
      <Err text={err} />
      <div><button className="sk-btn sk-btn-primary" disabled={busy}>
        {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="plus" />}{t("ob.ppl.add")}</button></div>
    </form>
  );
}

/** What to hand over after an invitation: the code or the link once, and a message to paste. */
export function Handover({ a, name }: { a: Added; name: string }) {
  const t = useOT();
  const { lang } = useUI();
  const when = a.expires_at ? new Date(a.expires_at * 1000).toLocaleDateString(lang === "fr" ? "fr-CH" : "en-GB",
    { day: "numeric", month: "long" }) : "";
  return (
    <div className="sk-notice is-ok sk-ob-handover" role="status">
      <Icon name="check" />
      <div className="sk-stack sk-grow">
        <b>{t("ob.ppl.added", { name })}</b>
        {a.code && <CopyBox label={t("ob.ppl.code")} value={a.code} />}
        {a.link && <CopyBox label={t("ob.ppl.link", { when })} value={a.link} />}
        {!a.code && !a.link && <span className="sk-small">{t("ob.ppl.sso", { url: a.url })}</span>}
        <CopyBox mono={false} label={t("ob.ppl.message")} value={a.link ? t("ob.ppl.msg.link", { name, link: a.link })
          : t("ob.ppl.msg.code", { name, url: a.url })} />
      </div>
    </div>
  );
}

function PeopleStep({ ob, onDone }: { ob: Onboarding; onDone: () => void }) {
  const t = useOT();
  const [team, setTeam] = useState<{ people: Person[]; projects: { slug: string; name: string }[] } | null>(null);
  const [last, setLast] = useState<{ a: Added; name: string } | null>(null);
  const load = useCallback(() => fetchTeam().then(setTeam).catch(() => setTeam({ people: [], projects: [] })), []);
  useEffect(() => { load(); }, [load]);
  if (!team) return <div className="sk-skel" />;
  const others = team.people.filter((p) => !p.owner);
  return (
    <div className="sk-stack">
      <p className="sk-muted">{t(ob.auth.sso ? "ob.ppl.lede.sso" : "ob.ppl.lede")}</p>
      {team.projects.length === 0 && <div className="sk-notice"><Icon name="info" /><span>{t("ob.ppl.no.projects")}</span></div>}
      <PersonForm ob={ob} projects={team.projects} onAdded={(a, name) => { setLast({ a, name }); load(); }} />
      {last && <Handover a={last.a} name={last.name} />}
      <h3 className="sk-h3">{t("ob.ppl.list")}</h3>
      {others.length === 0 ? <p className="sk-muted">{t("ob.ppl.none")}</p> : (
        <ul className="sk-plain-list sk-ob-list">
          {others.map((p) => (
            <li key={p.email} className="sk-row"><span className="sk-avatar" aria-hidden>{(p.name || p.email)[0].toUpperCase()}</span>
              <span className="sk-grow"><b>{p.name || p.email}</b>{p.name && <span className="sk-small sk-muted"> · {p.email}</span>}</span>
              <span className="sk-small sk-muted">{p.admin ? t("tm.admin") : p.projects.map((x) => x.name).join(", ")}</span></li>
          ))}
        </ul>
      )}
      <div><button className="sk-btn sk-btn-primary" onClick={onDone}><Icon name="check" />{t("ob.next")}</button></div>
    </div>
  );
}

// ---- 5. documents -----------------------------------------------------------------------------
function DocumentsStep({ ob, onDone }: { ob: Onboarding; onDone: () => void }) {
  const t = useOT();
  const [projects, setProjects] = useState<ProjectRow[] | null>(null);
  const [project, setProject] = useState(currentProject());
  const [sending, setSending] = useState<string[]>([]);
  const [added, setAdded] = useState(0);
  const [errs, setErrs] = useState<string[]>([]);
  const [drop, setDrop] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  useEffect(() => {
    fetchSetupProjects().then((r) => {
      setProjects(r.projects);
      if (r.projects.length && !r.projects.some((p) => p.slug === project)) setProject(r.projects[0].slug);
    }).catch(() => setProjects([]));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  if (!ob.features?.documents) {
    return (<><div className="sk-notice"><Icon name="info" /><span>{t("ob.docs.off")}</span></div>
      <div><button className="sk-btn sk-btn-primary" onClick={onDone}>{t("ob.next")}</button></div></>);
  }
  const add = async (files: FileList | File[]) => {
    const e: string[] = [];
    let n = 0;
    for (const f of Array.from(files).slice(0, 20)) {
      setSending((s) => [...s, f.name]);
      try { await uploadTo(project, f); n++; } catch (x) { e.push(`${f.name} — ${x instanceof Error ? x.message : ""}`); }
      setSending((s) => s.filter((x) => x !== f.name));
    }
    setAdded((a) => a + n); setErrs(e);
  };
  const pname = projects?.find((p) => p.slug === project)?.name || project;
  return (
    <div className="sk-stack">
      <p className="sk-muted">{t("ob.docs.lede")}</p>
      {projects && projects.length > 1 && (
        <label className="sk-field"><span>{t("ob.docs.project")}</span>
          <select className="sk-input sk-select" value={project} onChange={(e) => setProject(e.target.value)}>
            {projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
          </select></label>
      )}
      <div className={`sk-dropzone${drop ? " is-drop" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrop(true); }} onDragLeave={() => setDrop(false)}
        onDrop={(e) => { e.preventDefault(); setDrop(false); if (e.dataTransfer.files?.length) add(e.dataTransfer.files); }}>
        <Icon name="upload" />
        <div className="sk-row" style={{ justifyContent: "center" }}>
          <span>{t("ob.docs.drop")}</span>
          <input ref={picker} type="file" multiple hidden tabIndex={-1} aria-hidden accept=".pdf,.docx,.pptx,.xlsx,.txt,.md"
            onChange={(e) => { if (e.target.files?.length) add(e.target.files); e.target.value = ""; }} />
          <button className="sk-btn" onClick={() => picker.current?.click()}><Icon name="clip" />{t("ob.docs.pick")}</button>
        </div>
        {sending.map((n) => <span key={n} className="sk-chip" role="status"><span className="sk-spin" aria-hidden />{n}</span>)}
      </div>
      {added > 0 && <div className="sk-notice is-ok" role="status"><Icon name="check" /><span>{t("ob.docs.added", { n: added, project: pname })}</span></div>}
      {errs.map((e, i) => <Err key={i} text={e} />)}
      <div><button className="sk-btn sk-btn-primary" onClick={onDone}><Icon name="check" />{t("ob.next")}</button></div>
    </div>
  );
}

// ---- 6. first work ----------------------------------------------------------------------------
function FirstWorkStep({ ob, onStarted }: { ob: Onboarding; onStarted: () => Promise<unknown> | void }) {
  const t = useOT();
  const { lang } = useUI();
  const [role, setRole] = useState<Role | null>(null);
  const [projects, setProjects] = useState<ProjectRow[] | null>(null);
  const [project, setProject] = useState(currentProject());
  const [sample, setSample] = useState<{ ask: string; document: { id: string; name: string } | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    fetchSetupProjects().then((r) => {
      setProjects(r.projects);
      if (r.projects.length && !r.projects.some((p) => p.slug === project)) setProject(r.projects[0].slug);
    }).catch(() => setProjects([]));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setSample(null); }, [role, project]);
  const noModel = !ob.model?.configured;
  const go = async () => {
    if (!role) return;
    setBusy(true); setErr("");
    try {
      const s = sample || await fetchSample(role.id, lang, project);
      setSample(s);
      const r = await startIn(project, s.ask, s.document ? [s.document.id] : [], lang, newRequestId());
      // the job picked for the first work is the admin's own job when they have none yet (one choice)
      if (!ob.me.role_id) await saveMe({ role_id: role.id }).catch(() => {});
      await onStarted();
      openWorkIn(project, r.session_id);
    } catch (x) { setErr(x instanceof Error && x.message ? t("ob.failed", { why: x.message }) : t("ob.failed.generic")); setBusy(false); }
  };
  return (
    <div className="sk-stack">
      <p className="sk-muted">{t("ob.first.lede")}</p>
      {noModel && <div className="sk-notice"><Icon name="alert" /><span>{t("ob.first.no.model")}</span></div>}
      <RolePicker value={role?.id || ""} onPick={setRole} />
      {projects && projects.length > 1 && (
        <label className="sk-field"><span>{t("ob.first.project")}</span>
          <select className="sk-input sk-select" value={project} onChange={(e) => setProject(e.target.value)}>
            {projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
          </select></label>
      )}
      {role && <Preview role={role} project={project} sample={sample} setSample={setSample} />}
      <Err text={err} />
      <div><button className="sk-btn sk-btn-primary" onClick={go} disabled={!role || busy || noModel}>
        {busy ? <span className="sk-spin" aria-hidden /> : <Icon name="send" />}{t(busy ? "ob.first.going" : "ob.first.go")}</button></div>
    </div>
  );
}

// ---- finished ---------------------------------------------------------------------------------
function Finished({ ob, reload, onClose }: { ob: Onboarding; reload: () => Promise<void> | void; onClose: () => void }) {
  const t = useOT();
  const { lang } = useUI();
  const finished = !!ob.setup?.finished_at;
  return (
    <div className="sk-stack">
      <p>{t("ob.done.text")}</p>
      {ob.ttfv?.seconds != null && <div className="sk-notice is-ok"><Icon name="seal" /><span>{t("ob.done.ttfv", { d: duration(ob.ttfv.seconds, lang) })}</span></div>}
      <div className="sk-row">
        {!finished && <button className="sk-btn sk-btn-primary" onClick={async () => { await finishSetup().catch(() => {}); await reload(); onClose(); }}>
          <Icon name="check" />{t("ob.done.finish")}</button>}
        {finished && <button className="sk-btn sk-btn-primary" onClick={onClose}><Icon name="home" />{t("ob.done.open")}</button>}
        {finished && <button className="sk-btn sk-btn-quiet" onClick={async () => { await finishSetup(true).catch(() => {}); await reload(); }}>{t("ob.done.reopen")}</button>}
      </div>
    </div>
  );
}
