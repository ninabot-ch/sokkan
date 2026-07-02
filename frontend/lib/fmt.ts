// helpers d'affichage partagés (Board / Journal / Preview)

export function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "à l’instant";
  if (s < 3600) return `il y a ${Math.floor(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`;
  return `il y a ${Math.floor(s / 86400)} j`;
}

export function stamp(ts: number): string {
  return new Date(ts * 1000).toLocaleString("fr-CH", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

// priorités carte : 0 urgente · 1 haute · 2 normale · 3 basse
export const PRIORITIES: Record<number, { label: string; text: string; dot: string; border: string }> = {
  0: { label: "urgente", text: "text-red-300", dot: "bg-red-400", border: "#f87171" },
  1: { label: "haute", text: "text-amber-300", dot: "bg-amber-400", border: "#fbbf24" },
  2: { label: "normale", text: "text-sky-300", dot: "bg-sky-400", border: "#38bdf8" },
  3: { label: "basse", text: "text-slate-400", dot: "bg-slate-500", border: "#64748b" },
};

export function dueTone(due: string): string {
  if (!due) return "";
  const d = new Date(`${due}T23:59:59`);
  const today = new Date();
  if (d.getTime() < today.getTime() - 86400_000) return "text-red-300 ring-red-500/40";
  if (d.toDateString() === today.toDateString()) return "text-amber-300 ring-amber-500/40";
  return "text-mut ring-line";
}
