"use client";
import { levelLabel, levelRank, useClassification } from "@/lib/classification";

const TONES = [
  "border-emerald-400/40 text-emerald-300",   // public
  "border-sky-400/40 text-sky-300",           // team
  "border-line text-slate-300",               // project (the default)
  "border-amber-400/50 text-amber-300",       // confidential
  "border-red-400/60 text-red-300",           // restricted
];

/** 3.4 — the classification of a note / card / deliverable. Hidden while the feature is off;
 *  `quiet` hides the default level (project) so only what stands out is marked. */
export default function LevelBadge({ level, quiet = false }: { level?: number | string | null; quiet?: boolean }) {
  const info = useClassification();
  if (!info?.enabled || level === undefined || level === null) return null;
  const r = levelRank(info, level);
  if (quiet && r === 2) return null;
  return (
    <span title={`classification: ${levelLabel(info, level)}`}
      className={`rounded border px-1 text-[10px] leading-4 ${TONES[r] ?? TONES[2]}`}>
      {levelLabel(info, level)}
    </span>
  );
}

/** Selector of a level at creation (default: project). */
export function LevelSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const info = useClassification();
  if (!info?.enabled) return null;
  return (
    <select aria-label="Classification" value={value} onChange={(e) => onChange(e.target.value)}
      className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
      {info.scale.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
    </select>
  );
}
