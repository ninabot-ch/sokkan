"use client";
// Simple profile — the deliverable: preview, download (Word or text), and the gesture that
// closes the loop: « Validate the deliverable » (who, when, exactly which content — the
// journal keeps it; a later change by the agent shows « changed since your validation »).
import { useCallback, useEffect, useState } from "react";
import { downloadUrl, fetchDeliverable, validateDeliverable, type Deliverable } from "@/lib/simple/api";
import { fullDate, useT, useUI } from "@/lib/simple/i18n";
import { useMe } from "@/lib/me";
import Icon from "./icons";
import Markdown from "./Markdown";

export default function DeliverablePanel({ sid, name, canDrive, onClose, onChanged }: {
  sid: string; name: string; canDrive: boolean; onClose: () => void; onChanged: () => void;
}) {
  const t = useT();
  const { lang } = useUI();
  const me = useMe();
  const [d, setD] = useState<Deliverable | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    setFailed(false);
    fetchDeliverable(sid, name).then(setD).catch(() => setFailed(true));
  }, [sid, name]);
  useEffect(() => { setD(null); load(); }, [load]);

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
          <div className="sk-h3">{d?.title || "…"}</div>
        </div>
        <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={onClose} aria-label={t("close")}><Icon name="close" /></button>
      </div>
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
          ) : null}
          {d.previewable && <a className="sk-btn" href={downloadUrl(sid, name, "docx")} download><Icon name="download" />{t("dl.download.word")}</a>}
          <a className="sk-btn sk-btn-quiet" href={downloadUrl(sid, name, "md")} download><Icon name="download" />{t("dl.download.md")}</a>
          {d.changed_since_validation && <p className="sk-changed sk-small" role="status" style={{ flexBasis: "100%" }}><Icon name="alert" small />{t("dl.changed")}</p>}
          {!d.validated && canDrive && !d.changed_since_validation && <p className="sk-xs sk-muted" style={{ flexBasis: "100%" }}>{t("dl.validate.hint")}</p>}
        </div>
      )}
    </aside>
  );
}
