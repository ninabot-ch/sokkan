"use client";
// Review of the ingestion batches (feature `doc_ingest`): what the memory agent proposed from
// the organisation's documents waits here, by batch, until a person decides. One screen, two
// places: the simple profile (« Notes to review ») and the technical cockpit (Setup ›
// Ingestion, embedded with the same tokens). Order of the page = order of the review: the
// batch in figures, the flagged notes first, a sample to read, then the decision.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/simple/api";
import type { Key } from "@/lib/simple/dict";
import { useT, useUI, when, type T } from "@/lib/simple/i18n";
import {
  approveBatch, approveNote, batchFlagged, batchNote, batchSample, batchStats, ingestBatches, ingestStatus,
  noteBody, noteHead, rejectBatch, rejectNote, type BatchNote, type BatchRow, type BatchStats, type IngestStatus,
} from "@/lib/simple/ingest";
import Icon from "./icons";
import Markdown from "./Markdown";

const SAMPLE = 10;
const FLAGS = ["quote_not_found", "unsupported_figure", "conflict", "injection_suspected", "invented_link",
  "truncated_source", "level_inherited", "updates_existing", "previous_version_notes"];

const flagLabel = (t: T, f: string) => (FLAGS.includes(f) ? t(`ing.flag.${f}` as Key) : t("ing.flag.other", { flag: f }));
// refusals a reviewer can act on are said in the screen's language; the rest keeps the API's words
const why = (t: T, e: unknown) => (e instanceof ApiError && e.code === "memory_store_required" ? t("ing.err.store")
  : e instanceof ApiError ? e.message : String((e as Error)?.message || e));
const todo = (b: BatchRow) => (b.status === "pending" || b.status === "partial") && (b.counts?.pending || 0) > 0;

function StatusPill({ status }: { status: BatchRow["status"] }) {
  const t = useT();
  const cls = status === "approved" ? "sk-pill-ok" : status === "rejected" ? "sk-pill-idle"
    : status === "open" ? "sk-pill-work" : "sk-pill-wait";
  return <span className={`sk-pill ${cls}`}><span className="sk-dot" />{t(`ing.status.${status}` as Key)}</span>;
}

export default function IngestReview({ onCount }: { onCount?: (pending: number) => void }) {
  const t = useT();
  const { lang } = useUI();
  const [status, setStatus] = useState<IngestStatus | null>(null);
  const [rows, setRows] = useState<BatchRow[] | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "denied" | "failed">("loading");
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<number | null>(null);

  const load = useCallback(() => {
    Promise.all([ingestStatus(), ingestBatches()])
      .then(([s, b]) => { setStatus(s); setRows(b.batches); setState("ok"); onCount?.(b.batches.filter(todo).length); })
      .catch((e) => setState(e instanceof ApiError && (e.status === 403 || e.status === 404) ? "denied" : "failed"));
  }, [onCount]);
  useEffect(load, [load]);

  if (open !== null) return <Batch id={open} onBack={() => { setOpen(null); load(); }} />;

  const waiting = (rows || []).filter(todo);
  const shown = all ? rows || [] : waiting;
  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div><h1 className="sk-h1">{t("ing.title")}</h1><p className="sk-lede">{t("ing.lede")}</p></div>
      {state === "loading" && <div className="sk-list" aria-busy="true" aria-label={t("loading")}><div className="sk-skel" /><div className="sk-skel" /></div>}
      {state === "denied" && <div className="sk-empty"><Icon name="shield" /><p>{t("ing.denied")}</p></div>}
      {state === "failed" && <div className="sk-empty" role="alert"><Icon name="alert" /><p>{t("ing.failed")}</p><button className="sk-btn" onClick={load}>{t("retry")}</button></div>}
      {state === "ok" && status?.model.not_ready && (
        <div className="sk-notice" role="status"><Icon name="info" /><span>{t("ing.model.missing", { why: status.model.not_ready })}</span></div>
      )}
      {state === "ok" && (
        <>
          <div className="sk-seg" role="group" aria-label={t("ing.title")}>
            <button aria-pressed={!all} onClick={() => setAll(false)}>{t("ing.filter.todo", { n: waiting.length })}</button>
            <button aria-pressed={all} onClick={() => setAll(true)}>{t("ing.filter.all")}</button>
          </div>
          {shown.length === 0 ? (
            <div className="sk-empty"><Icon name="seal" /><p>{t(all ? "ing.empty.all" : "ing.empty.todo")}</p></div>
          ) : (
            <ul className="sk-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {shown.map((b) => {
                const notes = Object.values(b.counts || {}).reduce((a, n) => a + n, 0);
                return (
                  <li key={b.id}>
                    <button className="sk-card sk-card-btn" onClick={() => setOpen(b.id)}>
                      <span className="sk-batch">
                        <span className="sk-card-title">{t("ing.batch", { id: b.id })} <span className="sk-muted sk-small">· {t("ing.batch.source", { source: b.source })}</span></span>
                        <StatusPill status={b.status} />
                      </span>
                      <span className="sk-small sk-muted">{t("ing.batch.line", { notes, when: when(t, b.closed_at || b.created_at, lang) })}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          <p className="sk-small sk-muted">{t("ing.journal")}</p>
        </>
      )}
    </div></div>
  );
}

// ---- one batch ---------------------------------------------------------------------------------
function Batch({ id, onBack }: { id: number; onBack: () => void }) {
  const t = useT();
  const { lang } = useUI();
  const [stats, setStats] = useState<BatchStats | null>(null);
  const [flagged, setFlagged] = useState<BatchNote[]>([]);
  const [sample, setSample] = useState<BatchNote[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string[]>([]);
  const [rejected, setRejected] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const top = useRef<HTMLHeadingElement>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, f] = await Promise.all([batchStats(id), batchFlagged(id)]);
      setStats(s);
      // a flagged note that was just decided stays on screen with its decision
      setFlagged((cur) => [...cur.filter((c) => c.status !== "pending" && !f.notes.some((n) => n.name === c.name)), ...f.notes]);
      setFailed(false);
    } catch { setFailed(true); }
  }, [id]);
  useEffect(() => { refresh(); top.current?.focus(); }, [refresh]);

  // an action that fails says so WHERE it was asked: on the note, or above the batch's decision
  const [noteError, setNoteError] = useState<{ name: string; text: string } | null>(null);
  const run = async (fn: () => Promise<void>, note = "") => {
    setBusy(true); setError(""); setNoteError(null);
    try { await fn(); } catch (e) {
      const text = t("ing.error", { why: why(t, e) });
      if (note) setNoteError({ name: note, text }); else setError(text);
    } finally { setBusy(false); }
  };
  const draw = () => run(async () => { const s = await batchSample(id, SAMPLE); setSample(s.notes); await refresh(); });
  const decided = (n: BatchNote, status: "approved" | "rejected") => {
    const set = (list: BatchNote[]) => list.map((x) => (x.name === n.name ? { ...x, status } : x));
    setFlagged(set); setSample((cur) => (cur ? set(cur) : cur));
  };
  const onNote = (n: BatchNote, action: "approve" | "reject", r = "") => run(async () => {
    if (action === "approve") await approveNote(id, n.name); else await rejectNote(id, n.name, r);
    decided(n, action === "approve" ? "approved" : "rejected");
    await refresh();
  }, n.name);

  if (failed && !stats) {
    return <div className="sk-scroll"><div className="sk-page"><div className="sk-empty" role="alert"><Icon name="alert" /><p>{t("ing.failed")}</p>
      <button className="sk-btn" onClick={onBack}><Icon name="back" />{t("ing.back")}</button></div></div></div>;
  }
  if (!stats) return <div className="sk-scroll"><div className="sk-page"><div className="sk-skel" aria-label={t("loading")} /><div className="sk-skel" /></div></div>;

  const b = stats.batch;
  const pending = stats.by_status.pending || 0;
  const pendingFlagged = flagged.filter((n) => n.status === "pending").length;
  const bulk = Math.max(0, pending - pendingFlagged);
  const min = Math.min(stats.review_min_sample, pending);
  const seen = Math.min(stats.viewed_by_you, min);
  const ready = stats.viewed_by_you >= min;
  const live = b.status === "pending" || b.status === "partial";
  const stat = (n: number, key: Key, flag = false) => (
    <div className={`sk-stat${flag && n > 0 ? " is-flag" : ""}`}><b>{n}</b><span>{t(key)}</span></div>
  );
  const levels = Object.entries(stats.by_level).map(([k, n]) => `${k} (${n})`).join(", ");
  const sampleOnly = (sample || []).filter((n) => !flagged.some((f) => f.name === n.name));

  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div><button className="sk-btn sk-btn-quiet" onClick={onBack}><Icon name="back" />{t("ing.back")}</button></div>
      <div className="sk-row">
        <h1 className="sk-h2 sk-grow" tabIndex={-1} ref={top}>{t("ing.batch", { id: b.id })} — {t("ing.batch.source", { source: b.source })}</h1>
        <StatusPill status={b.status} />
      </div>
      <p className="sk-small sk-muted">{t("ing.batch.line", { notes: stats.notes, when: when(t, b.closed_at || b.created_at, lang) })}{levels ? ` · ${t("ing.levels", { list: levels })}` : ""}</p>

      <div className="sk-stats">
        {stat(stats.documents, "ing.stat.documents")}
        {stat(stats.notes, "ing.stat.notes")}
        {stat(stats.by_type.decision || 0, "ing.stat.decision")}
        {stat(stats.by_type.reference || 0, "ing.stat.reference")}
        {stat(stats.flagged, "ing.stat.flagged", true)}
        {stat(pending, "ing.stat.pending")}
        {(stats.by_status.approved || 0) > 0 && stat(stats.by_status.approved, "ing.stat.approved")}
        {(stats.by_status.rejected || 0) > 0 && stat(stats.by_status.rejected, "ing.stat.rejected")}
        {stats.abstained.length > 0 && stat(stats.abstained.length, "ing.stat.abstained")}
        {stats.duplicates.length > 0 && stat(stats.duplicates.length, "ing.stat.duplicates")}
        {stats.errors.length > 0 && stat(stats.errors.length, "ing.stat.errors", true)}
      </div>
      {stats.hidden_above_clearance > 0 && (
        <div className="sk-notice" role="status"><Icon name="shield" /><span>{t("ing.hidden", { n: stats.hidden_above_clearance })}</span></div>
      )}

      <section aria-labelledby="sk-ing-flag" className="sk-list">
        <div className="sk-section-head"><h2 className="sk-h3" id="sk-ing-flag">{t("ing.flagged.title", { n: flagged.length })}</h2></div>
        {flagged.length === 0 ? <p className="sk-muted">{t("ing.flagged.none")}</p> : <p className="sk-muted">{t("ing.flagged.help")}</p>}
        {flagged.map((n) => <Note key={n.name} batch={id} n={n} live={live} busy={busy} onDecide={onNote} onOpened={refresh} error={noteError?.name === n.name ? noteError.text : ""} />)}
      </section>

      {live && (
        <section aria-labelledby="sk-ing-sample" className="sk-list">
          <div className="sk-section-head"><h2 className="sk-h3" id="sk-ing-sample">{t("ing.sample.title")}</h2></div>
          <p className="sk-muted">{t("ing.sample.help", { min: stats.review_min_sample })}</p>
          <div><button className="sk-btn" disabled={busy || pending === 0} onClick={draw}>
            <Icon name="eye" />{t(sample ? "ing.sample.again" : "ing.sample.draw", { n: SAMPLE })}</button></div>
          {sample && sampleOnly.length === 0 && sample.length === 0 && <p className="sk-muted">{t("ing.sample.empty")}</p>}
          {sampleOnly.map((n) => <Note key={n.name} batch={id} n={n} live={live} busy={busy} onDecide={onNote} onOpened={refresh} error={noteError?.name === n.name ? noteError.text : ""} />)}
        </section>
      )}

      {done.length > 0 && <div className={`sk-notice${rejected ? "" : " is-ok"}`} role="status"><Icon name={rejected ? "info" : "check"} /><div>{done.map((d) => <div key={d}>{d}</div>)}</div></div>}
      {error && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{error}</span></div>}

      {live && (
        <div className="sk-decide" role="group" aria-label={t("ing.batch", { id: b.id })}>
          <div className="sk-row">
            <span className="sk-small sk-grow" role="status">{ready ? t("ing.progress.ok") : t("ing.progress", { seen, min })}</span>
            <span className="sk-xs sk-muted">{t("ing.journal")}</span>
          </div>
          <div className="sk-progress" aria-hidden><i style={{ width: `${min ? Math.min(100, Math.round((seen / min) * 100)) : 100}%` }} /></div>
          {rejecting ? (
            <div className="sk-reason">
              <label className="sk-visually-hidden" htmlFor="sk-ing-why">{t("ing.reason")}</label>
              <input id="sk-ing-why" className="sk-input" value={reason} placeholder={t("ing.reason")} maxLength={300} autoFocus
                onChange={(e) => setReason(e.target.value)} />
              <button className="sk-btn sk-btn-danger" disabled={busy} onClick={() => run(async () => {
                const r = await rejectBatch(id, reason.trim());
                setDone([t("ing.done.rejected", { n: r.rejected })]); setRejected(true); setRejecting(false); setReason("");
                setFlagged((cur) => cur.map((x) => (x.status === "pending" ? { ...x, status: "rejected" } : x)));
                setSample((cur) => (cur ? cur.map((x) => (x.status === "pending" ? { ...x, status: "rejected" } : x)) : cur));
                await refresh();
              })}>{t("ing.reject.confirm", { n: pending })}</button>
              <button className="sk-btn sk-btn-quiet" disabled={busy} onClick={() => setRejecting(false)}>{t("cancel")}</button>
            </div>
          ) : (
            <div className="sk-actions">
              <button className="sk-btn sk-btn-primary" disabled={busy || !ready || bulk === 0} onClick={() => run(async () => {
                const r = await approveBatch(id);
                const said = [t("ing.done.approved", { n: r.approved })];
                if (r.left_pending.length) said.push(t("ing.done.left", { n: r.left_pending.length }));
                if (r.failed.length) said.push(t("ing.done.failed", { n: r.failed.length }));
                setDone(said); setRejected(false);
                setSample((cur) => (cur ? cur.map((x) => (x.status === "pending" && !x.flags.length ? { ...x, status: "approved" } : x)) : cur));
                await refresh();
              })}><Icon name="check" />{bulk > 0 ? t("ing.approve", { n: bulk }) : t("ing.approve.none")}</button>
              <button className="sk-btn sk-btn-danger" disabled={busy || pending === 0} onClick={() => setRejecting(true)}>{t("ing.reject")}</button>
            </div>
          )}
        </div>
      )}
    </div></div>
  );
}

// ---- one note ----------------------------------------------------------------------------------
function Note({ batch, n, live, busy, onDecide, onOpened, error }: {
  batch: number; n: BatchNote; live: boolean; busy: boolean; error: string;
  onDecide: (n: BatchNote, action: "approve" | "reject", reason?: string) => void; onOpened: () => void;
}) {
  const t = useT();
  const [text, setText] = useState<string | null>(n.text ?? null);
  const [shown, setShown] = useState(n.text !== undefined);
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const pending = n.status === "pending";
  const head = noteHead(n);
  const read = async () => {
    if (shown) { setShown(false); return; }
    if (text === null) {
      try { setText((await batchNote(batch, n.name)).text || ""); onOpened(); } catch { setText(""); }
    }
    setShown(true);
  };
  const cls = n.status === "approved" ? " is-approved" : n.status === "rejected" ? " is-rejected" : n.flags.length ? " is-flagged" : "";
  return (
    <article className={`sk-note${cls}`} aria-label={head.title}>
      <div className="sk-row">
        <span className="sk-pill sk-pill-idle">{n.type === "decision" ? t("ing.type.decision") : n.type === "reference" ? t("ing.type.reference") : n.type}</span>
        <span className="sk-pill sk-pill-idle">{n.level_id}</span>
        {n.flags.map((f) => <span key={f} className="sk-pill sk-pill-flag"><Icon name="alert" small />{flagLabel(t, f)}</span>)}
      </div>
      <div className="sk-note-title">{head.title}</div>
      {head.more && <div className="sk-muted">{head.more}</div>}
      <div className="sk-small sk-muted">{t("ing.source")} <span className="sk-note-src">{n.rel}</span></div>
      {shown && text !== null && <div className="sk-note-body"><Markdown text={noteBody(text)} /></div>}
      <div className="sk-actions">
        <button className="sk-btn sk-btn-quiet" aria-expanded={shown} onClick={read}><Icon name="eye" />{t(shown ? "ing.read.hide" : "ing.read")}</button>
        {!pending && (
          <span className={n.status === "approved" ? "sk-validated" : "sk-muted"} role="status">
            {n.status === "approved" && <Icon name="check" small />}{t(n.status === "approved" ? "ing.note.approved" : "ing.note.rejected")}
          </span>
        )}
        {pending && live && !asking && (
          <>
            <button className="sk-btn" disabled={busy || !shown} title={shown ? undefined : t("ing.read")} onClick={() => onDecide(n, "approve")}><Icon name="check" />{t("ing.note.approve")}</button>
            <button className="sk-btn sk-btn-danger" disabled={busy} onClick={() => setAsking(true)}>{t("ing.note.reject")}</button>
          </>
        )}
      </div>
      {error && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{error}</span></div>}
      {pending && live && asking && (
        <div className="sk-reason">
          <label className="sk-visually-hidden" htmlFor={`sk-why-${n.name}`}>{t("ing.reason")}</label>
          <input id={`sk-why-${n.name}`} className="sk-input" value={reason} placeholder={t("ing.reason")} maxLength={300} autoFocus
            onChange={(e) => setReason(e.target.value)} />
          <button className="sk-btn sk-btn-danger" disabled={busy} onClick={() => { onDecide(n, "reject", reason.trim()); setAsking(false); }}>{t("ing.reason.confirm")}</button>
          <button className="sk-btn sk-btn-quiet" disabled={busy} onClick={() => setAsking(false)}>{t("cancel")}</button>
        </div>
      )}
    </article>
  );
}
