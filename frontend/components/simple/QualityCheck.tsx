"use client";
// 4.0 — what the session checked before showing a deliverable (backend/qualitygate.py): the
// approval card and the deliverable say it, so a document with a known inconsistency is never
// presented as finished.
import type { QualityCheck as Check } from "@/lib/simple/api";
import { useT } from "@/lib/simple/i18n";
import Icon from "./icons";

export default function QualityCheck({ check }: { check: Check | null | undefined }) {
  const t = useT();
  if (!check) return null;
  const warn = !check.ok;
  const fixed = check.fixed || [];
  return (
    <div className={`sk-qc${warn ? " is-warn" : ""}`} role={warn ? "alert" : "note"}>
      <div className="sk-qc-title"><Icon name={warn ? "info" : "check"} small />{t(warn ? "qc.title.warn" : "qc.title.ok")}</div>
      <p className="sk-small sk-muted">{t(`qc.by.${check.reviewed_by}`)}{check.corrected ? ` ${t(fixed.length ? "qc.corrected.list" : "qc.corrected")}` : ""}</p>
      {check.review_failed && <p className="sk-small sk-muted">{t("qc.failed")}</p>}
      {check.remaining.length > 0 && (
        <>
          <div className="sk-qc-sub">{t("qc.remaining")}</div>
          <ul className="sk-qc-list">{check.remaining.map((x, i) => <li key={`r${i}`}>{x}</li>)}</ul>
        </>
      )}
      {(fixed.length > 0 || check.verified.length > 0) && (
        // 4.0.1: open by default, and WHAT the agent corrected comes first, in words
        <details className="sk-qc-more" open>
          <summary>{t("qc.verified")} ({fixed.length + check.verified.length})</summary>
          <ul className="sk-qc-list">
            {fixed.map((x, i) => <li key={`f${i}`} className="sk-qc-fixed"><b>{t("qc.fixed")}</b> {x}</li>)}
            {check.verified.map((x, i) => <li key={`v${i}`}>{x}</li>)}
          </ul>
        </details>
      )}
      {check.to_check.length > 0 && (
        <>
          <div className="sk-qc-sub">{t("qc.to_check")}</div>
          <ul className="sk-qc-list">{check.to_check.map((x, i) => <li key={`c${i}`}>{x}</li>)}</ul>
        </>
      )}
    </div>
  );
}
