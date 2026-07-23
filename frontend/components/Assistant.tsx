"use client";
import { useEffect, useRef, useState } from "react";
import { useFeatures } from "@/lib/features";

type Msg = { role: string; content: string; ts?: number };

// Nina — l'agente d'assistance embarquée (S1) : bouton flottant + panneau.
// Feature-gated serveur (SOKKAN_FEATURE_ASSISTANT) — le flag front ne fait
// que masquer le bouton.
export default function Assistant() {
  const features = useFeatures();
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const loaded = useRef(false);

  useEffect(() => {
    if (!open || loaded.current) return;
    loaded.current = true;
    fetch("/api/assistant/history", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : []))
      .then(setMsgs)
      .catch(() => {});
  }, [open]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs, busy]);

  if (!features.assistant) return null;

  const send = async () => {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setErr(null);
    setMsgs((m) => [...m, { role: "user", content: message }]);
    setBusy(true);
    try {
      const r = await fetch("/api/assistant/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data?.detail || `erreur ${r.status}`);
      setMsgs((m) => [...m, { role: "assistant", content: data.reply }]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "erreur inconnue");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {/* bouton flottant */}
      <button
        onClick={() => setOpen((o) => !o)}
        title="Nina — assistance"
        className="fixed bottom-5 right-5 z-40 flex h-12 w-12 items-center justify-center rounded-full border border-line bg-panel text-xl shadow-lg transition hover:border-amber-500/60"
      >
        {open ? "✕" : "🧭"}
      </button>

      {/* panneau */}
      {open && (
        <div className="fixed bottom-20 right-5 z-40 flex h-[min(560px,75vh)] w-[min(400px,92vw)] flex-col rounded-2xl border border-line bg-[#0d0f14] shadow-2xl">
          <div className="border-b border-line px-4 py-3">
            <div className="text-sm font-semibold text-slate-100">Nina</div>
            <div className="text-[11px] text-mut">
              Votre ingénieure DevOps — produit, mémoire, flotte, coûts.
            </div>
          </div>
          <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
            {msgs.length === 0 && !busy && (
              <div className="text-[13px] leading-relaxed text-mut">
                Bonjour 👋 Je connais SOKKAN par cœur. Par exemple :
                <ul className="mt-2 list-disc pl-4">
                  <li>« Comment importer mon projet ? »</li>
                  <li>« Comment semer la mémoire de ce projet ? »</li>
                  <li>« Worker ou plan supérieur, comment choisir ? »</li>
                </ul>
              </div>
            )}
            {msgs.map((m, i) => (
              <div
                key={i}
                className={
                  m.role === "user"
                    ? "ml-6 rounded-xl bg-blue-500/10 px-3 py-2 text-[13px] leading-relaxed text-slate-100"
                    : "mr-6 whitespace-pre-wrap rounded-xl border border-line bg-panel px-3 py-2 text-[13px] leading-relaxed text-slate-200"
                }
              >
                {m.content}
              </div>
            ))}
            {busy && <div className="mr-6 animate-pulse text-[13px] text-mut">Nina réfléchit…</div>}
            {err && <div className="text-[12px] text-red-400">{err}</div>}
            <div ref={endRef} />
          </div>
          <div className="flex gap-2 border-t border-line p-3">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
              placeholder="Votre question…"
              className="flex-1 rounded-lg border border-line bg-[#07080a] px-3 py-2 text-[13px] text-slate-100 outline-none focus:border-blue-500/50"
            />
            <button
              onClick={send}
              disabled={busy || !input.trim()}
              className="rounded-lg border border-line bg-panel px-3 py-2 text-[13px] text-slate-100 disabled:opacity-40"
            >
              ➤
            </button>
          </div>
        </div>
      )}
    </>
  );
}
