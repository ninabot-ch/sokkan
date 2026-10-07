"use client";
// 3.2 `shared_review` — the recipient's side, at the top of the session rail: what was
// shared with me (« shared by … »), read or write, and the tool approvals delegated to me.
import { useEffect, useState } from "react";
import { shareDecide, shareOpen, shareShotUrl, sharesInbox, useFeatureOn, when, type Share } from "@/lib/uifeatures";
import { AccessChip } from "./ShareDialog";

function PreviewViewer({ s, onClose }: { s: Share; onClose: () => void }) {
  const [src] = useState(shareShotUrl(s.id));
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    shareOpen(s.id).catch(() => {});                  // logged: the recipient opened it
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [s.id, onClose]);
  return (
    <div className="fixed inset-0 z-[70] flex items-start justify-center bg-black/70 p-4 pt-12" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-labelledby="sp-title"
        className="flex max-h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
          <span aria-hidden>🖼</span>
          <span id="sp-title" className="text-[13.5px] font-medium text-slate-100">{s.title || "Shared preview"}</span>
          <span className="ui-chip ui-c-read">shared by {s.created_by}</span>
          <AccessChip access={s.effective_access ?? s.access} />
          <code className="truncate text-[11px] text-mut">{s.target}</code>
          <button onClick={onClose} aria-label="close" className="ui-focus ml-auto rounded px-1.5 text-mut hover:text-slate-200">✕</button>
        </div>
        {s.note && <div className="border-b border-line bg-panel2/40 px-4 py-2 text-[12px] text-slate-300">“{s.note}”</div>}
        <div className="min-h-0 flex-1 overflow-auto bg-ink p-3">
          {loading && !failed && <div role="status" className="p-6 text-center text-[12px] text-mut">capturing the preview…</div>}
          {failed && <div role="alert" className="p-6 text-center text-[12px] text-amber-300">The preview is not reachable right now (dev server stopped?).</div>}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={src} alt={`preview of ${s.target}`} onLoad={() => setLoading(false)}
            onError={() => { setLoading(false); setFailed(true); }}
            className={`mx-auto rounded-lg border border-line ${loading || failed ? "hidden" : ""}`} />
        </div>
        <div className="border-t border-line px-4 py-2 text-[10.5px] text-mut">
          {s.expires_at ? `Shared until ${when(s.expires_at)}` : "Shared with no end date"} · project {s.project}</div>
      </div>
    </div>
  );
}

export default function SharedWithMe({ onOpenSession }: { onOpenSession: (sid: string) => void }) {
  const on = useFeatureOn("shared_review");
  const [items, setItems] = useState<Share[]>([]);
  const [openPrev, setOpenPrev] = useState<Share | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [msg, setMsg] = useState("");
  const reload = () => sharesInbox().then(setItems).catch(() => setItems([]));
  useEffect(() => {
    if (!on) return;
    reload();
    const iv = setInterval(reload, 5000);
    return () => clearInterval(iv);
  }, [on]);
  if (!on || !items.length) return null;

  const decide = (s: Share, pid: string, d: "allow" | "deny") =>
    shareDecide(s.id, pid, d).then(() => { setMsg(`${d === "allow" ? "Allowed" : "Denied"} — logged.`); reload(); })
      .catch((e) => setMsg(String(e.message || e)));

  return (
    <section aria-label="Shared with me" className="border-b border-line bg-panel2/30">
      <div className="flex items-center gap-2 px-3 pt-2 text-[11px] font-semibold uppercase tracking-wide text-mut">
        <span aria-hidden>⇪</span> Shared with me <span className="ml-auto rounded-full bg-panel px-1.5 text-[10px] text-slate-300 ring-1 ring-line">{items.length}</span>
      </div>
      <ul className="py-1">
        {items.map((s) => {
          const pend = s.pending?.length ?? 0;
          const isOpen = expanded === s.id;
          return (
            <li key={s.id} className={`mx-2 my-1 rounded-lg border ${pend ? "ui-attn border-amber-400/60" : "border-line"} bg-panel`}>
              <button onClick={() => s.kind === "preview" ? setOpenPrev(s) : setExpanded(isOpen ? null : s.id)}
                aria-expanded={s.kind === "session" ? isOpen : undefined}
                className="ui-focus block w-full px-2 py-1.5 text-left">
                <span className="flex flex-wrap items-center gap-1">
                  <span className="rounded bg-brass/15 px-1.5 text-[10px] text-brass">{s.kind === "preview" ? "preview" : "session"}</span>
                  <AccessChip access={s.effective_access ?? s.access} />
                  {pend > 0 ? <span className="ui-chip ui-c-approval"><span aria-hidden>✋</span>{pend} to validate</span>
                    : s.approver && <span className="ui-chip ui-c-approval"><span aria-hidden>✋</span>you validate</span>}
                </span>
                <span className="mt-0.5 block truncate text-[12px] text-slate-200">{s.title || s.target}</span>
                <span className="block truncate text-[10px] text-mut">shared by {s.created_by}{s.project !== "default" ? ` · ${s.project}` : ""}
                  {s.expires_at ? ` · until ${when(s.expires_at)}` : ""}</span>
              </button>
              {isOpen && s.kind === "session" && (
                <div className="space-y-1.5 border-t border-line px-2 py-2">
                  {s.note && <div className="text-[11px] text-slate-300">“{s.note}”</div>}
                  {(s.pending ?? []).map((p) => (
                    <div key={p.id} className="rounded border border-amber-400/40 bg-amber-400/5 p-1.5">
                      <div className="text-[11px] text-slate-200"><b>{p.tool}</b> — {p.title}</div>
                      <div className="mt-1 flex gap-1">
                        <button onClick={() => decide(s, p.id, "allow")} className="ui-focus rounded bg-emerald-600/80 px-2 py-0.5 text-[11px] text-white hover:bg-emerald-600">✓ allow</button>
                        <button onClick={() => decide(s, p.id, "deny")} className="ui-focus rounded border border-red-500/50 px-2 py-0.5 text-[11px] text-red-300 hover:bg-red-500/10">✕ deny</button>
                      </div>
                    </div>
                  ))}
                  {s.approver && !pend && <div className="text-[10.5px] text-mut">You validate this session — nothing waiting.</div>}
                  <button onClick={() => { shareOpen(s.id).catch(() => {}); onOpenSession(s.target); }}
                    className="ui-focus w-full rounded bg-sea/80 py-1 text-[11.5px] font-medium text-white hover:bg-sea">open the session</button>
                  <div aria-live="polite" className="text-[10.5px] text-mut">{msg}</div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {openPrev && <PreviewViewer s={openPrev} onClose={() => setOpenPrev(null)} />}
    </section>
  );
}
