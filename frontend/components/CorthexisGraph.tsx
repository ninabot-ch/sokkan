"use client";
/* CortHeXis graph — a port of the hexis-brain renderer: d3-force on a canvas, pulses
 * travelling along the links, broken links as dashed red edges to a « ghost »,
 * four readings (Synapses / Constellation / Age / Health). Wheel = zoom, drag the
 * background = pan, drag a note = move it, click = open, double-click = focus. */
import { useEffect, useRef } from "react";
import {
  forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY,
  type Simulation, type SimulationLinkDatum, type SimulationNodeDatum,
} from "d3-force";
import type { CxGraph, Severity } from "@/lib/corthexis";

export type Mode = "synapse" | "semantic" | "age" | "health";

export const TYPE_COLORS: Record<string, string> = {
  project: "#d4a74a", feedback: "#9b7bff", reference: "#3ecfb2", user: "#e9e6e1",
  unknown: "#6b7280",
};
const RED = "#ff5c6c", ORANGE = "#ffb454", CYAN = "#5ee1ff";

interface N extends SimulationNodeDatum {
  id: string; ghost?: boolean; label?: string; type: string; words: number; in: number;
  out: number; level: Severity | null; age: number | null; sx: number | null; sy: number | null;
  desc?: string; refs?: number; neighbors: Set<string>;
}
interface L extends SimulationLinkDatum<N> { source: N | string; target: N | string; broken: boolean }

function mix(a: string, b: string, t: number): string {
  const p = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const [x, y] = [p(a), p(b)];
  return `rgb(${x.map((v, i) => Math.round(v + (y[i] - v) * t)).join(",")})`;
}

export default function CorthexisGraph({
  graph, mode, labels, hidden, highlight, selected, flashes, onPick, onGhost,
}: {
  graph: CxGraph | null; mode: Mode; labels: boolean; hidden: Set<string>;
  highlight: Map<string, number>; selected: string | null; flashes: Map<string, number>;
  onPick: (id: string) => void; onGhost: (label: string, citedBy: string[]) => void;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const cv = useRef<HTMLCanvasElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const S = useRef({
    nodes: [] as N[], links: [] as L[], byId: new Map<string, N>(), sim: null as Simulation<N, L> | null,
    t: { x: 0, y: 0, k: 1 }, W: 800, H: 600, hover: null as N | null, particles: [] as { l: L; t: number; v: number }[],
    props: { mode, labels, hidden, highlight, selected, flashes },
    drag: null as null | { node: N | null; x0: number; y0: number; tx: number; ty: number; moved: boolean },
  });
  S.current.props = { mode, labels, hidden, highlight, selected, flashes };
  const cb = useRef({ onPick, onGhost });
  cb.current = { onPick, onGhost };

  const radius = (n: N) => n.ghost ? 3.2 : 3 + Math.min(9, Math.sqrt(n.words || 50) / 6) + Math.min(6, (n.in || 0) * 0.6);
  const color = (n: N) => {
    const m = S.current.props.mode;
    if (n.ghost) return RED;
    if (m === "age") return n.age == null ? "#4b5563" : mix(CYAN, "#6b4630", Math.min(1, n.age / 120));
    if (m === "health") return n.level === "crit" ? RED : n.level === "warn" ? ORANGE : n.level === "info" ? "#8a94a6" : "#2b3140";
    return TYPE_COLORS[n.type] || TYPE_COLORS.unknown;
  };

  function applyMode() {
    const s = S.current, sim = s.sim;
    if (!sim) return;
    const semantic = s.props.mode === "semantic";
    const R = Math.min(s.W, s.H) * 0.42;
    sim.force("x", forceX<N>((d) => semantic && d.sx != null ? s.W / 2 + d.sx * R : s.W / 2).strength(semantic ? 0.28 : 0.05));
    sim.force("y", forceY<N>((d) => semantic && d.sy != null ? s.H / 2 + d.sy * R * 0.85 : s.H / 2).strength(semantic ? 0.28 : 0.06));
    (sim.force("link") as ReturnType<typeof forceLink<N, L>>).strength((l) => semantic ? 0.05 : l.broken ? 0.25 : 0.55);
    (sim.force("charge") as ReturnType<typeof forceManyBody<N>>).strength((d) => semantic ? -25 : d.ghost ? -30 : -95);
    sim.alpha(0.6).restart();
  }

  // data → simulation (positions kept across refreshes)
  useEffect(() => {
    const s = S.current;
    if (!graph) return;
    const prev = s.byId;
    const first = prev.size === 0;
    const nodes: N[] = graph.nodes.map((n) => Object.assign(
      prev.get(n.id) || { x: s.W / 2 + (Math.random() - 0.5) * 200, y: s.H / 2 + (Math.random() - 0.5) * 200 },
      n, { neighbors: new Set<string>() }) as N);
    for (const g of graph.ghosts) nodes.push(Object.assign(
      prev.get(g.id) || { x: s.W / 2 + (Math.random() - 0.5) * 500, y: s.H / 2 + (Math.random() - 0.5) * 500 },
      { ...g, type: "ghost", words: 0, in: 0, out: 0, level: null, age: null, sx: null, sy: null, neighbors: new Set<string>() }) as N);
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const links: L[] = graph.edges.filter((e) => byId.has(e.s) && byId.has(e.t))
      .map((e) => ({ source: e.s, target: e.t, broken: !!e.broken }));
    for (const l of links) { byId.get(l.source as string)!.neighbors.add(l.target as string); byId.get(l.target as string)!.neighbors.add(l.source as string); }
    s.sim?.stop();
    s.sim = forceSimulation<N, L>(nodes)
      .force("link", forceLink<N, L>(links).id((d) => d.id).distance((l) => l.broken ? 40 : 46 + 22 * Math.random()).strength((l) => l.broken ? 0.25 : 0.55))
      .force("charge", forceManyBody<N>().strength((d) => d.ghost ? -30 : -95).distanceMax(420))
      .force("collide", forceCollide<N>().radius((d) => radius(d) + 4).iterations(2))
      .force("center", forceCenter(s.W / 2, s.H / 2))
      .force("x", forceX<N>(s.W / 2).strength(0.04)).force("y", forceY<N>(s.H / 2).strength(0.05))
      .alphaDecay(0.02).velocityDecay(0.35);
    s.nodes = nodes; s.links = links; s.byId = byId;
    s.particles = links.filter((l) => !l.broken && Math.random() < 0.55).map((l) => ({ l, t: Math.random(), v: 0.002 + Math.random() * 0.004 }));
    applyMode();
    if (first) {
      // frame the whole memory once the layout has settled a bit
      const fit = () => {
        const pts = s.nodes.filter((n) => n.x != null);
        if (!pts.length) return;
        const xs = pts.map((n) => n.x!), ys = pts.map((n) => n.y!);
        const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
        const k = Math.max(0.3, Math.min(3, Math.min(s.W / (x1 - x0 + 160), (s.H - 120) / (y1 - y0 + 160))));
        s.t = { x: s.W / 2 - ((x0 + x1) / 2) * k, y: (s.H + 40) / 2 - ((y0 + y1) / 2) * k, k };
      };
      setTimeout(fit, 1600);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  useEffect(() => { applyMode(); }, [mode]); // eslint-disable-line react-hooks/exhaustive-deps

  // focus the selected note
  useEffect(() => {
    const s = S.current, n = selected ? s.byId.get(selected) : null;
    if (!n || n.x == null) return;
    const k = Math.max(1.4, s.t.k);
    s.t = { x: s.W / 2 - n.x * k - 180, y: s.H / 2 - n.y! * k, k };
  }, [selected]);

  // canvas size + render loop + input
  useEffect(() => {
    const s = S.current, c = cv.current!, ctx = c.getContext("2d")!;
    let raf = 0, dpr = 1;
    const resize = () => {
      const r = wrap.current!.getBoundingClientRect();
      dpr = Math.min(2, window.devicePixelRatio || 1);
      s.W = r.width; s.H = r.height;
      c.width = r.width * dpr; c.height = r.height * dpr;
      c.style.width = r.width + "px"; c.style.height = r.height + "px";
      s.sim?.force("center", forceCenter(s.W / 2, s.H / 2)); applyMode();
    };
    const ro = new ResizeObserver(resize); ro.observe(wrap.current!); resize();

    const vis = (n: N) => !s.props.hidden.has(n.ghost ? "ghost" : n.type);
    const draw = (time: number) => {
      const { t } = s, P = s.props;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const g = ctx.createRadialGradient(s.W * 0.5, s.H * 0.45, 0, s.W * 0.5, s.H * 0.45, Math.max(s.W, s.H) * 0.7);
      g.addColorStop(0, "#0e1522"); g.addColorStop(0.6, "#0a0e16"); g.addColorStop(1, "#06080d");
      ctx.fillStyle = g; ctx.fillRect(0, 0, s.W, s.H);
      ctx.translate(t.x, t.y); ctx.scale(t.k, t.k);
      const focus = s.hover || (P.selected ? s.byId.get(P.selected) || null : null);
      const lit = P.highlight.size > 0;
      for (const l of s.links) {
        const a = l.source as N, b = l.target as N;
        if (!vis(a) || !vis(b) || a.x == null || b.x == null) continue;
        let alpha = 0.1, w = 0.8, col = "212,167,74";
        if (focus) { const on = focus === a || focus === b; alpha = on ? 0.75 : 0.03; w = on ? 1.6 : 0.6; if (on) col = "94,225,255"; }
        if (lit) { const m = Math.max(P.highlight.get(a.id) || 0, P.highlight.get(b.id) || 0); alpha = m ? 0.12 + m * 0.6 : 0.03; if (m) col = "94,225,255"; }
        if (l.broken) { col = "255,92,108"; ctx.setLineDash([3, 4]); alpha = Math.max(alpha, 0.25); } else ctx.setLineDash([]);
        ctx.strokeStyle = `rgba(${col},${alpha})`; ctx.lineWidth = w / t.k;
        ctx.beginPath(); ctx.moveTo(a.x, a.y!); ctx.lineTo(b.x, b.y!); ctx.stroke();
      }
      ctx.setLineDash([]);
      for (const p of s.particles) {
        const a = p.l.source as N, b = p.l.target as N;
        if (!vis(a) || !vis(b) || a.x == null || b.x == null) continue;
        p.t += p.v * (lit ? 2.2 : 1); if (p.t > 1) p.t = 0;
        const hot = focus ? (focus === a || focus === b) : lit ? (P.highlight.has(a.id) || P.highlight.has(b.id)) : true;
        if (!hot && (focus || lit)) continue;
        ctx.fillStyle = hot && (focus || lit) ? "rgba(94,225,255,.95)" : "rgba(240,211,138,.5)";
        ctx.beginPath(); ctx.arc(a.x + (b.x - a.x) * p.t, a.y! + (b.y! - a.y!) * p.t, 1.3 / Math.sqrt(t.k), 0, Math.PI * 2); ctx.fill();
      }
      for (const n of s.nodes) {
        if (!vis(n) || n.x == null) continue;
        const r = radius(n), c0 = color(n);
        let dim = 1;
        if (focus) dim = focus === n || focus.neighbors.has(n.id) ? 1 : 0.18;
        if (lit) dim = P.highlight.has(n.id) ? 1 : 0.12;
        if (P.mode === "health" && !n.level && !n.ghost) dim = Math.min(dim, 0.35);
        ctx.globalAlpha = dim;
        const hs = P.highlight.get(n.id) || 0;
        const fl = P.flashes.get(n.id); let burst = 0;
        if (fl != null) { const d = (time - fl) / 1800; if (d <= 1 && d >= 0) burst = 1 - d; }
        ctx.shadowColor = hs || burst ? CYAN : c0; ctx.shadowBlur = 6 + r * 1.2 + hs * 22 + burst * 36 + (focus === n ? 14 : 0);
        if (n.ghost) {
          ctx.strokeStyle = c0; ctx.lineWidth = 1 / t.k; ctx.setLineDash([2, 2]);
          ctx.beginPath(); ctx.arc(n.x, n.y!, r + 1.5, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
        } else {
          const gr = ctx.createRadialGradient(n.x - r * 0.3, n.y! - r * 0.3, r * 0.1, n.x, n.y!, r);
          gr.addColorStop(0, "#fff"); gr.addColorStop(0.25, c0); gr.addColorStop(1, "rgba(0,0,0,.55)");
          ctx.fillStyle = gr; ctx.beginPath(); ctx.arc(n.x, n.y!, r, 0, Math.PI * 2); ctx.fill();
          if (hs || burst) { ctx.strokeStyle = `rgba(94,225,255,${0.5 + 0.5 * Math.max(hs, burst)})`; ctx.lineWidth = 1.2 / t.k; ctx.beginPath(); ctx.arc(n.x, n.y!, r + 3 + burst * 18 + Math.sin(time / 250) * 1.5, 0, Math.PI * 2); ctx.stroke(); }
          if (P.mode !== "health" && n.level === "crit") { ctx.strokeStyle = RED; ctx.lineWidth = 1 / t.k; ctx.beginPath(); ctx.arc(n.x, n.y!, r + 2.5, 0, Math.PI * 2); ctx.stroke(); }
        }
        ctx.shadowBlur = 0;
        const show = P.labels || focus === n || (focus && focus.neighbors.has(n.id)) || hs > 0 || t.k > 2.2 || (n.in || 0) > 9;
        if ((show && !n.ghost) || (n.ghost && (focus === n || t.k > 1.8))) {
          ctx.font = `${Math.max(10, 11 / Math.sqrt(t.k))}px ui-monospace, Menlo, monospace`;
          ctx.fillStyle = focus === n || hs ? "#fff" : `rgba(233,230,225,${n.ghost ? 0.6 : 0.78})`;
          ctx.shadowColor = "rgba(0,0,0,.9)"; ctx.shadowBlur = 6; ctx.textAlign = "center";
          ctx.fillText(n.ghost ? `[[${n.label}]]` : n.id, n.x, n.y! + r + 12 / Math.sqrt(t.k));
          ctx.shadowBlur = 0;
        }
        ctx.globalAlpha = 1;
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);

    const toWorld = (px: number, py: number) => [(px - s.t.x) / s.t.k, (py - s.t.y) / s.t.k];
    const pick = (px: number, py: number): N | null => {
      const [x, y] = toWorld(px, py);
      let best: N | null = null, bd = 1e9;
      for (const n of s.nodes) {
        if (!vis(n) || n.x == null) continue;
        const d = Math.hypot(n.x - x, n.y! - y);
        if (d < radius(n) + 6 / s.t.k && d < bd) { best = n; bd = d; }
      }
      return best;
    };
    const pos = (e: PointerEvent | WheelEvent | MouseEvent) => { const r = c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const [px, py] = pos(e);
      const k = Math.max(0.25, Math.min(8, s.t.k * Math.exp(-e.deltaY * 0.0015)));
      s.t = { x: px - (px - s.t.x) * (k / s.t.k), y: py - (py - s.t.y) * (k / s.t.k), k };
    };
    const onDown = (e: PointerEvent) => {
      const [px, py] = pos(e);
      const node = pick(px, py);
      s.drag = { node, x0: px, y0: py, tx: s.t.x, ty: s.t.y, moved: false };
      c.setPointerCapture(e.pointerId);
      if (node) { s.sim?.alphaTarget(0.25).restart(); node.fx = node.x; node.fy = node.y; }
    };
    const onMove = (e: PointerEvent) => {
      const [px, py] = pos(e), d = s.drag;
      if (d) {
        if (Math.hypot(px - d.x0, py - d.y0) > 3) d.moved = true;
        if (d.node) { const [x, y] = toWorld(px, py); d.node.fx = x; d.node.fy = y; }
        else s.t = { ...s.t, x: d.tx + px - d.x0, y: d.ty + py - d.y0 };
        return;
      }
      const n = pick(px, py);
      s.hover = n;
      c.style.cursor = n ? "pointer" : "grab";
      const el = tip.current!;
      if (!n) { el.hidden = true; return; }
      el.hidden = false;
      el.style.left = Math.min(s.W - 330, px + 16) + "px"; el.style.top = Math.min(s.H - 110, py + 16) + "px";
      el.innerHTML = "";
      const b = document.createElement("b"); b.textContent = n.ghost ? `[[${n.label}]]` : n.id; el.appendChild(b);
      const d1 = document.createElement("div"); d1.className = "text-mut";
      d1.textContent = n.ghost ? `missing note · cited ${n.refs}×` : (n.desc || "").slice(0, 180); el.appendChild(d1);
      if (!n.ghost) {
        const d2 = document.createElement("div"); d2.className = "mt-0.5 text-[10.5px] text-slate-400";
        d2.textContent = `${n.type} · ${n.words} words · ${n.in}↙ ${n.out}↗ · ${n.age != null ? `${n.age} d` : "no date"}${n.level ? " · ⚑ review" : ""}`;
        el.appendChild(d2);
      }
    };
    const onUp = (e: PointerEvent) => {
      const d = s.drag; s.drag = null;
      c.releasePointerCapture(e.pointerId);
      if (d?.node) { s.sim?.alphaTarget(0); d.node.fx = d.node.fy = null; }
      if (d && !d.moved) {
        const n = d.node;
        if (n?.ghost) cb.current.onGhost(n.label!, s.links.filter((l) => l.target === n).map((l) => (l.source as N).id));
        else if (n) cb.current.onPick(n.id);
      }
    };
    const onDbl = (e: MouseEvent) => {
      const [px, py] = pos(e), n = pick(px, py);
      if (n && n.x != null) s.t = { x: s.W / 2 - n.x * 2.2, y: s.H / 2 - n.y! * 2.2, k: 2.2 };
    };
    const onLeave = () => { s.hover = null; if (tip.current) tip.current.hidden = true; };
    c.addEventListener("wheel", onWheel, { passive: false });
    c.addEventListener("pointerdown", onDown); c.addEventListener("pointermove", onMove);
    c.addEventListener("pointerup", onUp); c.addEventListener("dblclick", onDbl);
    c.addEventListener("pointerleave", onLeave);
    return () => {
      cancelAnimationFrame(raf); ro.disconnect();
      c.removeEventListener("wheel", onWheel); c.removeEventListener("pointerdown", onDown);
      c.removeEventListener("pointermove", onMove); c.removeEventListener("pointerup", onUp);
      c.removeEventListener("dblclick", onDbl); c.removeEventListener("pointerleave", onLeave);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div ref={wrap} className="absolute inset-0">
      <canvas ref={cv} className="block touch-none" />
      <div ref={tip} hidden className="pointer-events-none absolute z-10 max-w-[320px] rounded-lg border border-line bg-panel/95 px-3 py-2 text-[12px] text-slate-200 shadow-xl" />
    </div>
  );
}
