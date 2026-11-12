"use client";
// Simple profile — one work: the thread in plain language (backend/plain.py), the approval
// card, the deliverable, and ONE composer. No tool name, path or internal brief is shown; the
// raw material stays behind « Technical detail », folded.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError, PlainSocket, fetchWork, keepAsDeliverable, newRequestId, sendMessage,
  type Doc, type Limits, type Source, type TechDetail, type Work,
} from "@/lib/simple/api";
import type { AgentQuestion } from "@/lib/agent";
import { age, useT } from "@/lib/simple/i18n";
import { reduce, type Act, type Approval, type Item } from "@/lib/simple/threadModel";
import type { Key } from "@/lib/simple/dict";
import Composer from "./Composer";
import DeliverablePanel from "./DeliverablePanel";
import Icon, { activityIcon } from "./icons";
import Markdown from "./Markdown";
import { StudyFeedback, StudyTag, useStudy } from "./StudyHooks";

type Side = { kind: "draft"; ev: Approval } | { kind: "deliverable"; name: string } | { kind: "docs" } | null;

const KEEP_FROM = 700;   // an answer this long is probably the document itself

export default function Thread({ sid, limits, onBack, onChanged }: {
  sid: string; limits: Limits | null; onBack: () => void; onChanged: () => void;
}) {
  const t = useT();
  const [work, setWork] = useState<Work | null>(null);
  const [missing, setMissing] = useState(false);
  const [items, setItems] = useState<Item[]>([]);
  const [working, setWorking] = useState(false);
  const [open, setOpen] = useState(false);
  const [side, setSide] = useState<Side>(null);
  const sock = useRef<PlainSocket | null>(null);
  const pendingMsg = useRef<{ sig: string; id: string } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const reload = useCallback(() => {
    fetchWork(sid).then(setWork).catch((e) => { if (e instanceof ApiError && e.status === 404) setMissing(true); });
  }, [sid]);
  useEffect(() => { setWork(null); setMissing(false); setSide(null); reload(); }, [sid, reload]);

  useEffect(() => {
    setItems([]); setWorking(false);
    const s = new PlainSocket(sid, () => setItems([]), (e) => {
      if (e.type === "status") { setWorking(e.state === "working"); if (e.state === "idle") { reload(); onChanged(); } return; }
      if (e.type === "deliverable") { reload(); onChanged(); }
      if (e.type === "approval" || e.type === "approval_resolved") onChanged();
      setItems((cur) => reduce(cur, e));
    }, setOpen);
    sock.current = s;
    return () => { s.close(); sock.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items, working]);

  const decide = (ev: Approval, decision: "allow" | "deny", draftText?: string) => {
    sock.current?.decide(ev.id, decision, draftText);
    setItems((cur) => cur.map((it) => (it.k === "approval" && it.ev.id === ev.id ? { ...it, decided: decision } : it)));
    setSide((s) => (s?.kind === "draft" && s.ev.id === ev.id ? null : s));
  };

  const canDrive = !!work?.can_drive;
  const waiting = items.some((it) => it.k === "approval" && !it.decided);
  const lastDeliverable = work?.deliverables[0];
  // study mode: the work is tagged by its first turn, the two questions come with the
  // validation — the tag is asked again when either can have changed
  const study = useStudy(sid, `${working ? 1 : 0}${lastDeliverable?.validated ? 1 : 0}${work?.deliverables.length || 0}`);
  const liveAct = useMemo(() => {
    const last = items[items.length - 1];
    return last?.k === "acts" && last.acts.some((a) => a.state === "live");
  }, [items]);

  if (missing) {
    return (
      <div className="sk-page"><div className="sk-empty"><Icon name="info" /><p>{t("dl.failed")}</p>
        <button className="sk-btn" onClick={onBack}><Icon name="back" />{t("back")}</button></div></div>
    );
  }

  const pill = waiting ? <span className="sk-pill sk-pill-wait"><span className="sk-dot" />{t("state.waiting")}</span>
    : working ? <span className="sk-pill sk-pill-work"><span className="sk-dot" />{t("state.working")}</span>
    : lastDeliverable?.validated ? <span className="sk-pill sk-pill-ok"><Icon name="check" small />{t("state.validated")}</span>
    : lastDeliverable ? <span className="sk-pill sk-pill-ok"><span className="sk-dot" />{t("state.done")}</span>
    : null;

  return (
    <>
      <header className="sk-thread-head">
        <button className="sk-btn sk-btn-quiet sk-btn-icon sk-only-narrow" onClick={onBack} aria-label={t("back")}><Icon name="back" /></button>
        <div className="sk-grow">
          <h1 className="sk-thread-title" title={work?.title}>{work?.title || "…"}</h1>
          {!open && <div className="sk-xs sk-muted" role="status">{items.length ? t("th.offline") : t("th.connecting")}</div>}
        </div>
        {pill}
        {work && (
          <button className="sk-btn sk-btn-quiet" aria-pressed={side?.kind === "docs"} aria-label={t("th.docs", { n: work.documents.length })}
            onClick={() => setSide(side?.kind === "docs" ? null : { kind: "docs" })}>
            <Icon name="clip" /><span className="sk-hide-narrow">{t("th.docs", { n: work.documents.length })}</span>
            <span className="sk-only-narrow">{work.documents.length}</span>
          </button>
        )}
        {lastDeliverable && (
          <button className="sk-btn sk-open-deliverable" aria-label={t("th.deliverable")} onClick={() => setSide({ kind: "deliverable", name: lastDeliverable.name })}>
            <Icon name="page" /><span className="sk-hide-narrow">{t("th.deliverable")}</span>
          </button>
        )}
      </header>

      {study && canDrive && <StudyTag sid={sid} study={study} />}
      <div className="sk-thread-body">
        <div className="sk-thread-col">
          <div className="sk-scroll" ref={scroller} tabIndex={0} aria-label={work?.title || ""}
            onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120; }}>
            <div className="sk-msgs" aria-live="polite">
              {items.length === 0 && <p className="sk-muted">{open ? t("th.empty") : t("th.connecting")}</p>}
              {items.map((it, i) => {
                if (it.k === "user") return (
                  <div key={i} className="sk-msg-user">
                    <span className="sk-visually-hidden">{t("th.you")} : </span>{it.text}
                    {it.docs.length > 0 && (
                      <div className="sk-msg-docs">{it.docs.map((d) => <span key={d.id} className="sk-chip"><Icon name="page" small /><span className="sk-chip-name">{d.name}</span></span>)}</div>
                    )}
                  </div>
                );
                if (it.k === "text") return (
                  <div key={i} className="sk-msg-agent">
                    <span className="sk-visually-hidden">{t("th.agent")} : </span>
                    <Markdown text={it.text} />
                    {canDrive && it.text.length >= KEEP_FROM && <Keep sid={sid} text={it.text} onKept={(name) => { reload(); onChanged(); setSide({ kind: "deliverable", name }); }} />}
                  </div>
                );
                if (it.k === "acts") return <Activities key={i} acts={it.acts} />;
                if (it.k === "approval") return it.decided
                  ? <div key={i} className="sk-act is-done"><span className="sk-act-ico"><Icon name={it.decided === "allow" ? "check" : "close"} small /></span><span>{t(it.decided === "allow" ? "ap.allowed" : "ap.denied", { what: headline(t, it.ev) })}</span></div>
                  : <ApprovalCard key={i} ev={it.ev} canDrive={canDrive} onDecide={(d) => decide(it.ev, d)}
                      onDraft={() => setSide({ kind: "draft", ev: it.ev })} />;
                if (it.k === "deliverable") return (
                  <div key={i} className="sk-deliv-card">
                    <span className="sk-deliv-ico"><Icon name="page" /></span>
                    <div className="sk-grow">
                      <div className="sk-card-title">{it.title}</div>
                      <div className="sk-small sk-muted">{t("dl.ready")} · {t("dl.version", { n: it.version })}</div>
                    </div>
                    <button className="sk-btn sk-btn-primary" onClick={() => setSide({ kind: "deliverable", name: it.name })}>
                      <Icon name="eye" />{t("dl.open")}
                    </button>
                  </div>
                );
                if (it.k === "notice") return (
                  <div key={i} className="sk-notice" role="status">
                    <Icon name="alert" />
                    <div className="sk-grow">
                      <div>{t((`no.${["busy", "budget", "engine", "read_only", "mode_locked", "sandbox", "study_expired", "study_mode_required", "user_daily_cap", "user_daily_cap_local", "user_limit_reached"].includes(it.code) ? it.code : "engine"}`) as Key)}</div>
                      <Tech detail={{ result: it.detail }} />
                    </div>
                  </div>
                );
                return <Question key={i} id={it.id} questions={it.questions} disabled={!canDrive}
                  onAnswer={(a) => { sock.current?.answer(it.id, a); setItems((cur) => cur.filter((x) => !(x.k === "question" && x.id === it.id))); }} />;
              })}
              {study?.tag?.tagged && canDrive && lastDeliverable?.validated && <StudyFeedback sid={sid} />}
              {working && !liveAct && !waiting && (
                <Working />
              )}
            </div>
          </div>
          <div className="sk-compose-dock">
            <div>
              {work && !canDrive ? <p className="sk-small sk-muted">{t("th.readonly")}</p> : (
                <div style={{ display: "flex", flexDirection: "column", gap: "var(--sk-s2)" }}>
                  {working && !waiting && (
                    <div className="sk-row">
                      <button className="sk-btn sk-push" onClick={() => sock.current?.interrupt()}><Icon name="stop" />{t("th.stop")}</button>
                    </div>
                  )}
                  <Composer compact session={sid} limits={limits} placeholder={t("th.reply")} submitLabel={t("th.send")}
                    disabled={!work || !open}
                    onSubmit={async (text, docs: Doc[]) => {
                      // one id per message: kept while the same message is tried again, so a second
                      // click after a lost answer does not send it twice (3.6.4)
                      const sig = JSON.stringify([text, docs.map((d) => d.id)]);
                      if (pendingMsg.current?.sig !== sig) pendingMsg.current = { sig, id: newRequestId() };
                      try {
                        await sendMessage(sid, text, docs.map((d) => d.id), pendingMsg.current.id);
                        pendingMsg.current = null; stick.current = true; if (docs.length) reload();
                      } catch (e) {
                        if (e instanceof ApiError && e.status < 500) pendingMsg.current = null;   // refused: nothing was sent
                        throw new Error(e instanceof ApiError && e.code === "busy" ? t("no.busy") : t("th.send.failed"));
                      }
                    }} />
                </div>
              )}
            </div>
          </div>
        </div>

        {side?.kind === "draft" && (
          <DraftPanel ev={side.ev} onClose={() => setSide(null)} onDecide={(d, text) => decide(side.ev, d, text)}
            pending={canDrive && items.some((it) => it.k === "approval" && it.ev.id === side.ev.id && !it.decided)} />
        )}
        {side?.kind === "deliverable" && (
          <DeliverablePanel sid={sid} name={side.name} canDrive={canDrive} onClose={() => setSide(null)}
            rev={work?.deliverables.find((x) => x.name === side.name)?.updated_at}
            onChanged={() => { reload(); onChanged(); }} />
        )}
        {side?.kind === "docs" && work && (
          <aside className="sk-panel" aria-label={t("th.docs.title")}>
            <div className="sk-panel-head">
              <div className="sk-h3 sk-grow">{t("th.docs.title")}</div>
              <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={() => setSide(null)} aria-label={t("close")}><Icon name="close" /></button>
            </div>
            <div className="sk-panel-body">
              {work.documents.length === 0 ? <p className="sk-muted">{t("th.docs.none")}</p> : (
                <div className="sk-list">{work.documents.map((d) => <SourceRow key={d.id} d={d} />)}</div>
              )}
            </div>
          </aside>
        )}
      </div>
    </>
  );
}

/** The draft before it is saved: read it, and — when it is a whole document — correct it; the
 *  save then carries the corrected text (the agent's path and everything else untouched). */
function DraftPanel({ ev, pending, onClose, onDecide }: {
  ev: Approval; pending: boolean; onClose: () => void; onDecide: (d: "allow" | "deny", text?: string) => void;
}) {
  const t = useT();
  const original = ev.draft?.text || "";
  const editable = pending && !!ev.draft && !ev.draft.partial && !ev.draft.truncated && ev.kind === "write_draft";
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(original);
  const changed = text !== original && text.trim().length > 0;
  return (
    <aside className="sk-panel" aria-label={t("ap.draft.title")}>
      <div className="sk-panel-head">
        <div className="sk-grow"><div className="sk-xs sk-muted">{t("ap.draft.title")}</div><div className="sk-h3">{versioned(t, ev)}</div></div>
        <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={onClose} aria-label={t("close")}><Icon name="close" /></button>
      </div>
      <div className="sk-panel-body" tabIndex={0}>
        {ev.draft?.partial && <p className="sk-small sk-muted">{t("ap.partial")}</p>}
        {editing ? (
          <>
            <label className="sk-visually-hidden" htmlFor={`sk-draft-${ev.id}`}>{t("ap.edit.label")}</label>
            <textarea id={`sk-draft-${ev.id}`} className="sk-draft-edit" value={text} onChange={(e) => setText(e.target.value)} />
          </>
        ) : <Markdown text={text} />}
      </div>
      {pending && (
        <div className="sk-panel-foot">
          <button className="sk-btn sk-btn-primary" onClick={() => onDecide("allow", changed ? text : undefined)}>
            <Icon name="check" />{changed ? t("ap.allow.edited") : t("ap.allow")}
          </button>
          {editable && <button className="sk-btn" onClick={() => setEditing(!editing)}><Icon name={editing ? "eye" : "pen"} />{editing ? t("ap.edit.done") : t("ap.edit")}</button>}
          <button className="sk-btn" onClick={() => onDecide("deny")}>{t("ap.deny")}</button>
        </div>
      )}
    </aside>
  );
}

/** « The agent is working… », then — a full document takes minutes — what to expect. */
function Working() {
  const t = useT();
  const [long, setLong] = useState(false);
  useEffect(() => { const id = window.setTimeout(() => setLong(true), 15000); return () => window.clearTimeout(id); }, []);
  return <div className="sk-working" role="status"><span className="sk-spin" aria-hidden />{t(long ? "th.working.long" : "th.working")}</div>;
}

function SourceRow({ d }: { d: Doc }) {
  const t = useT();
  const a = age(t, d.modified_at);
  return (
    <div className="sk-doc-row">
      <span className="sk-doc-ext">{d.ext}</span>
      <div className="sk-grow">
        <div className="sk-doc-name">{d.name}</div>
        <div className={`sk-small sk-muted sk-source-age${a.old ? " is-old" : ""}`}>{a.text}{d.text_state !== "ok" && d.text_state !== "partial" ? ` · ${t("up.text.none")}` : ""}</div>
      </div>
    </div>
  );
}

function versioned(t: ReturnType<typeof useT>, ev: Approval): string {
  return ev.version > 1 ? t("ap.version", { title: ev.title, n: ev.version }) : ev.title;
}

function headline(t: ReturnType<typeof useT>, ev: Approval): string {
  const known = ["write_draft", "write_file", "save_memory", "run_command", "web_search", "web_read", "delegate"];
  const key = (`ap.${known.includes(ev.kind) ? ev.kind : "other"}`) as Key;
  return t(key, { title: versioned(t, ev) });
}

function ApprovalCard({ ev, canDrive, onDecide, onDraft }: {
  ev: Approval; canDrive: boolean; onDecide: (d: "allow" | "deny") => void; onDraft: () => void;
}) {
  const t = useT();
  const n = ev.sources.length;
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { ref.current?.scrollIntoView({ block: "nearest" }); }, []);
  const isDoc = ev.kind === "write_draft" || ev.kind === "write_file";
  return (
    <section className="sk-approval" ref={ref} aria-labelledby={`ap-${ev.id}`}>
      <div className="sk-approval-eyebrow"><Icon name="shield" small />{t("ap.eyebrow")}</div>
      <h2 className="sk-approval-title" id={`ap-${ev.id}`}>{headline(t, ev)}</h2>
      <p className="sk-approval-note">
        {isDoc && (n > 1 ? t("ap.sources.n", { n }) : n === 1 ? t("ap.sources.1") : ev.attached > 0 ? t("ap.sources.0", { n: ev.attached }) : "")}
        {isDoc && " "}
        {ev.stays_inside ? t("ap.inside") : ev.kind.startsWith("web_") ? t("ap.outside") : t("ap.unknown")}
      </p>
      {n > 0 && (
        <ul className="sk-sources" style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {ev.sources.map((s: Source) => {
            const a = age(t, s.modified_at);
            return (
              <li key={s.id} className="sk-source"><Icon name="page" small />
                <span className="sk-chip-name">{s.name}</span>
                <span className={`sk-source-age sk-muted${a.old ? " is-old" : ""}`}>· {a.text}</span>
              </li>
            );
          })}
        </ul>
      )}
      {canDrive ? (
        <div className="sk-actions">
          <button className="sk-btn sk-btn-primary" onClick={() => onDecide("allow")}><Icon name="check" />{t("ap.allow")}</button>
          {ev.draft && <button className="sk-btn" onClick={onDraft}><Icon name="eye" />{t("ap.draft")}</button>}
          <button className="sk-btn" onClick={() => onDecide("deny")}>{t("ap.deny")}</button>
        </div>
      ) : <p className="sk-small sk-muted">{t("ap.readonly")}</p>}
      <p className="sk-xs sk-muted">{t("ap.mode")}</p>
      <Tech detail={ev.detail} />
    </section>
  );
}

function Activities({ acts }: { acts: Act[] }) {
  const t = useT();
  const sentence = (a: Act) => {
    if (a.state === "fail") return t("act.failed");
    const base = `act.${a.kind}${a.state === "done" ? ".done" : ""}`;
    const text = t(base as Key, { subject: a.subject });
    return text === base ? t((`act.other${a.state === "done" ? ".done" : ""}`) as Key) : text;
  };
  // the same sentence several times in a row (three searches) is said once
  const rows: (Act & { n: number })[] = [];
  for (const a of acts) {
    const last = rows[rows.length - 1];
    if (last && last.state === a.state && sentence(last) === sentence(a)) last.n += 1;
    else rows.push({ ...a, n: 1 });
  }
  return (
    <div className="sk-activity">
      {rows.map((a) => (
        <div key={a.id} className={`sk-act is-${a.state}`}>
          {a.state === "live" ? <span className="sk-spin" aria-hidden /> : <span className="sk-act-ico"><Icon name={a.state === "fail" ? "alert" : activityIcon(a.kind)} small /></span>}
          <span>{sentence(a)}{a.n > 1 ? ` (×${a.n})` : ""}</span>
        </div>
      ))}
      <Tech detail={{ title: acts.map((a) => `${a.detail.tool || ""}  ${a.detail.title || ""}`).join("\n") }} />
    </div>
  );
}

/** Folded by default: what a technician would want to see, never needed to do the task. */
function Tech({ detail }: { detail: TechDetail }) {
  const t = useT();
  const body = [detail.tool, detail.title, detail.input ? JSON.stringify(detail.input, null, 2) : "", detail.result]
    .filter((x) => x && String(x).trim()).join("\n");
  if (!body.trim()) return null;
  return (
    <details className="sk-tech">
      <summary><Icon name="chevron" small />{t("th.tech")}</summary>
      <div className="sk-tech-body">{body}</div>
    </details>
  );
}

function Keep({ sid, text, onKept }: { sid: string; text: string; onKept: (name: string) => void }) {
  const t = useT();
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  if (state === "done") return <span className="sk-small sk-validated"><Icon name="check" small />{t("th.kept")}</span>;
  return (
    <button className="sk-btn sk-btn-quiet" disabled={state === "busy"} onClick={async () => {
      setState("busy");
      try { const r = await keepAsDeliverable(sid, "", text); setState("done"); onKept(r.name); } catch { setState("idle"); }
    }}><Icon name="download" />{t("th.keep")}</button>
  );
}

function Question({ id, questions, disabled, onAnswer }: {
  id: string; questions: AgentQuestion[]; disabled: boolean; onAnswer: (a: Record<string, string | string[]>) => void;
}) {
  const t = useT();
  const [sel, setSel] = useState<Record<string, string[]>>({});
  const ready = questions.every((q) => (sel[q.question] || []).length > 0);
  return (
    <section className="sk-question" aria-labelledby={`q-${id}`}>
      {questions.map((q, qi) => (
        <fieldset key={qi}>
          <legend className="sk-legend" id={qi === 0 ? `q-${id}` : undefined}>{q.question}</legend>
          <div className="sk-row">
            {q.options.map((o) => {
              const on = (sel[q.question] || []).includes(o.label);
              return (
                <button key={o.label} type="button" className="sk-opt" aria-pressed={on} disabled={disabled} title={o.description}
                  onClick={() => setSel((s) => {
                    const cur = s[q.question] || [];
                    return { ...s, [q.question]: q.multiSelect ? (on ? cur.filter((x) => x !== o.label) : [...cur, o.label]) : [o.label] };
                  })}>{o.label}</button>
              );
            })}
          </div>
        </fieldset>
      ))}
      <div><button className="sk-btn sk-btn-primary" disabled={disabled || !ready} onClick={() => {
        const a: Record<string, string | string[]> = {};
        for (const q of questions) a[q.question] = q.multiSelect ? (sel[q.question] || []) : (sel[q.question] || [""])[0];
        onAnswer(a);
      }}>{t("q.submit")}</button></div>
    </section>
  );
}
