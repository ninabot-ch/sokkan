"use client";
// 3.5 — Operate › Alerts: small shared pieces. Colour is never the only signal: every state and
// severity carries an icon and a word (same rule as the Crew deck and the ui-chip family).
import { useEffect, useId, useRef, useState } from "react";
import {
  durS, sDur, STATE_WORD, type AlertState, type RuleState, type RuleType, type Severity, type TV,
} from "@/lib/alertingModel";

/** Group series colours — validated for the dark surface (dataviz validator, fixed order, ≤ 4). */
export const SERIES = ["#3987e5", "#199e70", "#9085e9", "#d55181"];
export const FIRE = "#d03b3b";
export const THRESHOLD = "#D4A017";

const SEV_STYLE: Record<Severity, { cls: string; icon: string; label: string }> = {
  critical: { cls: "border-red-500/50 bg-red-500/10 text-red-300", icon: "◆", label: "Critical" },
  warning: { cls: "border-amber-500/50 bg-amber-500/10 text-amber-200", icon: "▲", label: "Warning" },
  info: { cls: "border-sky-500/40 bg-sky-500/10 text-sky-200", icon: "●", label: "Info" },
};

export function SeverityChip({ s, compact = false }: { s: Severity; compact?: boolean }) {
  const v = SEV_STYLE[s] ?? SEV_STYLE.info;
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-1.5 text-[10.5px] leading-4 ${v.cls}`}
      title={`severity: ${v.label}`}>
      <span aria-hidden>{v.icon}</span>{compact ? <span className="sr-only">{v.label}</span> : v.label}
    </span>
  );
}

const STATE_CLS: Record<RuleState | AlertState, string> = {
  firing: "border-red-500/60 bg-red-500/15 text-red-200",
  pending: "border-amber-500/50 bg-amber-500/10 text-amber-200",
  ok: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  resolved: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  silenced: "border-slate-500/50 bg-slate-500/10 text-slate-300",
  error: "border-orange-400/60 bg-orange-400/10 text-orange-200",
  disabled: "border-line bg-panel2 text-mut",
};

export function StateChip({ s, n }: { s: RuleState | AlertState; n?: number }) {
  const w = STATE_WORD[s] ?? STATE_WORD.ok;
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-1.5 text-[10.5px] leading-4 ${STATE_CLS[s]}`}>
      <span aria-hidden className={s === "firing" ? "motion-safe:animate-pulse" : ""}>{w.icon}</span>
      {w.label}{n !== undefined && n > 1 ? ` ×${n}` : ""}
    </span>
  );
}

/** A 24 h sparkline in a rule row: shape only, no axis (the rule page has the real chart). */
export function Sparkline({ pts, state, w = 96, h = 22 }: { pts?: TV[]; state: RuleState; w?: number; h?: number }) {
  const v = (pts || []).filter((p) => p[1] !== null && Number.isFinite(p[1] as number)) as [number, number][];
  if (v.length < 2) return <span className="inline-block text-[10px] text-mut" style={{ width: w }} aria-hidden>no data yet</span>;
  const t0 = v[0][0]; const t1 = v[v.length - 1][0];
  let lo = Math.min(...v.map((p) => p[1])); let hi = Math.max(...v.map((p) => p[1]));
  if (lo >= 0) lo = 0;
  if (hi === lo) hi = lo + 1;
  const x = (t: number) => (t1 === t0 ? w / 2 : ((t - t0) / (t1 - t0)) * (w - 2) + 1);
  const y = (n: number) => h - 2 - ((n - lo) / (hi - lo)) * (h - 4);
  const d = v.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const c = state === "firing" ? FIRE : state === "error" ? "#fb923c" : SERIES[0];
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden className="shrink-0">
      <path d={d} fill="none" stroke={c} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(v[v.length - 1][0])} cy={y(v[v.length - 1][1])} r={2} fill={c} />
    </svg>
  );
}

/** Little drawings of each rule type for the type cards (illustration of the condition). */
export function TypeArt({ t, active }: { t: RuleType; active: boolean }) {
  const c = active ? "#60a5fa" : "#8aa0b6";
  const f = FIRE;
  const th = THRESHOLD;
  const P = (d: string, col = c, dash?: string) => <path d={d} fill="none" stroke={col} strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" strokeDasharray={dash} />;
  const bars = (hs: number[], hot: number[] = []) => hs.map((hh, i) =>
    <rect key={i} x={4 + i * 7} y={26 - hh} width={4.5} height={hh} rx={1.2} fill={hot.includes(i) ? f : c} opacity={hot.includes(i) ? 1 : 0.75} />);
  let body: React.ReactNode;
  switch (t) {
    case "threshold": body = <>{P("M2,22 L12,18 L20,20 L28,8 L36,6 L44,16 L58,19")}{P("M2,12 L58,12", th, "3 2")}<rect x={25} y={4} width={14} height={8} fill={f} opacity={0.18} /></>; break;
    case "frequency": body = <>{bars([4, 6, 5, 7, 18, 20, 6, 5], [4, 5])}{P("M2,10 L58,10", th, "3 2")}</>; break;
    case "any": body = <>{[8, 20, 31, 47].map((x, i) => <circle key={i} cx={x} cy={16} r={3} fill={i === 2 ? f : c} />)}{P("M2,16 L58,16", "#26303d")}</>; break;
    case "spike": body = <>{P("M2,22 L14,21 L24,22 L30,20 L34,5 L38,20 L58,21")}{P("M2,18 L58,18", "#8aa0b6", "2 2")}<circle cx={34} cy={5} r={2.5} fill={f} /></>; break;
    case "flatline": body = <>{bars([12, 10, 14, 11, 0, 0, 0, 0])}<rect x={31} y={4} width={27} height={22} fill={f} opacity={0.15} rx={2} /></>; break;
    case "absence": body = <>{P("M2,14 L10,12 L18,15 L26,13")}{P("M26,13 L58,13", "#8aa0b6", "1 3")}<text x={42} y={10} fontSize={9} fill={f} textAnchor="middle">?</text></>; break;
    case "new_term": body = <>{["a", "b", "a", "c"].map((s, i) => <text key={i} x={8 + i * 13} y={19} fontSize={10} fill={i === 3 ? f : c} fontFamily="monospace">{s}</text>)}<circle cx={47} cy={9} r={2.5} fill={f} /></>; break;
    case "change": body = <>{P("M2,20 L28,20 L28,9 L58,9")}<circle cx={28} cy={14.5} r={3} fill={f} /></>; break;
    case "cardinality": body = <>{[0, 1, 2, 3, 4, 5, 6].map((i) => <circle key={i} cx={8 + (i % 4) * 13} cy={9 + Math.floor(i / 4) * 10} r={3} fill={i > 4 ? f : c} opacity={0.9} />)}</>; break;
    case "anomaly": body = <><path d="M2,10 C16,8 30,12 58,10 L58,20 C30,22 16,18 2,20 Z" fill="#8aa0b6" opacity={0.18} />{P("M2,15 L20,14 L30,16 L38,4 L46,15 L58,15")}<circle cx={38} cy={4} r={2.5} fill={f} /></>; break;
  }
  return <svg width={60} height={28} viewBox="0 0 60 28" aria-hidden className="shrink-0">{body}</svg>;
}

/** Segmented control (radio group), keyboard: arrows move, Space/Enter select. */
export function Seg<T extends string>({ value, onChange, opts, label, size = "md" }: {
  value: T; onChange: (v: T) => void; opts: { id: T; label: React.ReactNode; title?: string }[]; label: string; size?: "sm" | "md";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const move = (dir: number) => {
    const i = opts.findIndex((o) => o.id === value);
    const n = opts[(i + dir + opts.length) % opts.length];
    onChange(n.id);
    requestAnimationFrame(() => ref.current?.querySelector<HTMLButtonElement>(`[data-id="${n.id}"]`)?.focus());
  };
  return (
    <div ref={ref} role="radiogroup" aria-label={label} className="inline-flex rounded-lg border border-line bg-panel2/60 p-0.5">
      {opts.map((o) => {
        const on = o.id === value;
        return (
          <button key={o.id} type="button" role="radio" aria-checked={on} data-id={o.id} title={o.title}
            tabIndex={on ? 0 : -1} onClick={() => onChange(o.id)}
            onKeyDown={(e) => { if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
              if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); move(-1); } }}
            className={`ui-focus rounded-md ${size === "sm" ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-[12px]"} ${on ? "bg-sea/20 text-slate-100 ring-1 ring-sea/50" : "text-mut hover:text-slate-200"}`}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function Field({ label, hint, children, htmlFor, className = "" }: {
  label: React.ReactNode; hint?: React.ReactNode; children: React.ReactNode; htmlFor?: string; className?: string;
}) {
  return (
    <div className={`flex min-w-0 flex-col gap-1 ${className}`}>
      <label htmlFor={htmlFor} className="text-[11.5px] font-medium text-slate-300">{label}</label>
      {children}
      {hint && <span className="text-[10.5px] leading-snug text-mut">{hint}</span>}
    </div>
  );
}

export const inputCls = "ui-focus w-full min-w-0 rounded-lg border border-line bg-panel2 px-2.5 py-1.5 text-[12.5px] text-slate-100 placeholder:text-slate-500 outline-none focus:border-sea/60";
export const btn = {
  primary: "ui-focus inline-flex items-center gap-1.5 rounded-lg bg-sea px-3 py-1.5 text-[12.5px] font-medium text-white hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40",
  ghost: "ui-focus inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-slate-200 hover:border-sea/50 disabled:cursor-not-allowed disabled:opacity-40",
  small: "ui-focus inline-flex items-center gap-1 rounded-md border border-line px-2 py-0.5 text-[11px] text-slate-200 hover:border-sea/50 disabled:cursor-not-allowed disabled:opacity-40",
  danger: "ui-focus inline-flex items-center gap-1 rounded-md border border-red-500/40 px-2 py-0.5 text-[11px] text-red-200 hover:border-red-400 disabled:opacity-40",
};

const UNITS = [{ id: "s", label: "sec", k: 1 }, { id: "m", label: "min", k: 60 }, { id: "h", label: "hours", k: 3600 }, { id: "d", label: "days", k: 86400 }];

/** A duration as « number + unit » (the API wants "5m"); never makes the person type "5m". */
export function DurInput({ value, onChange, id, min = 0, label }: {
  value: string; onChange: (v: string) => void; id?: string; min?: number; label: string;
}) {
  const s = durS(value);
  const unit = [...UNITS].reverse().find((u) => s > 0 && s % u.k === 0) ?? UNITS[1];
  const [n, setN] = useState(String(s ? s / unit.k : 0));
  const [u, setU] = useState(unit.id);
  useEffect(() => {
    const ss = durS(value); const uu = [...UNITS].reverse().find((x) => ss > 0 && ss % x.k === 0) ?? UNITS.find((x) => x.id === u) ?? UNITS[1];
    setN(String(ss ? ss / uu.k : 0)); setU(uu.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  const push = (nn: string, uu: string) => {
    const k = UNITS.find((x) => x.id === uu)?.k ?? 60;
    const v = Math.max(min, Math.round((parseFloat(nn) || 0) * k));
    onChange(sDur(v));
  };
  return (
    <div className="flex min-w-0 gap-1">
      <input id={id} aria-label={`${label} (number)`} inputMode="decimal" value={n}
        onChange={(e) => setN(e.target.value)} onBlur={() => push(n, u)}
        onKeyDown={(e) => e.key === "Enter" && push(n, u)}
        className={`${inputCls} w-16 text-right tabular-nums`} />
      <select aria-label={`${label} (unit)`} value={u} onChange={(e) => { setU(e.target.value); push(n, e.target.value); }}
        className={`${inputCls} w-auto pr-6`}>
        {UNITS.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
      </select>
    </div>
  );
}

/** Text input with suggestions from the source (metrics, labels, values, fields) — a real combobox:
 *  arrows move, Enter picks, Escape closes; the list is fetched as the person types (debounced). */
export function Combo({ value, onChange, fetcher, placeholder, label, mono = true, id, onPick }: {
  value: string; onChange: (v: string) => void; fetcher?: (q: string) => Promise<string[]>;
  placeholder?: string; label: string; mono?: boolean; id?: string;
  /** called when a value is chosen (suggestion clicked / Enter) — for « add » inputs */
  onPick?: (v: string) => void;
}) {
  const uid = useId();
  const listId = `${uid}-list`;
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<string[]>([]);
  const [hi, setHi] = useState(0);
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);
  useEffect(() => {
    if (!open || !fetcher) return;
    const my = ++seq.current;
    setBusy(true);
    const t = setTimeout(() => {
      fetcher(value).then((xs) => { if (my === seq.current) { setItems(xs.slice(0, 60)); setHi(0); } })
        .catch(() => { if (my === seq.current) setItems([]); })
        .finally(() => { if (my === seq.current) setBusy(false); });
    }, 180);
    return () => clearTimeout(t);
  }, [value, open, fetcher]);
  const pick = (v: string) => { onChange(v); onPick?.(v); setOpen(false); };
  return (
    <div className="relative min-w-0">
      <input id={id} role="combobox" aria-label={label} aria-expanded={open && items.length > 0} aria-controls={listId}
        aria-autocomplete="list" aria-activedescendant={open && items[hi] ? `${uid}-${hi}` : undefined}
        value={value} placeholder={placeholder} spellCheck={false} autoComplete="off"
        onChange={(e) => { onChange(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && onPick && (!open || !items.length) && value.trim()) { e.preventDefault(); pick(value.trim()); return; }
          if (!open || !items.length) return;
          if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(items.length - 1, h + 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(0, h - 1)); }
          else if (e.key === "Enter") { e.preventDefault(); pick(items[hi]); }
          else if (e.key === "Escape") setOpen(false);
        }}
        className={`${inputCls} ${mono ? "font-mono text-[12px]" : ""}`} />
      {open && (items.length > 0 || busy) && (
        <ul id={listId} role="listbox" aria-label={`${label} suggestions`}
          className="absolute left-0 right-0 top-full z-30 mt-1 max-h-56 overflow-y-auto rounded-lg border border-line bg-panel p-1 shadow-xl shadow-black/40">
          {busy && !items.length && <li className="px-2 py-1 text-[11px] text-mut">Looking…</li>}
          {items.map((it, i) => (
            <li key={it} id={`${uid}-${i}`} role="option" aria-selected={i === hi}
              onMouseDown={(e) => { e.preventDefault(); pick(it); }} onMouseEnter={() => setHi(i)}
              className={`cursor-pointer truncate rounded px-2 py-1 font-mono text-[11.5px] ${i === hi ? "bg-sea/20 text-slate-100" : "text-slate-300"}`}>
              {it}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Toggle switch with a visible label (not a bare checkbox). */
export function Toggle({ on, onChange, label, hint, disabled }: { on: boolean; onChange: (v: boolean) => void; label: React.ReactNode; hint?: React.ReactNode; disabled?: boolean }) {
  return (
    <label className={`flex items-start gap-2.5 ${disabled ? "opacity-50" : "cursor-pointer"}`}>
      <button type="button" role="switch" aria-checked={on} disabled={disabled} onClick={() => onChange(!on)}
        className={`ui-focus relative mt-0.5 h-4.5 w-8 shrink-0 rounded-full border transition-colors ${on ? "border-sea bg-sea/70" : "border-line bg-panel2"}`}
        style={{ height: 18 }}>
        <span className={`absolute top-[2px] h-3 w-3 rounded-full bg-white transition-all ${on ? "left-[15px]" : "left-[2px]"}`} />
      </button>
      <span className="min-w-0">
        <span className="block text-[12.5px] text-slate-200">{label}</span>
        {hint && <span className="block text-[10.5px] leading-snug text-mut">{hint}</span>}
      </span>
    </label>
  );
}

export function Banner({ tone, children }: { tone: "error" | "info" | "warn" | "ok"; children: React.ReactNode }) {
  const c = tone === "error" ? "border-red-500/40 bg-red-500/10 text-red-100" : tone === "warn" ? "border-amber-500/40 bg-amber-500/10 text-amber-100"
    : tone === "ok" ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-100" : "border-line bg-panel2/50 text-slate-300";
  return <div role={tone === "error" ? "alert" : "status"} className={`rounded-lg border px-2.5 py-2 text-[12px] leading-snug ${c}`}>{children}</div>;
}

export const fmtTime = (t: number, withDay = false) => {
  const d = new Date(t * 1000);
  const hm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return withDay ? `${d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} ${hm}` : hm;
};
