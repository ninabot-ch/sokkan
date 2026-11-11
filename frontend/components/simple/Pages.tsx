"use client";
// Simple profile — the three list pages of the drawer: My work, Documents, My agent crew.
// Each has its loading, empty and error state; none shows an identifier or a path.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, deleteDocument, documentUrl, fetchDocuments, fetchWorks, uploadDocument,
  type Doc, type Limits, type Recent,
} from "@/lib/simple/api";
import { agentsList, type Agent } from "@/lib/api";
import { size, useT, useUI, when } from "@/lib/simple/i18n";
import type { Key } from "@/lib/simple/dict";
import { precheck, uploadError } from "./Composer";
import Icon from "./icons";

function Loading() {
  const t = useT();
  return <div className="sk-list" aria-busy="true" aria-label={t("loading")}><div className="sk-skel" /><div className="sk-skel" /></div>;
}
function Failed({ retry }: { retry: () => void }) {
  const t = useT();
  return <div className="sk-empty" role="alert"><Icon name="alert" /><p>{t("home.failed")}</p><button className="sk-btn" onClick={retry}>{t("retry")}</button></div>;
}

export function StatePill({ r }: { r: Pick<Recent, "state" | "deliverables" | "validated"> }) {
  const t = useT();
  if (r.state === "waiting") return <span className="sk-pill sk-pill-wait"><span className="sk-dot" />{t("state.waiting")}</span>;
  if (r.state === "working") return <span className="sk-pill sk-pill-work"><span className="sk-dot" />{t("state.working")}</span>;
  if (r.deliverables && r.validated) return <span className="sk-pill sk-pill-ok"><Icon name="check" small />{t("state.validated")}</span>;
  if (r.deliverables) return <span className="sk-pill sk-pill-ok"><span className="sk-dot" />{t("state.done")}</span>;
  return <span className="sk-pill sk-pill-idle">{t("state.idle")}</span>;
}

// ---- My work ----------------------------------------------------------------------------------
export function Works({ onOpen, onHome }: { onOpen: (sid: string) => void; onHome: () => void }) {
  const t = useT();
  const { lang } = useUI();
  const [rows, setRows] = useState<Recent[] | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => { setFailed(false); fetchWorks().then((r) => setRows(r.works)).catch(() => setFailed(true)); }, []);
  useEffect(load, [load]);
  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div><h1 className="sk-h1">{t("works.title")}</h1><p className="sk-lede">{t("works.lede")}</p></div>
      {failed ? <Failed retry={load} /> : rows === null ? <Loading /> : rows.length === 0 ? (
        <div className="sk-empty"><Icon name="works" /><p>{t("works.empty")}</p>
          <button className="sk-btn sk-btn-primary" onClick={onHome}><Icon name="plus" />{t("works.empty.cta")}</button></div>
      ) : (
        <ul className="sk-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {rows.map((r) => (
            <li key={r.sid}>
              <button className="sk-card sk-card-btn" onClick={() => onOpen(r.sid)}>
                <span className="sk-row"><span className="sk-card-title sk-grow">{r.title}</span><StatePill r={r} /></span>
                {r.last_line && <span className="sk-muted">{r.last_line}</span>}
                <span className="sk-small sk-muted sk-row">
                  <span>{t("works.updated", { when: when(t, r.updated_at, lang) })}</span>
                  <span aria-hidden>·</span>
                  <span>{r.deliverables === 0 ? t("works.none_yet")
                    : r.to_validate > 0 ? t(r.to_validate > 1 ? "works.to_validate.n" : "works.to_validate", { n: r.to_validate })
                    : t("state.validated")}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div></div>
  );
}

// ---- Documents --------------------------------------------------------------------------------
export function Documents({ canAdd }: { canAdd: boolean }) {
  const t = useT();
  const { lang } = useUI();
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [limits, setLimits] = useState<Limits | null>(null);
  const [failed, setFailed] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [sending, setSending] = useState<string[]>([]);
  const [drop, setDrop] = useState(false);
  const [asking, setAsking] = useState<Doc | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const load = useCallback(() => {
    setFailed(false);
    fetchDocuments().then((r) => { setDocs(r.documents); setLimits(r.limits); }).catch(() => setFailed(true));
  }, []);
  useEffect(load, [load]);

  const add = async (list: FileList | File[]) => {
    const errs: string[] = [];
    for (const f of Array.from(list).slice(0, 20)) {
      const refusal = precheck(t, f, limits);
      if (refusal) { errs.push(refusal); continue; }
      setSending((s) => [...s, f.name]);
      try { await uploadDocument(f); } catch (e) { errs.push(uploadError(t, e, f.name, limits)); }
      setSending((s) => s.filter((x) => x !== f.name));
    }
    setErrors(errs);
    load();
  };

  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div><h1 className="sk-h1">{t("docs.title")}</h1><p className="sk-lede">{t("docs.lede")}</p></div>
      {canAdd && (
        <div className={`sk-dropzone${drop ? " is-drop" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDrop(true); }} onDragLeave={() => setDrop(false)}
          onDrop={(e) => { e.preventDefault(); setDrop(false); if (e.dataTransfer.files?.length) add(e.dataTransfer.files); }}>
          <Icon name="upload" />
          <div className="sk-row" style={{ justifyContent: "center" }}>
            <span>{t("docs.drop")}</span>
            <input ref={picker} type="file" multiple hidden tabIndex={-1} aria-hidden accept={(limits?.extensions || []).map((x) => `.${x}`).join(",")}
              onChange={(e) => { if (e.target.files?.length) add(e.target.files); e.target.value = ""; }} />
            <button className="sk-btn" onClick={() => picker.current?.click()}><Icon name="clip" />{t("docs.pick")}</button>
          </div>
          {limits && <span className="sk-small sk-muted">{t("home.formats", { mb: Math.round(limits.max_bytes / 1024 / 1024) })}</span>}
          {sending.map((n) => <span key={n} className="sk-chip" role="status"><span className="sk-spin" aria-hidden />{t("up.sending", { name: n })}</span>)}
        </div>
      )}
      {errors.length > 0 && <div role="alert" className="sk-list">{errors.map((e, i) => <div key={i} className="sk-error"><Icon name="alert" small /><span>{e}</span></div>)}</div>}
      {failed ? <Failed retry={load} /> : docs === null ? <Loading /> : docs.length === 0 ? (
        <div className="sk-empty"><Icon name="page" /><p>{t("docs.empty")}</p></div>
      ) : (
        <ul className="sk-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {docs.map((d) => (
            <li key={d.id} className="sk-doc-row">
              <span className="sk-doc-ext">{d.ext}</span>
              <div className="sk-grow">
                <div className="sk-doc-name">{d.name}</div>
                <div className="sk-small sk-muted">
                  {size(t, d.size)} · {t("docs.added", { when: when(t, d.created_at, lang) })}
                  {d.uses > 0 && ` · ${t(d.uses > 1 ? "docs.used.n" : "docs.used", { n: d.uses })}`}
                  {!["ok", "partial"].includes(d.text_state) && ` · ${t("up.text.none")}`}
                </div>
              </div>
              <a className="sk-btn sk-btn-quiet sk-btn-icon" href={documentUrl(d.id)} download aria-label={t("docs.download", { name: d.name })} title={t("docs.download", { name: d.name })}><Icon name="download" /></a>
              {canAdd && <button className="sk-btn sk-btn-quiet sk-btn-icon sk-btn-danger" aria-label={t("docs.delete", { name: d.name })} title={t("docs.delete", { name: d.name })} onClick={() => setAsking(d)}><Icon name="trash" /></button>}
            </li>
          ))}
        </ul>
      )}
      {asking && (
        <div className="sk-scrim" onClick={() => setAsking(null)}>
          <div className="sk-dialog" role="alertdialog" aria-modal="true" aria-labelledby="sk-del" onClick={(e) => e.stopPropagation()}>
            <p id="sk-del" className="sk-h3">{t("docs.delete.confirm", { name: asking.name })}</p>
            <div className="sk-actions">
              <button className="sk-btn sk-btn-danger" autoFocus onClick={async () => {
                const d = asking; setAsking(null);
                try { await deleteDocument(d.id); } catch (e) { setErrors([e instanceof ApiError && e.status === 403 ? t("up.err.forbidden") : t("home.failed")]); }
                load();
              }}><Icon name="trash" />{t("docs.delete.yes")}</button>
              <button className="sk-btn" onClick={() => setAsking(null)}>{t("cancel")}</button>
            </div>
          </div>
        </div>
      )}
    </div></div>
  );
}

// ---- My agent crew ----------------------------------------------------------------------------
export function Team({ enabled }: { enabled: boolean }) {
  const t = useT();
  const { lang } = useUI();
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => {
    if (!enabled) return;
    setFailed(false);
    agentsList().then((r) => setAgents(r.agents)).catch(() => setFailed(true));
  }, [enabled]);
  useEffect(load, [load]);
  const word = (a: Agent): { key: Key; cls: string } =>
    a.status === "draft" || a.status === "pending" || a.needs_approval ? { key: "team.state.draft", cls: "sk-pill-wait" }
    : a.status === "paused" ? { key: "team.state.paused", cls: "sk-pill-idle" }
    : a.deck === "running" ? { key: "team.state.running", cls: "sk-pill-work" }
    : a.deck === "error" ? { key: "team.state.error", cls: "sk-pill-wait" }
    : a.deck === "armed" ? { key: "team.state.armed", cls: "sk-pill-ok" }
    : { key: "team.state.idle", cls: "sk-pill-idle" };
  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div><h1 className="sk-h1">{t("team.title")}</h1><p className="sk-lede">{t("team.lede")}</p></div>
      {!enabled ? <div className="sk-empty"><Icon name="crew" /><p>{t("team.off")}</p></div>
        : failed ? <Failed retry={load} /> : agents === null ? <Loading />
        : agents.length === 0 ? <div className="sk-empty"><Icon name="crew" /><p>{t("team.empty")}</p><p className="sk-small sk-muted">{t("team.empty.hint")}</p></div>
        : (
          <ul className="sk-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {agents.map((a) => { const w = word(a); return (
              <li key={a.id} className="sk-card">
                <span className="sk-row"><span className="sk-card-title sk-grow">{a.name}</span><span className={`sk-pill ${w.cls}`}><span className="sk-dot" />{t(w.key)}</span></span>
                {a.purpose && <span>{a.purpose}</span>}
                <span className="sk-small sk-muted">{a.last_run_at ? t("team.last", { when: when(t, a.last_run_at, lang) }) : t("team.never")}</span>
              </li>
            ); })}
          </ul>
        )}
    </div></div>
  );
}
