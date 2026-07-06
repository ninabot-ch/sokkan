"use client";
import { useEffect, useState } from "react";
import {
  cloudEnvDestroy, cloudEnvs, cloudEnvSpawn,
  iamDelete, iamUpsert, iamUsers, infraNodes, infraTargets,
} from "@/lib/api";
import type { CloudEnv, IamUser, InfraNode, InfraTarget } from "@/lib/types";
import { useMe, useCan } from "@/lib/me";

const gb = (b: number | null) => (b ? (b / 1e9).toFixed(b > 1e11 ? 0 : 1) : "—");
const ROLES = ["viewer", "dev", "admin", "owner"];
function uptime(s: number | null) {
  if (!s) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return d > 0 ? `${d}j ${h}h` : `${h}h`;
}
function Bar({ pct }: { pct: number }) {
  const c = pct > 90 ? "bg-red-500" : pct > 70 ? "bg-amber-400" : "bg-emerald-500";
  return <div className="h-1.5 w-full overflow-hidden rounded-full bg-line"><div className={`h-full ${c}`} style={{ width: `${Math.min(100, Math.max(2, pct))}%` }} /></div>;
}
function Metric({ label, value, pct }: { label: string; value: string; pct?: number }) {
  return (
    <div>
      <div className="flex justify-between text-[10.5px] text-mut"><span>{label}</span><span className="text-slate-300">{value}</span></div>
      {pct != null && <div className="mt-0.5"><Bar pct={pct} /></div>}
    </div>
  );
}

function Topo() {
  const [nodes, setNodes] = useState<InfraNode[]>([]);
  const [targets, setTargets] = useState<InfraTarget[]>([]);
  useEffect(() => {
    const load = () => { infraNodes().then(setNodes).catch(() => {}); infraTargets().then(setTargets).catch(() => {}); };
    load(); const iv = setInterval(load, 5000); return () => clearInterval(iv);
  }, []);
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-3">
      <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(280px,1fr))]">
        {nodes.map((n) => {
          const memPct = n.mem_total && n.mem_avail != null ? (1 - n.mem_avail / n.mem_total) * 100 : null;
          const diskPct = n.disk_total && n.disk_avail != null ? (1 - n.disk_avail / n.disk_total) * 100 : null;
          return (
            <div key={n.ip} className={`rounded-xl border p-3 ${n.up ? "border-line bg-panel" : "border-line/60 bg-panel/40"}`}>
              <div className="flex items-center gap-2">
                <span className={`h-2.5 w-2.5 rounded-full ${n.up ? "bg-emerald-500" : n.up === false ? "bg-red-500" : "bg-slate-600"}`} />
                <span className="text-[14px] font-semibold text-slate-100">{n.name}</span>
                <span className="ml-auto text-[10px] text-mut">{n.ip}</span>
              </div>
              <div className="mt-0.5 text-[10.5px] text-mut">{n.role}</div>
              {n.monitored && n.up ? (
                <div className="mt-2.5 space-y-2">
                  <Metric label={`CPU · ${n.cores} cœurs`} value={`${n.cpu_pct ?? "—"}%`} pct={n.cpu_pct ?? 0} />
                  <Metric label="RAM" value={`${gb(n.mem_avail)} / ${gb(n.mem_total)} GB libres`} pct={memPct ?? 0} />
                  <Metric label="disque /" value={`${gb(n.disk_avail)} / ${gb(n.disk_total)} GB libres`} pct={diskPct ?? 0} />
                  <div className="flex justify-between text-[10.5px] text-mut"><span>load {n.load1?.toFixed(2)}</span><span>uptime {uptime(n.uptime_s)}</span></div>
                </div>
              ) : <div className="mt-3 text-[11px] text-mut">{n.up === false ? "hors-ligne (target down)" : "non monitoré"}</div>}
            </div>
          );
        })}
      </div>
      <div className="mt-5">
        <div className="mb-2 text-[12px] font-semibold text-slate-200">Targets Prometheus</div>
        <div className="grid gap-1.5 [grid-template-columns:repeat(auto-fill,minmax(240px,1fr))]">
          {targets.map((t, i) => (
            <div key={i} className="flex items-center gap-2 rounded-lg border border-line bg-panel2/40 px-2 py-1 text-[11.5px]">
              <span className={`h-2 w-2 shrink-0 rounded-full ${t.up ? "bg-emerald-500" : "bg-red-500"}`} />
              <span className="text-slate-200">{t.job}</span><span className="ml-auto truncate text-mut">{t.instance}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Access() {
  const me = useMe();
  const isAdmin = useCan("admin");
  const [users, setUsers] = useState<IamUser[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("dev");
  const reload = () => iamUsers().then(setUsers).catch(() => setUsers([]));
  useEffect(() => { if (isAdmin) reload(); }, [isAdmin]);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-3">
      <div className="mb-3 rounded-lg border border-line bg-panel2/40 p-2.5 text-[12px]">
        Connecté en tant que <span className="text-slate-100">{me?.name}</span> ·
        rôle <span className="text-brass">{me?.role}</span>
        <span className="ml-2 text-[10.5px] text-mut">(via Authentik / CF Access · {me?.source})</span>
      </div>

      {!isAdmin ? (
        <div className="text-[12px] text-mut">Gestion des accès réservée aux rôles admin/owner.</div>
      ) : (
        <div className="max-w-2xl space-y-2">
          <div className="text-[12px] font-semibold text-slate-200">Utilisateurs SOKKAN</div>
          <div className="text-[10.5px] text-mut">
            ⓘ Les rôles sont internes à SOKKAN. Pour qu'un nouvel email puisse atteindre SOKKAN, l'accès CF doit
            être ouvert séparément (par Claude Code — SOKKAN n'écrit pas sur Cloudflare).
          </div>
          {users.map((u) => (
            <div key={u.email} className="flex items-center gap-2 rounded-lg border border-line bg-panel2/50 p-2">
              <div className="min-w-0">
                <div className="truncate text-[12.5px] text-slate-100">{u.name}</div>
                <div className="truncate text-[10.5px] text-mut">{u.email}</div>
              </div>
              <select value={u.role} disabled={u.role === "owner"}
                onChange={(e) => iamUpsert(u.email, e.target.value, u.name).then(reload)}
                className="ml-auto rounded border border-line bg-panel2 px-1.5 py-0.5 text-[11.5px] text-slate-200 disabled:opacity-50">
                {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
              <button disabled={u.role === "owner"} onClick={() => iamDelete(u.email).then(reload)}
                className="rounded px-1 text-mut hover:text-red-400 disabled:opacity-30" title="retirer">✕</button>
            </div>
          ))}
          <div className="flex items-center gap-2 pt-1">
            <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email@…"
              className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
            <select value={role} onChange={(e) => setRole(e.target.value)}
              className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            <button onClick={() => { if (email.trim()) iamUpsert(email.trim(), role).then(() => { setEmail(""); reload(); }); }}
              className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">+ user</button>
          </div>
        </div>
      )}
    </div>
  );
}

const ENV_STATUS: Record<string, string> = {
  provisioning: "text-amber-300", "apply…": "text-amber-300", planned: "text-sky-300",
  running: "text-emerald-400", failed: "text-red-400", "destroy-failed": "text-red-400",
  destroying: "text-amber-300", destroyed: "text-mut",
};
const TIERS = [
  { id: "starter", label: "Starter — 2c/4Go (solo)" },
  { id: "standard", label: "Standard — 4c/8Go" },
  { id: "studio", label: "Studio — 4c/16Go (agence)" },
];

function Envs() {
  const isOwner = useCan("owner");
  const [envs, setEnvs] = useState<CloudEnv[] | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [client, setClient] = useState("");
  const [tier, setTier] = useState("starter");
  const [email, setEmail] = useState("");
  const [token, setToken] = useState<CloudEnv | null>(null); // réponse de spawn (token montré UNE fois)
  const [err, setErr] = useState("");

  const reload = () =>
    cloudEnvs().then((e) => { setEnvs(e); setEnabled(true); })
      .catch((e) => { if (String(e).includes("404")) setEnabled(false); });
  useEffect(() => { reload(); const iv = setInterval(reload, 8000); return () => clearInterval(iv); }, []);

  if (!enabled)
    return (
      <div className="p-4 text-[12px] text-mut">
        Provisioning non configuré sur cette instance (SOKKAN_PROVISIONER_URL absent).
        Les environnements cloud sont un service opéré NINABOT — le connecteur reste
        auditable ici : <span className="text-slate-300">backend/provision.py</span>.
      </div>
    );

  const doSpawn = () => {
    setErr("");
    if (!client.trim() || !email.trim()) { setErr("client + email requis"); return; }
    cloudEnvSpawn(client.trim().toLowerCase(), tier, email.trim())
      .then((r) => { setToken(r); setClient(""); reload(); })
      .catch((e) => setErr(String(e)));
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-3">
      <div className="mb-3 text-[10.5px] text-mut">
        ⓘ 1 client = 1 VM Exoscale isolée (zone CH). Spawn = rôle admin ; destroy = owner.
        Exécution déterministe (Terraform) côté control plane — cette instance n'a aucun credential cloud.
      </div>

      {token && (
        <div className="mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-2.5 text-[12px]">
          <div className="font-semibold text-amber-200">Environnement « {token.client} » en création</div>
          <div className="mt-1 text-slate-200">Token de login à transmettre au client (affiché une seule fois) :</div>
          <code className="mt-1 block select-all break-all rounded bg-black/40 p-1.5 text-[11px] text-amber-100">{token.local_token}</code>
          <div className="mt-1 text-[10.5px] text-mut">{token.public_url}</div>
          <button onClick={() => setToken(null)} className="mt-1.5 rounded border border-line px-2 py-0.5 text-[11px] text-mut hover:text-slate-200">j'ai copié, fermer</button>
        </div>
      )}

      <div className="max-w-3xl space-y-1.5">
        {(envs ?? []).map((e) => (
          <div key={e.client} className="flex items-center gap-2.5 rounded-lg border border-line bg-panel2/50 p-2 text-[12px]">
            <span className={`font-semibold ${ENV_STATUS[e.status] ?? "text-slate-200"}`}>●</span>
            <span className="font-medium text-slate-100">{e.client}</span>
            <span className="rounded border border-line px-1.5 py-px text-[10px] text-mut">{e.tier}</span>
            <span className={`text-[11px] ${ENV_STATUS[e.status] ?? "text-slate-300"}`}>{e.status}</span>
            <a href={e.public_url} target="_blank" rel="noreferrer" className="truncate text-[11px] text-sea hover:underline">{e.public_url}</a>
            <span className="ml-auto text-[10px] text-mut">{e.owner_email}</span>
            {isOwner && !["destroyed", "destroying"].includes(e.status) && (
              <button
                onClick={() => {
                  if (prompt(`Destruction DÉFINITIVE de « ${e.client} » (VM + données).\nRetape le nom pour confirmer :`) === e.client)
                    cloudEnvDestroy(e.client).then(reload).catch((x) => setErr(String(x)));
                }}
                className="rounded px-1 text-mut hover:text-red-400" title="détruire">✕</button>
            )}
          </div>
        ))}
        {envs !== null && envs.length === 0 && <div className="text-[12px] text-mut">Aucun environnement.</div>}

        <div className="flex items-center gap-2 pt-2">
          <input value={client} onChange={(e) => setClient(e.target.value)} placeholder="client (slug: acme)"
            className="w-40 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
          <select value={tier} onChange={(e) => setTier(e.target.value)}
            className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
            {TIERS.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
          </select>
          <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email owner client"
            className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
          <button onClick={doSpawn}
            className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">▶ spawn</button>
        </div>
        {err && <div className="text-[11px] text-red-400">{err}</div>}
      </div>
    </div>
  );
}

export default function Infra() {
  const [mode, setMode] = useState<"topo" | "envs" | "access">("topo");
  const isAdmin = useCan("admin");
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-line bg-panel/60 px-3 py-1.5">
        <div className="flex overflow-hidden rounded-md border border-line text-[12px]">
          {(["topo", ...(isAdmin ? ["envs"] : []), "access"] as ("topo" | "envs" | "access")[]).map((m) => (
            <button key={m} onClick={() => setMode(m)}
              className={`px-3 py-0.5 ${mode === m ? "bg-panel2 text-slate-100" : "text-mut hover:text-slate-200"}`}>
              {m === "topo" ? "Topologie" : m === "envs" ? "Environnements" : "Accès (IAM)"}
            </button>
          ))}
        </div>
      </div>
      {mode === "topo" ? <Topo /> : mode === "envs" ? <Envs /> : <Access />}
    </div>
  );
}
