"use client";
// Simple profile — the model that wrote an answer or a deliverable (3.6.5): the model of the
// API's response, as recorded by the session (backend/plain.py `engine`) — never the one asked for.
import type { Engine } from "@/lib/simple/api";
import { useT } from "@/lib/simple/i18n";

/** 3.6.5 — who wrote this answer: the model of the API's response (backend/plain.py `engine`) */
export function engineText(t: ReturnType<typeof useT>, e: Engine): string {
  if (e.where === "local") return t("eng.local");
  return e.place ? t("eng.badge", { label: e.label, place: e.place }) : e.label;
}

export default function EngineBadge({ engine }: { engine: Engine }) {
  const t = useT();
  return (
    <span className={`sk-eng-badge sk-eng-${engine.where}`} title={t("eng.title", { model: engine.model })}
      data-model={engine.model}>
      <span className="sk-eng-dot" aria-hidden />{engineText(t, engine)}
    </span>
  );
}
