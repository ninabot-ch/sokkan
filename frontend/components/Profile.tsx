"use client";
import { useEffect, useState } from "react";
import { useMe, useCan } from "@/lib/me";
import {
  instanceInfo, instanceRename, iamUsers, iamUpsert, iamDelete,
  llmCredit, llmStatus, llmUsage, llmSetApiKey, llmSetSubscription,
  type InstanceInfo, type LlmStatus, type LlmUsage,
} from "@/lib/api";
import type { IamUser } from "@/lib/types";

const ROLES = ["viewer", "dev", "admin", "owner"];
const fmt = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}k` : `${n}`;
type Section = "account" | "org" | "members" | "model";

function Bar({ used, quota }: { used: number; quota: number }) {
  const pct = quota ? Math.min(100, (used / quota) * 100) : 0;
  const c = pct > 90 ? "bg-red-500" : pct > 70 ? "bg-amber-400" : "bg-emerald-500";
  return <div className="h-1.5 w-full overflow-hidden rounded-full bg-line"><div className={`h-full ${c}`} style={{ width: `${Math.max(2, pct)}%` }} /></div>;
}

// ---------- Mon compte ----------
function Account() {
  const me = useMe();
  const color: Record<string, string> = { owner: "text-brass", admin: "text-sea", dev: "text-emerald-400", viewer: "text-mut" };
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="text-[13px] text-slate-100">{me?.name}</div>
        <div className="text-[11.5px] text-mut">{me?.email}</div>
        <div className="mt-1.5 text-[11px]">rôle <span className={color[me?.role || ""] || "text-mut"}>{me?.role}</span>
          <span className="ml-2 text-mut">· connexion {me?.source}</span></div>
      </div>
      <a href="/api/auth/logout" className="inline-block rounded-lg border border-line px-3 py-1.5 text-[12px] text-slate-200 hover:bg-panel2">Se déconnecter →</a>
    </div>
  );
}

// ---------- Organisation ----------
function Org() {
  const isAdmin = useCan("admin");
  const [inf, setInf] = useState<InstanceInfo | null>(null);
  const [name, setName] = useState("");
  const [edit, setEdit] = useState(false);
  useEffect(() => { instanceInfo().then((i) => { setInf(i); setName(i.org_name); }).catch(() => {}); }, []);
  if (!inf) return null;
  return (
    <div className="space-y-3 text-[12.5px]">
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="text-[11px] text-mut">Nom de l'organisation</div>
        {!edit ? (
          <div className="flex items-center gap-2">
            <span className="text-[14px] text-slate-100">{inf.org_name}</span>
            {isAdmin && <button onClick={() => setEdit(true)} className="text-[11px] text-sea hover:underline">modifier</button>}
          </div>
        ) : (
          <div className="mt-1 flex gap-1.5">
            <input value={name} onChange={(e) => setName(e.target.value)}
              className="flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
            <button onClick={() => instanceRename(name).then((i) => { setInf(i); setEdit(false); })}
              className="rounded bg-sea/80 px-2 py-0.5 text-[11px] text-white hover:bg-sea">ok</button>
            <button onClick={() => { setEdit(false); setName(inf.org_name); }} className="text-[11px] text-mut">annuler</button>
          </div>
        )}
      </div>
      <div className="grid grid-cols-2 gap-2">
        {inf.tier && <div className="rounded-lg border border-line bg-panel2/40 p-3">
          <div className="text-[11px] text-mut">Plan</div><div className="text-[13px] capitalize text-slate-100">{inf.tier}</div></div>}
        <div className="rounded-lg border border-line bg-panel2/40 p-3">
          <div className="text-[11px] text-mut">Adresse</div>
          <a href={inf.public_url} target="_blank" rel="noreferrer" className="truncate text-[12px] text-sea hover:underline">{inf.public_url || "—"}</a></div>
      </div>
      {inf.update?.update_available && (
        <div className="rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-[12px] text-sky-200">
          <b>Nouvelle version disponible : {inf.update.latest}</b>
          <span className="text-mut"> (installée : {inf.update.local_version})</span>
          {inf.tier ? (
            <div className="mt-1 text-[11.5px]">Instance managée : onglet <b>Infra → Ma flotte</b> → « ⬆ mettre à jour » (admin).</div>
          ) : (
            <div className="mt-1 text-[11.5px]">
              Self-hosted — relancez l'installeur depuis le dossier parent de <code>sokkan/</code> :
              <code className="mt-1 block select-all rounded bg-black/40 px-1.5 py-0.5 font-mono text-[11px] text-sky-100">curl -fsSL https://sokkan.ch/install.sh | sh</code>
              Il détecte l'installation existante et la met à jour (votre <code>.env</code> et vos données sont conservés).
            </div>
          )}
        </div>
      )}
      <div className="text-[10.5px] text-mut">Hébergé et opéré par NINABOT — infrastructure souveraine suisse.</div>
    </div>
  );
}

// ---------- Membres (IAM) ----------
function Members() {
  const isAdmin = useCan("admin");
  const [users, setUsers] = useState<IamUser[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("dev");
  const reload = () => iamUsers().then(setUsers).catch(() => setUsers([]));
  useEffect(() => { if (isAdmin) reload(); }, [isAdmin]);
  if (!isAdmin) return <div className="text-[12px] text-mut">Gestion des membres réservée aux administrateurs.</div>;
  return (
    <div className="space-y-2">
      <div className="text-[10.5px] text-mut">Les rôles sont internes à SOKKAN. viewer (lecture) · dev (travail) · admin (gère les membres) · owner.</div>
      {users.map((u) => (
        <div key={u.email} className="flex items-center gap-2 rounded-lg border border-line bg-panel2/50 p-2">
          <div className="min-w-0"><div className="truncate text-[12.5px] text-slate-100">{u.name}</div>
            <div className="truncate text-[10.5px] text-mut">{u.email}</div></div>
          <select value={u.role} disabled={u.role === "owner"} onChange={(e) => iamUpsert(u.email, e.target.value, u.name).then(reload)}
            className="ml-auto rounded border border-line bg-panel2 px-1.5 py-0.5 text-[11.5px] text-slate-200 disabled:opacity-50">
            {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select>
          <button disabled={u.role === "owner"} onClick={() => iamDelete(u.email).then(reload)}
            className="rounded px-1 text-mut hover:text-red-400 disabled:opacity-30" title="retirer">✕</button>
        </div>
      ))}
      <div className="flex items-center gap-2 pt-1">
        <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email@…"
          className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
        <select value={role} onChange={(e) => setRole(e.target.value)} className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select>
        <button onClick={() => { if (email.trim()) iamUpsert(email.trim(), role).then(() => { setEmail(""); reload(); }); }}
          className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">+ membre</button>
      </div>
    </div>
  );
}

// ---------- Modèle (LLM) ----------
function Model() {
  const isAdmin = useCan("admin");
  const [st, setSt] = useState<LlmStatus | null>(null);
  const [use, setUse] = useState<LlmUsage | null>(null);
  const [choice, setChoice] = useState<"api" | "sub" | null>(null);
  const [val, setVal] = useState(""); const [busy, setBusy] = useState(false); const [err, setErr] = useState("");
  useEffect(() => { llmStatus().then(setSt).catch(() => {}); llmUsage().then(setUse).catch(() => setUse(null)); }, []);
  const save = () => { setErr(""); if (!val.trim()) return; setBusy(true);
    (choice === "api" ? llmSetApiKey(val.trim()) : llmSetSubscription(val.trim()))
      .then((s) => { setSt(s); setChoice(null); setVal(""); }).catch((e) => setErr(String(e))).finally(() => setBusy(false)); };
  if (!st) return null;
  const included = st.mode === "included";
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-line bg-panel2/50 p-3 text-[12.5px]">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${st.configured ? "bg-emerald-500" : "bg-amber-400"}`} />
          <span className="text-slate-200">{included ? "Inférence gérée — Qwen3 Coder (Francfort UE), prépayée"
            : st.byok_kind === "api_key" ? "Votre clé API Anthropic"
            : st.byok_kind === "subscription" ? "Votre abonnement Claude Pro/Max"
            : st.mode === "env" ? "Clé configurée (environnement)" : "Aucun modèle configuré"}</span>
        </div>
        {included && use && (
          <div className="mt-2.5 space-y-2">
            <div className="flex items-baseline justify-between">
              <span className="text-[11px] text-mut">Crédits d'inférence</span>
              <span className={`text-[15px] font-semibold ${(use.balance_centimes ?? 0) > 500 ? "text-emerald-400" : "text-amber-300"}`}>
                {((use.balance_centimes ?? 0) / 100).toFixed(2)} CHF
              </span>
            </div>
            {isAdmin && (
              <div className="flex gap-1.5">
                {[25, 100, 500].map((p) => (
                  <button key={p} onClick={() => llmCredit(p).then((r) => window.open(r.checkout_url, "_blank")).catch(() => {})}
                    className="flex-1 rounded border border-sea/40 bg-sea/10 px-2 py-1 text-[11px] text-sea hover:border-sea">
                    +{p} CHF{p >= 500 ? " (+25%)" : ""}
                  </button>
                ))}
              </div>
            )}
            <div>
              <div className="flex justify-between text-[11px] text-mut"><span>Aujourd'hui (plafond de protection)</span>
                <span className="text-slate-300">{fmt(use.used_today)} / {use.daily_quota_tokens ? fmt(use.daily_quota_tokens) : "∞"} tokens</span></div>
              <div className="mt-1"><Bar used={use.used_today} quota={use.daily_quota_tokens} /></div>
            </div>
            {!!use.per_user?.length && (
              <div>
                <div className="text-[11px] text-mut">Usage du mois par utilisateur</div>
                {use.per_user.map((u2) => (
                  <div key={u2.user} className="flex justify-between text-[11px]">
                    <span className="text-slate-300">{u2.user || "(non attribué)"}</span>
                    <span className="text-mut">{fmt(u2.input_tokens)} in · {fmt(u2.output_tokens)} out</span>
                  </div>
                ))}
              </div>
            )}
            <div className="text-[10px] text-mut">
              Décompté au token (tarif affiché dès 4 CHF/Mtok in · 20 CHF/Mtok out, paliers de contexte).
              Solde épuisé = requêtes refusées, rien n'est facturé au-delà.
            </div>
          </div>
        )}
      </div>
      {included ? (
        <div className="text-[12px] text-mut">
          Inférence gérée : modèles <b className="text-slate-300">Qwen3 Coder</b>, servis depuis
          Francfort (UE), prépayée par crédits. Pour passer en clé personnelle (BYOK), contactez-nous.
        </div>
      ) : !isAdmin ? (
        <div className="text-[12px] text-mut">La configuration du modèle est réservée aux administrateurs.</div>
      ) : (
        <>
          <div className="text-[11px] text-mut">Comment vos sessions accèdent au modèle :</div>
          <button onClick={() => { setChoice("api"); setVal(""); }} className={`block w-full rounded-lg border p-3 text-left ${choice === "api" ? "border-sea bg-sea/10" : "border-line hover:border-line/80"}`}>
            <div className="text-[13px] text-slate-100">Clé API Anthropic <span className="text-mut">(BYOK)</span></div>
            <div className="text-[11px] text-mut">Sessions vers Anthropic en direct, facturé sur votre compte, aucune limite. La clé reste sur votre VM.</div></button>
          <button onClick={() => { setChoice("sub"); setVal(""); }} className={`block w-full rounded-lg border p-3 text-left ${choice === "sub" ? "border-sea bg-sea/10" : "border-line hover:border-line/80"}`}>
            <div className="text-[13px] text-slate-100">Abonnement Claude Pro / Max</div>
            <div className="text-[11px] text-mut">Sur votre poste : <code className="text-slate-300">claude setup-token</code> → collez le jeton. Utilise votre abonnement.</div></button>
          <div className="rounded-lg border border-line/60 bg-panel2/30 p-3 opacity-70">
            <div className="text-[13px] text-slate-300">Inférence gérée (Qwen3 Coder · Francfort UE)</div>
            <div className="text-[11px] text-mut">Prépayée par crédits, décomptée au token. Se choisit à la souscription.</div></div>
          {choice && (
            <div className="rounded-lg border border-line bg-panel2/40 p-3">
              <input value={val} onChange={(e) => setVal(e.target.value)} type="password"
                placeholder={choice === "api" ? "sk-ant-…" : "jeton claude setup-token…"}
                className="w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
              <div className="mt-2 flex items-center gap-2">
                <button disabled={busy} onClick={save} className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-50">enregistrer</button>
                <button onClick={() => { setChoice(null); setVal(""); }} className="text-[12px] text-mut hover:text-slate-200">annuler</button>
                <span className="ml-auto text-[10px] text-mut">reste sur votre VM, jamais transmis</span></div>
              {err && <div className="mt-1 text-[11px] text-red-400">{err}</div>}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function Profile({ onClose }: { onClose: () => void }) {
  const [sec, setSec] = useState<Section>("account");
  const nav: [Section, string][] = [["account", "Mon compte"], ["org", "Organisation"], ["members", "Membres"], ["model", "Modèle"]];
  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/60 p-4 pt-14" onClick={onClose}>
      <div className="flex w-full max-w-2xl overflow-hidden rounded-2xl border border-line bg-panel shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="w-44 shrink-0 border-r border-line bg-panel2/40 p-2">
          <div className="px-2 py-1.5 text-[13px] font-semibold text-slate-100">Profil & organisation</div>
          {nav.map(([k, label]) => (
            <button key={k} onClick={() => setSec(k)}
              className={`block w-full rounded-md px-2 py-1.5 text-left text-[12.5px] ${sec === k ? "bg-panel text-slate-100" : "text-mut hover:text-slate-200"}`}>{label}</button>
          ))}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center border-b border-line px-4 py-2.5">
            <span className="text-[13.5px] font-medium text-slate-100">{nav.find(([k]) => k === sec)?.[1]}</span>
            <button onClick={onClose} className="ml-auto text-mut hover:text-slate-200">✕</button>
          </div>
          <div className="max-h-[72vh] overflow-y-auto p-4">
            {sec === "account" ? <Account /> : sec === "org" ? <Org /> : sec === "members" ? <Members /> : <Model />}
          </div>
        </div>
      </div>
    </div>
  );
}
