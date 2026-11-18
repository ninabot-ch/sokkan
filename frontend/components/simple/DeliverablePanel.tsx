"use client";
// Simple profile — the deliverable: preview, download (Word or text), and the gesture that
// closes the loop: « Validate the deliverable » (who, when, exactly which content — the
// journal keeps it; a later change by the agent shows « changed since your validation »).
import { useCallback, useEffect, useState } from "react";
import { downloadUrl, fetchDeliverable, validateDeliverable, type Deliverable } from "@/lib/simple/api";
import { fullDate, useT, useUI } from "@/lib/simple/i18n";
import type { Key } from "@/lib/simple/dict";
import { useMe } from "@/lib/me";
import { useFeatures } from "@/lib/features";
import Icon from "./icons";
import Markdown from "./Markdown";
import QualityCheck from "./QualityCheck";
import EngineBadge from "./EngineBadge";
import LearnOffer from "./LearnOffer";

export default function DeliverablePanel({ sid, name, rev, canDrive, onClose, onChanged }: {
  /** changes when the file changed on disk (a new version was saved): the preview reloads */
  sid: string; name: string; rev?: number; canDrive: boolean; onClose: () => void; onChanged: () => void;
}) {
  const t = useT();
  const { lang } = useUI();
  const me = useMe();
  const demo = !!useFeatures().demo_atelier;
  const [d, setD] = useState<Deliverable | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    setFailed(false);
    fetchDeliverable(sid, name).then(setD).catch(() => setFailed(true));
  }, [sid, name]);
  useEffect(() => { setD(null); load(); }, [load]);
  useEffect(() => { if (rev) load(); }, [rev, load]);

  const validate = async () => {
    setBusy(true);
    try { await validateDeliverable(sid, name); load(); onChanged(); } catch { setFailed(true); } finally { setBusy(false); }
  };
  const mine = d && me && d.validated_by === (me.email || "").toLowerCase();

  return (
    <aside className="sk-panel" aria-label={t("th.deliverable")}>
      <div className="sk-panel-head">
        <div className="sk-grow">
          <div className="sk-xs sk-muted">{t("th.deliverable")}{d ? ` · ${t("dl.version", { n: d.version })}` : ""}</div>
          {d?.engine && <EngineBadge engine={d.engine} />}
          <div className="sk-h3">{d?.title || "…"}</div>
        </div>
        <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={onClose} aria-label={t("close")}><Icon name="close" /></button>
      </div>
      {d?.check && !d.validated && <div className="sk-panel-check"><QualityCheck check={d.check} /></div>}
      <div className="sk-panel-body sk-paper" tabIndex={0}>
        {failed ? (
          <div className="sk-empty"><Icon name="alert" /><p>{t("dl.failed")}</p><button className="sk-btn" onClick={load}>{t("retry")}</button></div>
        ) : !d ? <div className="sk-skel" aria-label={t("loading")} /> : <Markdown text={d.text || ""} />}
      </div>
      {d && (
        <div className="sk-panel-foot">
          {d.validated ? (
            <span className="sk-validated" role="status"><Icon name="seal" />
              {mine ? t("dl.validated", { date: fullDate(d.validated_at || 0, lang) })
                : t("dl.validated.by", { who: d.validated_by, date: fullDate(d.validated_at || 0, lang) })}</span>
          ) : canDrive ? (
            <button className="sk-btn sk-btn-primary" disabled={busy} onClick={validate}>
              {busy ? <><span className="sk-spin" aria-hidden />{t("dl.validating")}</> : <><Icon name="seal" />{t("dl.validate")}</>}
            </button>
          ) : demo ? (
            <p className="sk-small sk-demo-pending" role="status" style={{ flexBasis: "100%" }}><Icon name="info" small />{t("demo.dl.pending")}</p>
          ) : null}
          {(d.formats ?? (d.previewable ? ["docx"] : [])).map((f, i) => (
            <a key={f} className={i === 0 ? "sk-btn" : "sk-btn sk-btn-quiet"} href={downloadUrl(sid, name, f)} download>
              <Icon name="download" />{t(`dl.download.${f}` as Key)}</a>))}
          <a className="sk-btn sk-btn-quiet" href={downloadUrl(sid, name, "md")} download><Icon name="download" />{t("dl.download.md")}</a>
          {d.kind && d.kind !== "document" && <p className="sk-xs sk-muted" style={{ flexBasis: "100%" }}>{t(`dl.kind.${d.kind}` as Key)}</p>}
          {d.changed_since_validation && <p className="sk-changed sk-small" role="status" style={{ flexBasis: "100%" }}><Icon name="alert" small />{t("dl.changed")}</p>}
          {!d.validated && canDrive && !d.changed_since_validation && <p className="sk-xs sk-muted" style={{ flexBasis: "100%" }}>{t("dl.validate.hint")}</p>}
          {d.validated && canDrive && !demo && <LearnOffer sid={sid} name={name} validatedAt={d.validated_at} />}
        </div>
      )}
    </aside>
  );
}
