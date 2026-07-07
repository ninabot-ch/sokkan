"use client";
import { useEffect, useState } from "react";
import { useMe, useCan } from "@/lib/me";
import { llmStatus, llmUsage, llmSetApiKey, llmSetSubscription, type LlmStatus, type LlmUsage } from "@/lib/api";

function Bar({ used, quota }: { used: number; quota: number }) {
  const pct = quota ? Math.min(100, (used / quota) * 100) : 0;
  const c = pct > 90 ? "bg-red-500" : pct > 70 ? "bg-amber-400" : "bg-emerald-500";
  return <div className="h-1.5 w-full overflow-hidden rounded-full bg-line"><div className={`h-full ${c}`} style={{ width: `${Math.max(2, pct)}%` }} /></div>;
}
const fmt = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}k` : `${n}`;

export default function Settings({ onClose }: { onClose: () => void }) {
  const me = useMe();
  const isAdmin = useCan("admin");
  const [st, setSt] = useState<LlmStatus | null>(null);
  const [use, setUse] = useState<LlmUsage | null>(null);
  const [choice, setChoice] = useState<"api" | "sub" | null>(null);
  const [val, setVal] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const reload = () => { llmStatus().then(setSt).catch(() => {}); llmUsage().then(setUse).catch(() => setUse(null)); };
  useEffect(reload, []);

  const save = () => {
    setErr(""); if (!val.trim()) return; setBusy(true);
    const p = choice === "api" ? llmSetApiKey(val.trim()) : llmSetSubscription(val.trim());
    p.then((s) => { setSt(s); setChoice(null); setVal(""); }).catch((e) => setErr(String(e))).finally(() => setBusy(false));
  };

  const included = st?.mode === "included";
  const active = st?.configured;

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/60 p-4 pt-16" onClick={onClose}>
      <div className="w-full max-w-xl rounded-2xl border border-line bg-panel shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center border-b border-line px-5 py-3">
          <div>
            <div className="text-[15px] font-semibold text-slate-100">Réglages · Modèle</div>
            <div className="text-[11px] text-mut">{me?.email}</div>
          </div>
          <button onClick={onClose} className="ml-auto text-mut hover:text-slate-200">✕</button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto p-5">
          {/* état courant */}
          <div className="mb-4 rounded-lg border border-line bg-panel2/50 p-3 text-[12.5px]">
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${active ? "bg-emerald-500" : "bg-amber-400"}`} />
              <span className="text-slate-200">
                {included ? "Inférence incluse — opérée par NINABOT"
                  : st?.byok_kind === "api_key" ? "Votre clé API Anthropic"
                  : st?.byok_kind === "subscription" ? "Votre abonnement Claude Pro/Max"
                  : st?.mode === "env" ? "Clé configurée (environnement)"
                  : "Aucun modèle configuré — vos sessions ne peuvent pas démarrer"}
              </span>
            </div>
            {included && use && (
              <div className="mt-2.5">
                <div className="flex justify-between text-[11px] text-mut">
                  <span>Aujourd'hui</span>
                  <span className="text-slate-300">{fmt(use.used_today)} / {use.daily_quota_tokens ? fmt(use.daily_quota_tokens) : "∞"} tokens</span>
                </div>
                <div className="mt-1"><Bar used={use.used_today} quota={use.daily_quota_tokens} /></div>
                <div className="mt-1 text-[10px] text-mut">Réinitialisé à minuit UTC. Facturé par NINABOT.</div>
              </div>
            )}
          </div>

          {included ? (
            <div className="text-[12px] text-mut">
              Cette instance est en <b className="text-slate-200">inférence incluse</b> : NINABOT fournit et opère le modèle,
              avec un quota journalier. Pour passer en clé personnelle (BYOK), contactez-nous.
            </div>
          ) : !isAdmin ? (
            <div className="text-[12px] text-mut">La configuration du modèle est réservée aux administrateurs de l'instance.</div>
          ) : (
            <>
              <div className="mb-2 text-[12px] font-semibold text-slate-200">Comment vos sessions accèdent au modèle</div>
              <div className="space-y-2">
                <button onClick={() => { setChoice("api"); setVal(""); }}
                  className={`block w-full rounded-lg border p-3 text-left ${choice === "api" ? "border-sea bg-sea/10" : "border-line hover:border-line/80"}`}>
                  <div className="text-[13px] text-slate-100">Clé API Anthropic <span className="text-mut">(BYOK)</span></div>
                  <div className="text-[11px] text-mut">Vos sessions tapent Anthropic en direct, facturé sur votre compte. Aucune limite de notre part. La clé reste sur votre machine.</div>
                </button>
                <button onClick={() => { setChoice("sub"); setVal(""); }}
                  className={`block w-full rounded-lg border p-3 text-left ${choice === "sub" ? "border-sea bg-sea/10" : "border-line hover:border-line/80"}`}>
                  <div className="text-[13px] text-slate-100">Abonnement Claude Pro / Max</div>
                  <div className="text-[11px] text-mut">Sur votre poste : <code className="text-slate-300">claude setup-token</code> → collez le jeton ici. Utilise votre abonnement, pas de facture à l'usage.</div>
                </button>
                <div className="rounded-lg border border-line/60 bg-panel2/30 p-3 opacity-70">
                  <div className="text-[13px] text-slate-300">Inférence incluse (opérée par NINABOT)</div>
                  <div className="text-[11px] text-mut">Modèle fourni et facturé par NINABOT, avec quota. Se choisit à la souscription — pas activable ici.</div>
                </div>
              </div>

              {choice && (
                <div className="mt-3 rounded-lg border border-line bg-panel2/40 p-3">
                  <input value={val} onChange={(e) => setVal(e.target.value)} type="password"
                    placeholder={choice === "api" ? "sk-ant-…" : "jeton claude setup-token…"}
                    className="w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
                  <div className="mt-2 flex items-center gap-2">
                    <button disabled={busy} onClick={save}
                      className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-50">enregistrer</button>
                    <button onClick={() => { setChoice(null); setVal(""); }} className="text-[12px] text-mut hover:text-slate-200">annuler</button>
                    <span className="ml-auto text-[10px] text-mut">reste sur votre VM, jamais transmis</span>
                  </div>
                  {err && <div className="mt-1 text-[11px] text-red-400">{err}</div>}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
