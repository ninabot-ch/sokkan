"use client";
/* Memory quarantine (3.1) — a note written by an agent run is NOT in the memory
 * until a human has read it: it is never recalled (spawn, search, hooks) before.
 * Approve = it joins the memory with its provenance; reject = archived. */
import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  quarantineApprove, quarantineGet, quarantineList, quarantineReject, type QuarantinedNote,
} from "@/lib/api";

const prov = (p: Record<string, unknown>) => {
  const bits = [p.agent ? `agent ${p.agent}` : "agent run", p.run ? `run #${p.run}` : ""];
  const ts = typeof p.quarantined_at === "number" ? new Date(p.quarantined_at * 1000).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "";
  return [...bits, ts].filter(Boolean).join(" · ");
};
const bodyOf = (text: string) => text.replace(/^---[\s\S]*?\n---\n/, "").trim();

/** Review one quarantined note: its text, provenance, approve / reject. */
export function QuarantineReview({ name, onDone }: { name: string; onDone?: () => void }) {
  const [q, setQ] = useState<(QuarantinedNote & { text: string }) | null | "gone">(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => { quarantineGet(name).then(setQ).catch(() => setQ("gone")); }, [name]);
  useEffect(load, [load]);
  if (q === null) return null;
  if (q === "gone") return <div className="text-[11.5px] text-mut">Memory note <span className="font-mono">{name}</span>: reviewed (approved or rejected).</div>;
  const act = (fn: () => Promise<unknown>) => fn().then(() => { setQ("gone"); onDone?.(); }).catch((e) => setErr(String(e.message || e)));
  return (
    <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-2.5 text-[12px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded border border-amber-500/50 bg-amber-500/10 px-1.5 text-[10.5px] font-semibold uppercase tracking-wide text-amber-300">⚠ quarantine</span>
        <span className="font-mono text-slate-200">{q.name}</span>
        <span className="text-[11px] text-mut">{prov(q.provenance)}</span>
        {q.exists_in_memory && <span className="text-[11px] text-mut">· replaces the current version</span>}
      </div>
      <div className="mt-1 text-[11px] text-mut">Written by an agent run — not recalled by any session until you approve it. Read it for anything that gives orders.</div>
      <div className="md mt-2 max-h-56 overflow-y-auto rounded border border-line/60 bg-panel2/40 p-2 text-[12.5px] text-slate-200">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{bodyOf(q.text)}</ReactMarkdown>
      </div>
      <div className="mt-2 flex gap-1.5">
        <button onClick={() => act(() => quarantineApprove(q.name))} className="rounded-md bg-emerald-600/25 px-2.5 py-1 text-[11.5px] font-medium text-emerald-200 ring-1 ring-emerald-500/50 hover:bg-emerald-600/40">✓ Add to memory</button>
        <button onClick={() => act(() => quarantineReject(q.name))} className="rounded-md px-2.5 py-1 text-[11.5px] text-mut ring-1 ring-line hover:text-red-300">Reject (archive)</button>
        <button onClick={() => { if (confirm(`Delete ${q.name} for good?`)) act(() => quarantineReject(q.name, true)); }} className="rounded-md px-2.5 py-1 text-[11.5px] text-mut ring-1 ring-line hover:text-red-300">Delete</button>
        {err && <span className="text-red-400">{err}</span>}
      </div>
    </div>
  );
}

/** Button + panel for the CortHeXis tab: every note waiting in quarantine. */
export function QuarantineButton() {
  const [items, setItems] = useState<QuarantinedNote[]>([]);
  const [open, setOpen] = useState(false);
  const load = useCallback(() => { quarantineList().then(setItems).catch(() => {}); }, []);
  useEffect(() => { load(); const iv = setInterval(load, 10000); return () => clearInterval(iv); }, [load]);
  return (
    <>
      <button onClick={() => setOpen((o) => !o)} title="Notes written by agent runs, waiting for a human before they join the memory"
        className={`rounded-lg border px-2.5 py-1.5 text-[12px] backdrop-blur ${items.length ? "border-amber-500/60 bg-amber-500/15 text-amber-300" : "border-line bg-panel/80 text-mut hover:text-slate-200"}`}>
        Quarantine{items.length ? ` · ${items.length}` : ""}
      </button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/55 p-3 pt-[8vh]" onMouseDown={(e) => { if (e.target === e.currentTarget) setOpen(false); }}>
          <div className="max-h-[80vh] w-full max-w-3xl overflow-y-auto rounded-xl border border-line bg-panel p-4 shadow-2xl">
            <div className="mb-1 flex items-center gap-2">
              <span className="text-[15px] font-semibold text-slate-100">Memory quarantine</span>
              <button onClick={() => setOpen(false)} className="ml-auto rounded px-2 text-mut hover:text-slate-200">✕</button>
            </div>
            <div className="mb-3 text-[11.5px] text-mut">Agents read the outside world; what they write is held here, with its provenance, until someone reads it. Nothing here is ever recalled.</div>
            {items.length === 0 ? <div className="text-[12.5px] text-mut">Nothing waiting.</div> :
              <div className="space-y-2">{items.map((n) => <QuarantineReview key={n.name} name={n.name} onDone={load} />)}</div>}
          </div>
        </div>
      )}
    </>
  );
}
