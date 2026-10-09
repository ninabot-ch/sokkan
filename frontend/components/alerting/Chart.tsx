"use client";
// 3.5 — the backtest chart of Operate › Alerts: the data of the last hour…7 days, the threshold as a
// line you can DRAG (mouse, touch or keyboard), and red bands where the rule would have fired —
// « this rule would have fired 3 times yesterday ». While the line moves, the bands are computed in
// the browser (instant); the server's backtest (it knows « for », cooldown, groups) takes over as
// soon as it answers. Hand-made SVG: one axis, recessive grid, 2 px lines, hover crosshair +
// tooltip, a table view for screen readers. Up to 12 groups: the ones that fired (or the one the
// person focuses in the legend) take the validated palette, the others stay quiet grey — colour is
// spent on what matters, never on « group #9 ».
import { useEffect, useMemo, useRef, useState } from "react";
import {
  areaPath, fmtValue, linePath, linear, localFires, mergeIntervals, firedFirst, groupTip, groupTitle, fmtDur, nearest, niceTicks, timeTicks, yDomain,
  type Interval, type PreviewResult, type Series, type TV, type Transition,
} from "@/lib/alertingModel";
import { FIRE, SERIES, THRESHOLD, fmtTime } from "./bits";

const M = { top: 14, right: 14, bottom: 22, left: 44 };
const MAX_GROUPS = 12;
const QUIET = "#5b6b80";
const UNIT_WORD: Record<string, string> = { "%": "%", "/s": "per second", s: "seconds", bytes: "bytes", days: "days", events: "events per step" };

export interface ChartProps {
  result: PreviewResult | null;
  loading?: boolean;
  /** editable threshold: the value and a setter (called when the person lets go) */
  threshold?: { op: string; value: number } | null;
  onThreshold?: (v: number) => void;
  /** seconds the condition must hold (« for ») — used by the instant re-computation */
  forS?: number;
  unit?: string | null;
  height?: number;
  /** real history of the rule (rule page): markers on top of the backtest */
  transitions?: Transition[];
  /** text shown in the frame before any data */
  idle?: React.ReactNode;
  /** the range the person asked, for the « would have fired » sentence */
  rangeLabel?: string;
}

export default function Chart({ result, loading, threshold, onThreshold, forS = 0, unit, height = 230,
  transitions, idle, rangeLabel }: ChartProps) {
  const box = useRef<HTMLDivElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const [w, setW] = useState(640);
  useEffect(() => {
    const el = box.current; if (!el) return;
    const ro = new ResizeObserver((e) => setW(Math.max(280, Math.floor(e[0].contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const all: Series[] = firedFirst(result?.series || [], result?.fired_intervals || []);
  const drawn = all.slice(0, MAX_GROUPS);
  const hiddenGroups = Math.max(0, all.length - drawn.length);
  // the legend focuses one group (click again: all)
  const [focus, setFocus] = useState<string | null>(null);
  const [allLegend, setAllLegend] = useState(false);
  useEffect(() => { if (focus && !drawn.some((s) => (s.group_key || "") === focus)) setFocus(null); }, [drawn, focus]);
  const ref = result?.reference && "points" in result.reference ? (result.reference as Series) : null;
  const band = result?.reference && "lower" in result.reference ? (result.reference as { lower: TV[]; upper: TV[] }) : null;

  // the threshold the person is dragging (local), else the one asked
  const [drag, setDrag] = useState<number | null>(null);
  useEffect(() => { setDrag(null); }, [threshold?.value]);
  const thVal = drag ?? threshold?.value ?? result?.threshold?.value ?? null;
  const thOp = threshold?.op ?? result?.threshold?.op ?? ">";

  const pts = drawn.flatMap((s) => s.points);
  const [t0, t1] = useMemo(() => {
    const ts = [...pts, ...(ref?.points || [])].map((p) => p[0]);
    if (!ts.length) { const now = Date.now() / 1000; return [now - 86400, now]; }
    return [Math.min(...ts), Math.max(...ts)];
  }, [pts, ref]);
  const ser = [...drawn.map((s) => s.points), ...(ref ? [ref.points] : []), ...(band ? [band.lower, band.upper] : [])];
  // a target rule (Prometheus `up`: 0 or 1) reads as « up / down », not as 0 … 1.5
  const binary = !unit && pts.length > 0 && pts.every((p) => p[1] === 0 || p[1] === 1) && (thVal === null || thVal === 1 || thVal === 0);
  const dom: [number, number] = binary ? [0, 1.15] : yDomain(ser, thVal, result?.threshold?.value);
  const iw = w - M.left - M.right; const ih = height - M.top - M.bottom;
  const x = linear([t0, t1], [M.left, M.left + iw]);
  const y = linear(dom, [M.top + ih, M.top]);
  const yt = binary ? [0, 1] : niceTicks(dom[0], dom[1], 4).filter((v) => v >= dom[0] - 1e-9 && v <= dom[1] + 1e-9);
  const showLine = thVal !== null && !binary;
  const xt = timeTicks(t0, t1);
  const multiDay = t1 - t0 > 30 * 3600;

  // fires: instant while dragging / when the drawn threshold differs from the server's; else the server's
  const local = drag !== null || (threshold && result?.threshold && threshold.value !== result.threshold.value);
  const fires: Interval[] = local && thVal !== null && result?.kind !== "events" ? localFires(drawn, thOp, thVal, forS) : (result?.fired_intervals || []);
  const bands = mergeIntervals(fires, t1);
  const nFires = local ? fires.length : (result?.fires ?? fires.length);
  // colour: few groups → each its colour; many → the groups that fired (≤ 4) in colour, the rest grey
  const hot = new Set(fires.map((f) => f.group_key ?? ""));
  const colour: string[] = (() => {
    if (drawn.length <= SERIES.length) return drawn.map((_, i) => SERIES[i]);
    // nothing fired: the groups closest to the line take the colour (the ones worth reading)
    const keys = hot.size ? hot : new Set(drawn.map((s) => ({ k: s.group_key ?? "", v: worst(s.points) }))
      .sort((a, b) => b.v - a.v).slice(0, SERIES.length).map((o) => o.k));
    let k = 0;
    return drawn.map((s) => (keys.has(s.group_key ?? "") && k < SERIES.length ? SERIES[k++] : QUIET));
  })();
  // legend: every group when ≤ 6, else the ones that fired + the first others up to 5 (+N more)
  const nHot = drawn.filter((d) => hot.has(d.group_key ?? "")).length;
  const legend = drawn.map((s, i) => ({ s, i })).filter(({ s, i }) => allLegend || drawn.length <= 6
    || hot.has(s.group_key ?? "") || focus === (s.group_key || "") || i < Math.max(0, 5 - nHot));
  // « toward the line »: the highest values for « above », the lowest for « below »
  function worst(ps: TV[]): number {
    const v = ps.map((p) => p[1]).filter((x): x is number => x !== null && Number.isFinite(x));
    if (!v.length) return -Infinity;
    return thOp.startsWith("<") ? -Math.min(...v) : Math.max(...v);
  }
  const dim = (s: Series, i: number) => focus !== null ? (s.group_key || "") !== focus : colour[i] === QUIET;
  const isCount = result?.kind === "count" || result?.kind === "events";
  const bars = isCount && drawn.length === 1;
  const step = result?.step_s || (pts.length > 1 ? (t1 - t0) / Math.max(1, drawn[0]?.points.length - 1 || 1) : 60);

  // hover
  const [hov, setHov] = useState<number | null>(null);
  const onMove = (e: React.PointerEvent) => {
    if (dragging.current) return;
    const r = svg.current?.getBoundingClientRect(); if (!r) return;
    const px = e.clientX - r.left;
    if (px < M.left || px > M.left + iw) { setHov(null); return; }
    const p = drawn[0] ? nearest(drawn[0].points, x.invert(px)) : null;
    setHov(p ? p[0] : null);
  };

  // drag the threshold
  const dragging = useRef(false);
  const editable = !!(threshold && onThreshold);
  const stepV = (() => { const span = dom[1] - dom[0]; const raw = span / 200; const mag = Math.pow(10, Math.floor(Math.log10(raw || 1))); return Math.max(mag, raw - (raw % mag)); })();
  const snap = (v: number) => Number((Math.round(v / stepV) * stepV).toPrecision(6));
  const fromEvent = (e: React.PointerEvent) => {
    const r = svg.current?.getBoundingClientRect(); if (!r) return null;
    return snap(Math.min(dom[1], Math.max(dom[0], y.invert(e.clientY - r.top))));
  };
  const down = (e: React.PointerEvent) => {
    if (!editable) return;
    e.preventDefault();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    dragging.current = true; setHov(null);
    const v = fromEvent(e); if (v !== null) setDrag(v);
  };
  const move = (e: React.PointerEvent) => { if (!dragging.current) return; const v = fromEvent(e); if (v !== null) setDrag(v); };
  const up = () => { if (!dragging.current) return; dragging.current = false; if (drag !== null) onThreshold?.(drag); };
  const key = (e: React.KeyboardEvent) => {
    if (!editable || thVal === null) return;
    const k = e.key === "ArrowUp" ? 1 : e.key === "ArrowDown" ? -1 : e.key === "PageUp" ? 10 : e.key === "PageDown" ? -10 : 0;
    if (!k) return;
    e.preventDefault();
    const v = snap(thVal + k * stepV * 2);
    setDrag(v); onThreshold?.(v);
  };

  const [table, setTable] = useState(false);
  const has = pts.length > 0;
  const hovVals = hov !== null ? drawn.map((s) => nearest(s.points, hov)?.[1] ?? null) : [];
  const tipLeft = hov !== null ? Math.min(Math.max(x(hov) + 10, M.left), w - 210) : 0;

  const summary = !has ? "No data in this range." :
    `${drawn.length > 1 ? `${all.length} groups` : "One series"}, ${fmtTime(t0, true)} to ${fmtTime(t1, true)}, `
    + `values from ${fmtValue(Math.min(...pts.map((p) => p[1] ?? Infinity)), unit)} to ${fmtValue(Math.max(...pts.map((p) => p[1] ?? -Infinity)), unit)}`
    + (thVal !== null ? `, threshold ${thOp} ${fmtValue(thVal, unit)}` : "")
    + `. Would have fired ${nFires} time${nFires === 1 ? "" : "s"}.`;

  return (
    <div className="min-w-0">
      <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
        <FireCount n={nFires} loading={!!loading} has={has} err={result?.error} range={rangeLabel} local={!!local}
          crossed={nFires === 0 && forS > 0 && thVal !== null && result?.kind === "metric" ? localFires(drawn, thOp, thVal, 0).length : 0}
          forS={forS} />
        {drawn.length > 1 && (
          <ul className="flex flex-wrap items-center gap-1 text-[11px] text-slate-300" aria-label="groups — click one to focus it">
            {legend.map(({ s, i }) => {
              const k = s.group_key || ""; const on = focus === k; const fired = hot.has(k);
              return (
                <li key={k || i}>
                  <button type="button" aria-pressed={on} onClick={() => setFocus(on ? null : k)} title={`${groupTip(s)}${fired ? " — fired" : ""} · click to ${on ? "show all groups" : "focus this group"}`}
                    className={`ui-focus flex min-h-6 items-center gap-1 rounded-md px-1.5 ${on ? "bg-sea/15 ring-1 ring-sea/50" : "hover:bg-panel2"} ${focus !== null && !on ? "opacity-50" : ""}`}>
                    <span aria-hidden className="inline-block h-[3px] w-3 rounded" style={{ background: colour[i] }} />
                    <span className="max-w-[30ch] truncate">{groupTitle(s)}</span>
                    {fired && <span aria-hidden className="text-[9px] text-red-300">▲</span>}
                  </button>
                </li>
              );
            })}
            {drawn.length > 6 && (allLegend || legend.length < drawn.length) && (
              <li><button type="button" className="ui-focus min-h-6 rounded-md px-1.5 text-mut hover:text-slate-200" aria-expanded={allLegend} onClick={() => setAllLegend((v) => !v)}>
                {allLegend ? "fewer" : `+${drawn.length - legend.length} more`}</button></li>
            )}
            {hiddenGroups > 0 && allLegend && <li className="px-1 text-mut" title="the largest groups are drawn — add a filter or « by » to see the others">+{hiddenGroups} smaller not drawn</li>}
          </ul>
        )}
        {ref && <span className="flex items-center gap-1 text-[11px] text-mut"><span aria-hidden className="inline-block w-3 border-t border-dashed border-slate-400" />usual level</span>}
        {showLine && <span className="flex items-center gap-1 text-[11px] text-mut"><span aria-hidden className="inline-block w-3 border-t-2 border-dashed" style={{ borderColor: THRESHOLD }} />threshold{editable ? " — drag it" : ""}</span>}
        {unit && !binary && UNIT_WORD[unit] && <span className="text-[11px] text-mut">values in {UNIT_WORD[unit]}</span>}
        <button type="button" onClick={() => setTable((v) => !v)} className="ui-focus ml-auto min-h-6 rounded px-1.5 text-[10.5px] text-mut underline-offset-2 hover:text-slate-200 hover:underline" aria-pressed={table}>
          {table ? "chart" : "table"}
        </button>
      </div>

      <div ref={box} className="relative w-full select-none rounded-lg border border-line bg-panel/70" style={{ height }}>
        {table ? (
          <DataTable drawn={drawn} fires={fires} unit={unit} height={height} />
        ) : (
          <svg ref={svg} width={w} height={height} role={editable ? "group" : "img"} aria-label={summary} className="block touch-none"
            onPointerMove={(e) => { onMove(e); move(e); }} onPointerLeave={() => setHov(null)} onPointerUp={up} onPointerCancel={up}>
            {/* grid + y axis (recessive) */}
            {yt.map((v) => (
              <g key={`y${v}`}>
                <line x1={M.left} x2={M.left + iw} y1={y(v)} y2={y(v)} stroke="#26303d" strokeWidth={1} />
                <text x={M.left - 6} y={y(v) + 3.5} textAnchor="end" fontSize={10} fill="#8aa0b6" className="tabular-nums">{binary ? (v === 1 ? "up" : "down") : fmtValue(v, unit === "%" ? null : unit)}</text>
              </g>
            ))}
            {xt.map((t) => (
              <text key={`x${t}`} x={x(t)} y={height - 6} textAnchor="middle" fontSize={10} fill="#8aa0b6">
                {multiDay ? new Date(t * 1000).toLocaleDateString(undefined, { weekday: "short", day: "numeric" }) : fmtTime(t)}
              </text>
            ))}
            {/* fired bands */}
            {/* beyond the line: a light wash where the value would fire (not the whole chart) */}
            {showLine && thVal !== null && thVal >= dom[0] && thVal <= dom[1] && result?.kind !== "events" && (
              thOp.startsWith(">")
                ? <rect x={M.left} y={M.top} width={iw} height={Math.max(0, y(thVal) - M.top)} fill={FIRE} opacity={0.05} />
                : <rect x={M.left} y={y(thVal)} width={iw} height={Math.max(0, M.top + ih - y(thVal))} fill={FIRE} opacity={0.05} />
            )}
            {/* when it would have fired: a discreet band + a mark on top */}
            {bands.map((b, i) => {
              const x0 = x(b.start); const x1 = Math.max(x(b.end), x0 + 3);
              return (
                <g key={`f${i}`}>
                  {/* a band over most of the range says nothing more than the mark on top: no wash then */}
                  {x1 - x0 < iw * 0.5 && <rect x={x0} y={M.top} width={x1 - x0} height={ih} fill={FIRE} opacity={0.07} />}
                  <rect x={x0} y={M.top - 5} width={x1 - x0} height={3} fill={FIRE} opacity={0.85} rx={1.5} />
                </g>
              );
            })}
            {/* anomaly band / usual level */}
            {band && <path d={`${linePath(band.upper, x, y)}L${[...band.lower].reverse().map(([t, v]) => `${x(t).toFixed(1)},${y(v ?? 0).toFixed(1)}`).join("L")}Z`} fill="#8aa0b6" opacity={0.12} />}
            {ref && <path d={linePath(ref.points, x, y)} fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeDasharray="4 3" />}
            {/* the threshold line, under the series (a series sitting on it stays visible) */}
            {showLine && thVal !== null && thVal >= dom[0] && thVal <= dom[1] && (
              <line x1={M.left} x2={M.left + iw} y1={y(thVal)} y2={y(thVal)} stroke={THRESHOLD} strokeWidth={2} strokeDasharray="6 4" />
            )}
            {/* series */}
            {bars && drawn[0] ? drawn[0].points.map(([t, v], i) => {
              if (v === null) return null;
              const bw = Math.max(1, (iw * step) / Math.max(1, t1 - t0) - 2);
              const hot = fires.some((f) => t >= f.start && t <= (f.end ?? f.start));
              const top = y(Math.max(0, v)); const base = y(0);
              return <rect key={i} x={x(t) - bw / 2} y={Math.min(top, base - (v > 0 ? 1 : 0))} width={bw} height={Math.max(v > 0 ? 1 : 0, base - top)}
                rx={Math.min(2, bw / 3)} fill={hot ? FIRE : SERIES[0]} opacity={hot ? 0.95 : 0.8} />;
            }) : drawn.map((s, i) => ({ s, i })).sort((a, b) => Number(!dim(a.s, a.i)) - Number(!dim(b.s, b.i))).map(({ s, i }) => {
              const quiet = dim(s, i);
              const c = focus !== null && !quiet ? (colour[i] === QUIET ? SERIES[0] : colour[i]) : colour[i];
              return (
                <g key={s.group_key || i}>
                  {drawn.length === 1 && <path d={areaPath(s.points, x, y, Math.max(dom[0], 0))} fill={SERIES[0]} opacity={0.1} />}
                  <path d={linePath(s.points, x, y)} fill="none" stroke={c} strokeWidth={quiet ? 1.25 : 2}
                    opacity={quiet ? (focus !== null ? 0.18 : 0.55) : 1} strokeLinejoin="round" strokeLinecap="round" />
                </g>
              );
            })}
            {/* real history (rule page): firing markers */}
            {(transitions || []).filter((tr) => tr.to === "firing" && tr.ts >= t0 && tr.ts <= t1).map((tr, i) => (
              <g key={`tr${i}`}>
                <line x1={x(tr.ts)} x2={x(tr.ts)} y1={M.top} y2={M.top + ih} stroke={FIRE} strokeWidth={1} strokeDasharray="2 2" />
                <path d={`M${x(tr.ts) - 4},${M.top - 1} L${x(tr.ts) + 4},${M.top - 1} L${x(tr.ts)},${M.top + 6} Z`} fill={FIRE} />
              </g>
            ))}
            {/* threshold handle (drag / keyboard), on top */}
            {showLine && thVal !== null && thVal >= dom[0] && thVal <= dom[1] && (
              <g>
                {editable && (
                  <>
                    <rect x={M.left} width={iw} y={y(thVal) - 8} height={16} fill="transparent" style={{ cursor: "ns-resize" }} onPointerDown={down} />
                    <g role="slider" tabIndex={0} aria-label="threshold" aria-valuenow={thVal} aria-valuemin={dom[0]} aria-valuemax={dom[1]}
                      aria-valuetext={`${thOp} ${fmtValue(thVal, unit)}`} onKeyDown={key} onPointerDown={down}
                      className="outline-none [&:focus-visible>rect]:stroke-sky-400" style={{ cursor: "ns-resize" }}>
                      <rect x={M.left + iw - 74} y={y(thVal) - 12} width={72} height={24} rx={12} fill="#141a24" stroke={THRESHOLD} strokeWidth={1.5} />
                      <text x={M.left + iw - 38} y={y(thVal) + 3.5} textAnchor="middle" fontSize={10.5} fill="#f8e7b0" className="tabular-nums">
                        ⇕ {thOp} {fmtValue(thVal, unit)}
                      </text>
                    </g>
                  </>
                )}
              </g>
            )}
            {/* hover */}
            {hov !== null && (
              <g pointerEvents="none">
                <line x1={x(hov)} x2={x(hov)} y1={M.top} y2={M.top + ih} stroke="#cbd5e1" strokeWidth={1} opacity={0.5} />
                {drawn.map((s, i) => { const v = hovVals[i]; return v === null || dim(s, i) ? null : <circle key={i} cx={x(hov)} cy={y(v)} r={4} fill={colour[i] === QUIET ? SERIES[0] : colour[i]} stroke="#141a24" strokeWidth={2} />; })}
              </g>
            )}
          </svg>
        )}
        {!table && hov !== null && (
          <div className="pointer-events-none absolute z-10 min-w-[170px] rounded-lg border border-line bg-panel px-2 py-1.5 text-[11px] shadow-lg shadow-black/40"
            style={{ left: tipLeft, top: 8 }}>
            <div className="mb-0.5 text-mut">{fmtTime(hov, multiDay)}</div>
            {drawn.map((s, i) => ({ s, i, v: hovVals[i] })).filter((o) => focus === null || (o.s.group_key || "") === focus)
              .sort((a, b) => (thOp.startsWith("<") ? 1 : -1) * ((a.v ?? -Infinity) - (b.v ?? -Infinity))).slice(0, 8).map(({ s, i, v }) => (
              <div key={i} className="flex items-center gap-1.5">
                <span aria-hidden className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: colour[i] }} />
                {drawn.length > 1 && <span className="max-w-[16ch] truncate text-[10.5px] text-slate-300">{groupTitle(s)}</span>}
                <span className="ml-auto pl-2 font-medium tabular-nums text-slate-100">{fmtValue(v, unit)}</span>
              </div>
            ))}
            {fires.some((f) => hov >= f.start && hov <= (f.end ?? f.start)) && <div className="mt-0.5 text-red-300">▲ would be firing</div>}
          </div>
        )}
        {(!has || loading || result?.error) && !table && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-4 text-center">
            {result?.error ? (
              <div className="pointer-events-auto max-w-md rounded-lg border border-orange-400/40 bg-ink/90 px-3 py-2 text-[12px] text-orange-100">
                <b>The source answered with an error.</b><br /><span className="font-mono text-[11px] text-orange-200/90">{result.error}</span>
              </div>
            ) : loading && !has ? (
              <span className="text-[12px] text-mut">Reading the data…</span>
            ) : !has ? (
              <span className="max-w-sm text-[12px] text-mut">{idle ?? "No data for this query in this range."}</span>
            ) : null}
          </div>
        )}
        {loading && has && <div className="absolute right-2 top-2 h-2 w-2 rounded-full bg-sea motion-safe:animate-ping" aria-label="updating" />}
      </div>
      {(result?.warnings || []).map((wn) => <div key={wn} className="mt-1 text-[10.5px] text-mut">ⓘ {wn}</div>)}
    </div>
  );
}

function FireCount({ n, loading, has, err, range, local, crossed = 0, forS = 0 }: {
  n: number; loading: boolean; has: boolean; err?: string | null; range?: string; local: boolean; crossed?: number; forS?: number }) {
  if (err || !has) return <span className="text-[12px] text-mut">Backtest{range ? ` · ${range}` : ""}{!err && loading ? " — reading the data…" : ""}</span>;
  return (
    <span className="text-[12.5px]" aria-live="polite">
      {n === 0 ? (
        <span className="text-emerald-300">✓ Would not have fired{range ? ` in the last ${range}` : ""}
          {/* the line was crossed but never long enough: say it, else « 0 » looks like a bug next to the spikes */}
          {crossed > 0 && <span className="text-mut"> — crossed the line {crossed} time{crossed === 1 ? "" : "s"}, never for {fmtDur(`${forS}s`)}</span>}
        </span>
      ) : (
        <span className="text-slate-100"><span className="font-semibold text-red-300">▲ Would have fired {n} time{n === 1 ? "" : "s"}</span>{range ? ` in the last ${range}` : ""}</span>
      )}
      {local && <span className="ml-1.5 text-[10.5px] text-mut">(estimate — checking…)</span>}
    </span>
  );
}

function DataTable({ drawn, fires, unit, height }: { drawn: Series[]; fires: Interval[]; unit?: string | null; height: number }) {
  const rows = (drawn[0]?.points || []).slice(-60).reverse();
  return (
    <div className="grid h-full grid-cols-1 gap-2 overflow-auto p-2 text-[11px] sm:grid-cols-2" style={{ maxHeight: height }}>
      <table className="w-full">
        <caption className="mb-1 text-left text-[11px] font-medium text-slate-300">Would have fired ({fires.length})</caption>
        <thead><tr className="text-left text-mut"><th className="font-normal">from</th><th className="font-normal">to</th><th className="font-normal">group</th><th className="text-right font-normal">peak</th></tr></thead>
        <tbody>
          {fires.length === 0 && <tr><td colSpan={4} className="text-mut">never</td></tr>}
          {fires.map((f, i) => (
            <tr key={i} className="border-t border-line/60"><td>{fmtTime(f.start, true)}</td><td>{f.end ? fmtTime(f.end) : "now"}</td>
              <td className="text-[10.5px] text-mut" title={f.group_key || ""}>{groupTitle(drawn.find((d) => (d.group_key || "") === (f.group_key || "")) || { group_key: f.group_key })}</td><td className="text-right tabular-nums">{fmtValue(f.peak, unit)}</td></tr>
          ))}
        </tbody>
      </table>
      <table className="w-full">
        <caption className="mb-1 text-left text-[11px] font-medium text-slate-300">Latest values{drawn.length > 1 ? ` (${groupTitle(drawn[0])})` : ""}</caption>
        <thead><tr className="text-left text-mut"><th className="font-normal">time</th><th className="text-right font-normal">value</th></tr></thead>
        <tbody>
          {rows.map(([t, v]) => <tr key={t} className="border-t border-line/60"><td>{fmtTime(t, true)}</td><td className="text-right tabular-nums">{fmtValue(v, unit)}</td></tr>)}
        </tbody>
      </table>
    </div>
  );
}
