"use client";
import { useState } from "react";
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

/** 3.4 — the classification of an existing note or card, and (when allowed) its change:
 *  raising is immediate; lowering asks for a reason inline (journaled, and refused by the
 *  server unless a maintainer cleared for the current level asks). Badge = colour + label. */
export function LevelControl({ level, canWrite = true, onSet }: {
  level?: number | string | null; canWrite?: boolean;
  onSet: (level: string, reason: string) => Promise<unknown>;
}) {
  const info = useClassification();
  const [cur, setCur] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  if (!info?.enabled) return null;
  const now = cur ?? info.scale.find((l) => l.rank === levelRank(info, level))?.id ?? info.default;
  const rank = (id: string) => info.scale.find((l) => l.id === id)?.rank ?? 2;
  const apply = (v: string, why: string) => {
    setBusy(true);
    onSet(v, why).then(() => { setCur(v); setPending(null); setReason(""); setErr(""); })
      .catch((e) => setErr(String((e as Error).message || e))).finally(() => setBusy(false));
  };
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <LevelBadge level={now} />
      {canWrite && (
        <select aria-label="Reclassify" value={pending ?? now} disabled={busy}
          onChange={(e) => { const v = e.target.value; if (rank(v) < rank(now)) setPending(v); else { setPending(null); if (v !== now) apply(v, ""); } }}
          className="rounded border border-line bg-panel2 px-1 py-0.5 text-[11px] text-slate-300">
          {info.scale.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
        </select>
      )}
      {pending && (
        <>
          <input aria-label="Reason for lowering the level" autoFocus placeholder="why lower it? (journaled)" value={reason}
            onChange={(e) => setReason(e.target.value)} className="w-52 rounded border border-amber-400/50 bg-panel2 px-1.5 py-0.5 text-[11px] text-slate-200" />
          <button disabled={!reason.trim() || busy} onClick={() => apply(pending, reason.trim())}
            className="rounded bg-amber-500/80 px-1.5 py-0.5 text-[11px] font-semibold text-ink disabled:opacity-40">lower</button>
          <button onClick={() => { setPending(null); setReason(""); }} className="text-[11px] text-mut hover:text-slate-200">cancel</button>
        </>
      )}
      {err && <span role="alert" className="text-[11px] text-red-300">{err}</span>}
    </span>
  );
}
