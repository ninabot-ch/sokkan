"use client";
import { useMemo, useState } from "react";
import type { MemNote } from "@/lib/types";

// Force-directed layout of the notes' [[wikilink]] graph — no chart lib, a
// deterministic simulation run once per data change, rendered as static SVG.
const W = 900, H = 620, ITER = 220;

interface Node { name: string; x: number; y: number; r: number; type: string; priority: boolean; deg: number }

function layout(notes: MemNote[]): { nodes: Node[]; edges: [string, string][] } {
  const names = new Set(notes.map((n) => n.name));
  const edges: [string, string][] = [];
  for (const n of notes) for (const l of n.links) if (names.has(l)) edges.push([n.name, l]);
  const deg: Record<string, number> = {};
  for (const [a, b] of edges) { deg[a] = (deg[a] || 0) + 1; deg[b] = (deg[b] || 0) + 1; }

  // deterministic start: ring ordered by name (no Math.random → stable layout)
  const nodes: Node[] = notes.map((n, i) => {
    const ang = (2 * Math.PI * i) / Math.max(1, notes.length);
    return { name: n.name, x: W / 2 + Math.cos(ang) * H * 0.38, y: H / 2 + Math.sin(ang) * H * 0.38,
             r: 4 + Math.min(6, (deg[n.name] || 0)), type: n.type, priority: !!n.priority, deg: deg[n.name] || 0 };
  });
  const idx = new Map(nodes.map((n, i) => [n.name, i]));
  const K = Math.sqrt((W * H) / Math.max(1, nodes.length));  // ideal spacing

  for (let it = 0; it < ITER; it++) {
    const t = 1 - it / ITER;                                  // cooling
    const fx = new Array(nodes.length).fill(0), fy = new Array(nodes.length).fill(0);
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {           // repulsion
        const dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
        const d2 = Math.max(64, dx * dx + dy * dy);
        const f = (K * K) / d2;
        fx[i] += dx * f; fy[i] += dy * f; fx[j] -= dx * f; fy[j] -= dy * f;
      }
    }
    for (const [a, b] of edges) {                             // springs
      const i = idx.get(a)!, j = idx.get(b)!;
      const dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
      const d = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const f = (d - K) / d * 0.06;
      fx[i] -= dx * f; fy[i] -= dy * f; fx[j] += dx * f; fy[j] += dy * f;
    }
    for (let i = 0; i < nodes.length; i++) {
      fx[i] += (W / 2 - nodes[i].x) * 0.012;                  // gentle centering
      fy[i] += (H / 2 - nodes[i].y) * 0.012;
      const step = Math.min(14 * t + 1, Math.hypot(fx[i], fy[i]));
      const norm = Math.hypot(fx[i], fy[i]) || 1;
      nodes[i].x = Math.max(20, Math.min(W - 20, nodes[i].x + (fx[i] / norm) * step));
      nodes[i].y = Math.max(20, Math.min(H - 20, nodes[i].y + (fy[i] / norm) * step));
    }
  }
  return { nodes, edges };
}

const TYPE_COLORS: Record<string, string> = {
  project: "#5aa7d6", feedback: "#d6a75a", reference: "#8b7ad6", user: "#5ad68f",
};

export default function MemoryGraph({ notes, onPick }: { notes: MemNote[]; onPick: (n: string) => void }) {
  const [hover, setHover] = useState<string | null>(null);
  const { nodes, edges } = useMemo(() => layout(notes), [notes]);
  const idx = useMemo(() => new Map(nodes.map((n) => [n.name, n])), [nodes]);
  const linked = useMemo(() => {
    if (!hover) return null;
    const s = new Set([hover]);
    for (const [a, b] of edges) { if (a === hover) s.add(b); if (b === hover) s.add(a); }
    return s;
  }, [hover, edges]);

  if (!nodes.length) return <div className="mt-10 text-center text-[13px] text-mut">no notes to graph yet</div>;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="px-1 pb-1 text-[10.5px] text-mut">
        {nodes.length} notes · {edges.length} links — node size = connections · ★ = priority · click to open
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="min-h-0 w-full flex-1 rounded-lg border border-line bg-[#0b0f16]">
        {edges.map(([a, b], i) => {
          const na = idx.get(a)!, nb = idx.get(b)!;
          const dim = linked && !(linked.has(a) && linked.has(b) && (a === hover || b === hover));
          return <line key={i} x1={na.x} y1={na.y} x2={nb.x} y2={nb.y}
            stroke={dim ? "#26303d" : "#4a5a6d"} strokeWidth={dim ? 0.6 : 1.2} />;
        })}
        {nodes.map((n) => {
          const dim = linked ? !linked.has(n.name) : false;
          const color = TYPE_COLORS[n.type] || "#7d8a99";
          return (
            <g key={n.name} className="cursor-pointer" opacity={dim ? 0.25 : 1}
               onMouseEnter={() => setHover(n.name)} onMouseLeave={() => setHover(null)}
               onClick={() => onPick(n.name)}>
              <circle cx={n.x} cy={n.y} r={n.r} fill={color}
                stroke={n.priority ? "#e3b341" : "#0b0f16"} strokeWidth={n.priority ? 2 : 1} />
              {(hover === n.name || n.deg >= 3 || n.priority || nodes.length <= 30) && (
                <text x={n.x} y={n.y - n.r - 3} textAnchor="middle" fontSize={9.5}
                  fill={hover === n.name ? "#e2e8f0" : "#94a3b8"}>{n.priority ? "★ " : ""}{n.name}</text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
