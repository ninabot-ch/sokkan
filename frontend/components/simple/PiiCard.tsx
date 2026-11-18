"use client";
// SOKKAN 4.0 — « PII sandwich » (backend/pii.py, backend/claudepii.py): before a request goes to
// Claude, the personal data found in it (and in its attachments) is said — categories, counts and
// a masked hint, never a value — and the person chooses. Nothing has left the machine yet.
import { useEffect, useRef } from "react";
import type { PiiFound } from "@/lib/simple/api";
import { useT } from "@/lib/simple/i18n";
import Icon from "./icons";

export type PiiChoice = "anonymize" | "local" | "cancel";

export default function PiiCard({ found, busy, onChoose }: {
  found: PiiFound; busy: boolean; onChoose: (c: PiiChoice) => void;
}) {
  const t = useT();
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => { first.current?.focus(); }, [found]);
  const other: PiiChoice = found.choices.includes("local") ? "local" : "cancel";
  return (
    <section className="sk-card sk-pii" role="region" aria-labelledby="sk-pii-title" aria-describedby="sk-pii-lead">
      <div className="sk-row sk-pii-head"><Icon name="shield" />
        <h2 className="sk-card-title" id="sk-pii-title">{t("pii.title")}</h2></div>
      <p className="sk-small" id="sk-pii-lead">{t(found.categories.length ? "pii.lead" : "pii.people")}</p>
      {found.categories.length > 0 && <ul className="sk-pii-list">
        {found.categories.map((c) => (
          <li key={c.category}><b>{c.count}</b> {c.label}
            {c.examples.length > 0 && <span className="sk-xs sk-muted"> · {c.examples.join(", ")}</span>}</li>
        ))}
      </ul>}
      <p className="sk-small">{t("pii.how")}</p>
      <p className="sk-xs sk-muted">{t("pii.limit")}</p>
      <div className="sk-row sk-pii-actions">
        <button ref={first} className="sk-btn sk-btn-primary" disabled={busy} onClick={() => onChoose("anonymize")}>
          <Icon name="shield" small />{t("pii.anonymize")}</button>
        <button className="sk-btn" disabled={busy} onClick={() => onChoose(other)}>
          {t(other === "local" ? "pii.local" : "pii.cancel")}</button>
      </div>
    </section>
  );
}

/** the counts the card showed, for the journal of a « do not send » */
export const piiCounts = (f: PiiFound): Record<string, number> =>
  Object.fromEntries(f.categories.map((c) => [c.category, c.count]));
