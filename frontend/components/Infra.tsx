"use client";
import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import {
  cloudEnvDestroy, cloudEnvs, cloudEnvSpawn, fleetGrants, fleetGrantsSet, fleetRemove, fleetRequest, fleetRouteAdd, fleetRouteRemove, fleetView, infraNodes, infraTargets,
} from "@/lib/api";

const FleetTerm = dynamic(() => import("./FleetTerm"), { ssr: false });
import type { FleetProduct, FleetResource, FleetView } from "@/lib/api";
import type { CloudEnv, InfraNode, InfraTarget } from "@/lib/types";
import { useFeatures } from "@/lib/features";
import { useCan } from "@/lib/me";

const gb = (b: number | null) => (b ? (b / 1e9).toFixed(b > 1e11 ? 0 : 1) : "—");
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

// connection string DB (admin only — le backend la retire pour les autres rôles)
function DbUri({ uri }: { uri: string }) {
  const [shown, setShown] = useState(false);
  const [copied, setCopied] = useState(false);
  return (
    <div className="mt-1.5 flex items-center gap-2 pl-6">
      <code className="min-w-0 flex-1 truncate rounded bg-black/40 px-1.5 py-0.5 font-mono text-[10.5px] text-slate-300">
        {shown ? uri : "postgres://••••••••••••••••••••••••••••"}
      </code>
      <button onClick={() => setShown(!shown)}
        className="rounded border border-line px-1.5 py-px text-[10.5px] text-mut hover:text-slate-200">
        {shown ? "masquer" : "révéler"}
      </button>
      <button onClick={() => { navigator.clipboard?.writeText(uri); setCopied(true); setTimeout(() => setCopied(false), 1500); }}
        className="rounded border border-line px-1.5 py-px text-[10.5px] text-mut hover:text-slate-200">
        {copied ? "✓ copié" : "copier"}
      </button>
    </div>
  );
}

const RES_STATUS: Record<string, string> = {
  pending: "text-amber-300", provisioning: "text-sky-300",
  live: "text-emerald-400", failed: "text-red-400",
  removing: "text-amber-300", destroyed: "text-mut",
};
const RES_LABEL: Record<string, string> = {
  pending: "en attente de paiement", provisioning: "création en cours",
  live: "en service", failed: "échec",
  removing: "résiliation en cours", destroyed: "résiliée",
};
const INFRA_LABEL: Record<string, string> = {
  planned: "planifié", provisioning: "création en cours", running: "en service",
};
const CAT_LABEL: Record<string, string> = {
  plan: "Cockpit", compute: "Instance", database: "Base de données",
};

// « Ma flotte » — vue CLIENT (managé). Le cockpit interroge le portail NINABOT
// avec son fleet token : il voit ses ressources et en demande de nouvelles
// (facturées au prorata, provisionnées après paiement). Aucun credential cloud ici.
// grants du terminal de maintenance (admin only) : qui, en dehors des admins,
// peut ouvrir une session root sur les machines de la flotte.
function TermGrants() {
  const [g, setG] = useState<string[]>([]);
  const [input, setInput] = useState("");
  useEffect(() => { fleetGrants().then((r) => setG(r.grants)).catch(() => {}); }, []);
  const save = (emails: string[]) => fleetGrantsSet(emails).then((r) => setG(r.grants)).catch(() => {});
  return (
    <div className="mt-4 max-w-3xl rounded-lg border border-line bg-panel2/40 p-2.5">
      <div className="text-[11.5px] font-semibold text-slate-200">Accès terminal de maintenance</div>
      <div className="mt-0.5 text-[10.5px] text-mut">
        Session <span className="text-amber-300">root</span> sur les machines de la flotte — admins d'office ;
        ajoutez ici un user à autoriser explicitement. Chaque ouverture est auditée.
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {g.map((e) => (
          <span key={e} className="flex items-center gap-1 rounded border border-line px-1.5 py-px text-[11px] text-slate-200">
            {e}<button onClick={() => save(g.filter((x) => x !== e))} className="text-mut hover:text-red-400">✕</button>
          </span>
        ))}
        <input value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && input.includes("@")) { save([...g, input]); setInput(""); } }}
          placeholder="email + Entrée"
          className="w-44 rounded border border-line bg-[#0b0f16] px-2 py-0.5 text-[11px] text-slate-100 outline-none focus:border-sea/50" />
      </div>
    </div>
  );
}

// Exposition web de la flotte : sous-domaines <name>-<tenant>.sokkan.ch (tunnel,
// TLS Cloudflare) et domaines custom du client (CNAME → edge, TLS Let's Encrypt
// on-demand émis par le caddy de la VM). Gratuit — mutations admin.
function FleetRoutes({ view, reload, isAdmin }: { view: FleetView; reload: () => void; isAdmin: boolean }) {
  const [kind, setKind] = useState<"subdomain" | "custom">("subdomain");
  const [name, setName] = useState("");
  const [hostname, setHostname] = useState("");
  const [target, setTarget] = useState("cockpit");
  const [port, setPort] = useState("80");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const routes = view.routes ?? [];
  const targets = ["cockpit", ...view.resources
    .filter((r) => r.status === "live" && r.fleet_host && !r.uri)
    .map((r) => r.fleet_host!.replace(/\.fleet$/, ""))];

  const add = () => {
    setErr(""); setMsg(""); setBusy(true);
    fleetRouteAdd(kind, name.trim(), hostname.trim(), target, parseInt(port, 10) || 80)
      .then((r) => {
        setMsg(kind === "custom"
          ? `Route « ${r.hostname} » créée — pointez un CNAME vers ${r.edge_host} ; le certificat TLS est émis au premier accès.`
          : `Route « ${r.hostname} » active dans ~1 minute.`);
        setName(""); setHostname(""); reload();
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setBusy(false));
  };

  return (
    <div className="mb-4 max-w-3xl">
      <div className="mb-1.5 text-[12px] font-semibold text-slate-200">Exposition web</div>
      <div className="mb-1.5 text-[10.5px] text-mut">
        Servez un service de votre flotte sur un sous-domaine{" "}
        <span className="font-mono text-slate-300">*{view.route_suffix ?? ".sokkan.ch"}</span> ou sur{" "}
        <span className="text-slate-300">votre propre domaine</span> (CNAME vers{" "}
        <span className="font-mono text-slate-300">{view.edge_host ?? "votre edge"}</span>, certificat automatique).
        HTTP(S) uniquement ; la cible doit écouter sur l'IP privée de la machine (0.0.0.0).
      </div>
      <div className="space-y-1.5">
        {routes.map((r) => (
          <div key={r.id} className="flex items-center gap-2.5 rounded-lg border border-line bg-panel2/50 p-2 text-[12px]">
            <span className="text-emerald-400">●</span>
            <a href={`https://${r.hostname}`} target="_blank" rel="noreferrer"
              className="min-w-0 truncate font-mono text-[11.5px] text-sea hover:underline">{r.hostname}</a>
            <span className="text-mut">→</span>
            <span className="font-mono text-[11px] text-slate-200">{r.target}:{r.port}</span>
            <span className="rounded border border-line px-1.5 py-px text-[10px] text-mut">
              {r.kind === "custom" ? "votre domaine" : "sokkan.ch"}
            </span>
            {isAdmin && (
              <button title="supprimer la route (le service n'est plus exposé)"
                onClick={() => {
                  if (confirm(`Supprimer la route ${r.hostname} ? Le service ne sera plus exposé.`))
                    fleetRouteRemove(r.id).then(reload).catch((e) => setErr(String(e)));
                }}
                className="ml-auto rounded px-1 text-mut hover:text-red-400">✕</button>
            )}
          </div>
        ))}
        {routes.length === 0 && <div className="text-[12px] text-mut">Aucune route — vos services ne sont accessibles que depuis votre réseau privé.</div>}
      </div>
      {isAdmin && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select value={kind} onChange={(e) => setKind(e.target.value as "subdomain" | "custom")}
            className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
            <option value="subdomain">sous-domaine sokkan.ch</option>
            <option value="custom">votre domaine</option>
          </select>
          {kind === "subdomain" ? (
            <span className="flex items-center rounded border border-line bg-[#0b0f16] pr-2">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="app"
                className="w-24 bg-transparent px-2 py-1 text-[12px] text-slate-100 outline-none" />
              <span className="font-mono text-[11px] text-mut">{view.route_suffix ?? ""}</span>
            </span>
          ) : (
            <input value={hostname} onChange={(e) => setHostname(e.target.value)} placeholder="app.mondomaine.com"
              className="w-52 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
          )}
          <span className="text-[11px] text-mut">→</span>
          <select value={target} onChange={(e) => setTarget(e.target.value)}
            className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
            {targets.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <input value={port} onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))} placeholder="port"
            className="w-16 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
          <button onClick={add} disabled={busy || (kind === "subdomain" ? !name.trim() : !hostname.trim())}
            className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-40">
            {busy ? "création…" : "＋ exposer"}
          </button>
        </div>
      )}
      {kind === "custom" && isAdmin && (
        <div className="mt-1.5 text-[10.5px] text-mut">
          Chez votre registrar : <span className="font-mono text-slate-300">CNAME {hostname.trim() || "app.mondomaine.com"} → {view.edge_host}</span>.
          Le certificat est émis automatiquement au premier accès (comptez ~1 min après la propagation DNS).
        </div>
      )}
      {msg && <div className="mt-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-2 text-[11.5px] text-emerald-200">{msg}</div>}
      {err && <div className="mt-1.5 text-[11px] text-red-400">{err}</div>}
    </div>
  );
}

function Fleet() {
  const isAdmin = useCan("admin");
  const [view, setView] = useState<FleetView | null | undefined>(undefined); // undefined=chargement, null=indispo
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [term, setTerm] = useState(""); // nom de la ressource ouverte au terminal

  const reload = () => fleetView().then(setView).catch(() => setView(null));
  useEffect(() => { reload(); const iv = setInterval(reload, 8000); return () => clearInterval(iv); }, []);

  if (view === undefined) return <div className="p-4 text-[12px] text-mut">Chargement de la flotte…</div>;
  if (view === null)
    return (
      <div className="p-4 text-[12px] text-mut">
        Gestion de flotte indisponible sur cette instance (self-hosted, ou SOKKAN_FLEET_* absent).
      </div>
    );

  const order = (p: FleetProduct) => {
    if (!isAdmin) { setErr("rôle admin requis pour commander"); return; }
    const name = p.category === "plan" ? "" :
      (prompt(`Nom pour « ${p.label} » (optionnel) :`) ?? "").trim();
    const msg = p.category === "plan"
      ? `Passer sur « ${p.label} » — ${p.price_chf} CHF/mois, prorata facturé/crédité immédiatement, courte interruption au redimensionnement. Confirmer ?`
      : `Commander « ${p.label} » — ${p.price_chf} CHF/mois, facturé au prorata immédiatement. Confirmer ?`;
    if (!confirm(msg)) return;
    setErr(""); setMsg(""); setBusy(p.sku);
    fleetRequest(p.sku, name)
      .then((r) => { setMsg(`« ${p.label} » commandé — facture ${r.invoice ?? "en cours"}, provisioning au paiement.`); reload(); })
      .catch((e) => setErr(String(e)))
      .finally(() => setBusy(""));
  };

  const catByCat: Record<string, FleetProduct[]> = {};
  for (const p of view.catalog) (catByCat[p.category] ??= []).push(p);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-3">
      <div className="mb-3 text-[10.5px] text-mut">
        ⓘ Vos ressources vivent dans <span className="text-slate-300">votre réseau privé</span> (compute + bases).
        Toute commande est facturée au prorata sur votre abonnement et provisionnée après paiement — opéré par NINABOT.
      </div>

      {msg && <div className="mb-2.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-2 text-[11.5px] text-emerald-200">{msg}</div>}
      {err && <div className="mb-2.5 text-[11px] text-red-400">{err}</div>}

      <div className="mb-4 max-w-3xl">
        <div className="mb-1.5 text-[12px] font-semibold text-slate-200">
          Ressources actives <span className="text-mut">· plan {view.plan ?? "—"}{view.infra_status ? ` · ${INFRA_LABEL[view.infra_status] ?? view.infra_status}` : ""}</span>
        </div>
        <div className="space-y-1.5">
          {view.resources.map((r: FleetResource) => (
            <div key={r.id} className="rounded-lg border border-line bg-panel2/50 p-2 text-[12px]">
              <div className="flex items-center gap-2.5">
                <span className={`${RES_STATUS[r.status] ?? "text-slate-200"}`}>●</span>
                <span className="font-medium text-slate-100">{r.name || r.sku}</span>
                <span className="rounded border border-line px-1.5 py-px text-[10px] text-mut">{r.sku}</span>
                {r.fleet_host && (
                  <button title={r.private_ip ? `copier (${r.private_ip})` : "copier"}
                    onClick={() => navigator.clipboard?.writeText(r.fleet_host!)}
                    className="rounded border border-sea/40 bg-sea/10 px-1.5 py-px font-mono text-[10.5px] text-sea hover:border-sea">
                    {r.fleet_host}
                  </button>
                )}
                {view.can_term && r.status === "live" && r.fleet_host && !r.uri && (
                  <button onClick={() => setTerm(r.fleet_host!.replace(/\.fleet$/, ""))}
                    title="terminal de maintenance (root, audité)"
                    className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-px text-[10.5px] text-amber-300 hover:border-amber-400">
                    ⌨ maintenance
                  </button>
                )}
                <span className={`ml-auto text-[11px] ${RES_STATUS[r.status] ?? "text-slate-300"}`}>{RES_LABEL[r.status] ?? r.status}</span>
                {isAdmin && !r.sku.startsWith("plan-") && ["live", "provisioning", "pending"].includes(r.status) && (
                  <button title="résilier (crédit du prorata, données détruites)"
                    onClick={() => {
                      const nm = r.name || r.sku;
                      if (prompt(`Résiliation DÉFINITIVE de « ${nm} » — le prorata restant est crédité, les données de la ressource sont DÉTRUITES.\nRetape son nom pour confirmer :`) === nm)
                        fleetRemove(r.id).then(() => { setMsg(`« ${nm} » en cours de résiliation.`); reload(); }).catch((e) => setErr(String(e)));
                    }}
                    className="rounded px-1 text-mut hover:text-red-400">✕</button>
                )}
              </div>
              {r.uri && <DbUri uri={r.uri} />}
            </div>
          ))}
          {view.resources.length === 0 && <div className="text-[12px] text-mut">Aucune ressource additionnelle — seulement votre cockpit.</div>}
        </div>
        <div className="mt-2 text-[10.5px] text-mut">
          Depuis vos sessions, chaque ressource répond sur <span className="font-mono text-slate-300">&lt;nom&gt;.fleet</span>
          {" "}(le cockpit est <span className="font-mono text-slate-300">cockpit.fleet</span>{view.cockpit_ip ? ` · ${view.cockpit_ip}` : ""}).
        </div>
        {isAdmin && <TermGrants />}
        {term && <FleetTerm name={term} onClose={() => setTerm("")} />}
      </div>

      <FleetRoutes view={view} reload={reload} isAdmin={isAdmin} />

      <div className="mb-1.5 text-[12px] font-semibold text-slate-200">Ajouter une ressource</div>
      <div className="max-w-3xl space-y-3">
        {["compute", "database", "plan"].filter((c) => catByCat[c]?.length).map((cat) => (
          <div key={cat}>
            <div className="mb-1 text-[10.5px] uppercase tracking-wide text-mut">{CAT_LABEL[cat] ?? cat}</div>
            <div className="grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(240px,1fr))]">
              {catByCat[cat].map((p) => {
                const isCurrent = p.sku === `plan-${view.plan}`;
                return (
                  <div key={p.sku} className={`rounded-xl border p-2.5 ${isCurrent ? "border-sea/40 bg-sea/5" : "border-line bg-panel"}`}>
                    <div className="flex items-baseline gap-2">
                      <span className="min-w-0 flex-1 text-[13px] font-semibold leading-snug text-slate-100">{p.label}</span>
                      <span className="shrink-0 whitespace-nowrap text-[12px] font-medium text-sea">{p.price_chf} CHF<span className="text-[10px] text-mut">/mois</span></span>
                    </div>
                    <div className="mt-0.5 text-[10.5px] text-mut">{p.desc}</div>
                    <button disabled={!isAdmin || busy === p.sku || isCurrent} onClick={() => order(p)}
                      className="mt-2 w-full rounded bg-sea/80 px-2 py-1 text-[11.5px] font-medium text-white hover:bg-sea disabled:opacity-40">
                      {isCurrent ? "votre plan actuel" : busy === p.sku ? "commande…" : !isAdmin ? "admin requis"
                        : p.category === "plan" ? "⇅ changer de plan" : "＋ commander"}
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

type Mode = "topo" | "fleet" | "envs";

export default function Infra() {
  // Accès (IAM) migré dans Profil & organisation → Membres.
  const feats = useFeatures();
  const isAdmin = useCan("admin");
  // VM client managée : pas de Prometheus → pas de Topologie, la flotte est l'onglet par défaut.
  const tabs: Mode[] = [
    ...(feats.infra_topo ? ["topo" as const] : []),
    ...(feats.fleet ? ["fleet" as const] : []),
    ...(isAdmin && feats.infra_topo ? ["envs" as const] : []),
  ];
  const [mode, setMode] = useState<Mode | null>(null);
  const cur = mode && tabs.includes(mode) ? mode : tabs[0];
  if (!cur) return <div className="p-4 text-[12px] text-mut">Rien à afficher (ni topologie, ni flotte).</div>;

  const LABEL: Record<Mode, string> = { topo: "Topologie", fleet: "Ma flotte", envs: "Environnements" };
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-line bg-panel/60 px-3 py-1.5">
        <div className="flex overflow-hidden rounded-md border border-line text-[12px]">
          {tabs.map((m) => (
            <button key={m} onClick={() => setMode(m)}
              className={`px-3 py-0.5 ${cur === m ? "bg-panel2 text-slate-100" : "text-mut hover:text-slate-200"}`}>
              {LABEL[m]}
            </button>
          ))}
        </div>
      </div>
      {cur === "topo" ? <Topo /> : cur === "fleet" ? <Fleet /> : <Envs />}
    </div>
  );
}
