"use client";
import { useEffect, useState } from "react";
import { useMe, useCan } from "@/lib/me";
import {
  instanceBudgets, instanceInfo, instanceRename, iamUsers, iamUpsert, iamDelete,
  adminRevocation, adminRevokeNow, adminReinstate, type RevocationState,
  llmCredit, llmStatus, llmUsage, llmSetApiKey, llmSetSubscription, llmSetCustom, llmTiers, llmSetTier, type LlmTier,
  memoryStats,
  notifyStatus, notifySet, notifyTest,
  vaultList, vaultSet, vaultDelete,
  type InstanceInfo, type LlmStatus, type LlmUsage, type NotifyStatus,
} from "@/lib/api";
import type { IamUser, MemStore } from "@/lib/types";
import ProjectsAdmin from "./ProjectsAdmin";
import FeaturesAdmin from "./FeaturesAdmin";
import LinkedAccounts from "./LinkedAccounts";
import ClassificationAdmin from "./ClassificationAdmin";
import TeamsAdmin from "./TeamsAdmin";
import { useFeatures } from "@/lib/features";
import { useFeatureOn } from "@/lib/uifeatures";
import ConnectAI from "./ConnectAI";
import { DemoMembers, DemoProjects, ReadOnlyNote } from "./DemoOrganization";
import ModelKeys from "./ModelKeys";
import SecretsProvider from "./SecretsProvider";
import type { OrgSection } from "@/lib/planes";

const ROLES = ["viewer", "dev", "admin", "owner"];
const fmt = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}k` : `${n}`;

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
        <div className="mt-1.5 text-[11px]">role <span className={color[me?.role || ""] || "text-mut"}>{me?.role}</span>
          <span className="ml-2 text-mut">· login {me?.source}</span></div>
        {me?.project && <div className="mt-0.5 text-[11px] text-mut">project <span className="text-slate-300">{me.project}</span>
          {me.project_role && <> · {me.project_role}</>} · instance role {me.instance_role || "—"}</div>}
      </div>
      <a href="/api/auth/logout" className="inline-block rounded-lg border border-line px-3 py-1.5 text-[12px] text-slate-200 hover:bg-panel2">Sign out →</a>
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
        <div className="text-[11px] text-mut">Organization name</div>
        {!edit ? (
          <div className="flex items-center gap-2">
            <span className="text-[14px] text-slate-100">{inf.org_name}</span>
            {isAdmin && <button onClick={() => setEdit(true)} className="text-[11px] text-sea hover:underline">edit</button>}
          </div>
        ) : (
          <div className="mt-1 flex gap-1.5">
            <input value={name} onChange={(e) => setName(e.target.value)}
              className="flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
            <button onClick={() => instanceRename(name).then((i) => { setInf(i); setEdit(false); })}
              className="rounded bg-sea/80 px-2 py-0.5 text-[11px] text-white hover:bg-sea">ok</button>
            <button onClick={() => { setEdit(false); setName(inf.org_name); }} className="text-[11px] text-mut">cancel</button>
          </div>
        )}
      </div>
      <div className="grid grid-cols-2 gap-2">
        {inf.tier && <div className="rounded-lg border border-line bg-panel2/40 p-3">
          <div className="text-[11px] text-mut">Plan</div><div className="text-[13px] capitalize text-slate-100">{inf.tier}</div></div>}
        <div className="rounded-lg border border-line bg-panel2/40 p-3">
          <div className="text-[11px] text-mut">Address</div>
          <a href={inf.public_url} target="_blank" rel="noreferrer" className="truncate text-[12px] text-sea hover:underline">{inf.public_url || "—"}</a></div>
      </div>
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="text-[11px] text-mut">Cost budgets (estimated USD — 0 = off)</div>
        <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11.5px]">
          <label className="flex items-center gap-1.5 text-mut">per session
            <input type="number" min={0} step={1} disabled={!isAdmin} defaultValue={inf.budget_session_usd ?? 0}
              onBlur={(e) => isAdmin && instanceBudgets(parseFloat(e.target.value) || 0, inf.budget_day_usd ?? 0).then(setInf)}
              className="w-20 rounded border border-line bg-[#0b0f16] px-1.5 py-0.5 text-[12px] text-slate-100 outline-none focus:border-sea/50 disabled:opacity-50" /></label>
          <label className="flex items-center gap-1.5 text-mut">per day
            <input type="number" min={0} step={1} disabled={!isAdmin} defaultValue={inf.budget_day_usd ?? 0}
              onBlur={(e) => isAdmin && instanceBudgets(inf.budget_session_usd ?? 0, parseFloat(e.target.value) || 0).then(setInf)}
              className="w-20 rounded border border-line bg-[#0b0f16] px-1.5 py-0.5 text-[12px] text-slate-100 outline-none focus:border-sea/50 disabled:opacity-50" /></label>
        </div>
        <div className="mt-1 text-[10.5px] text-mut">
          A session warns at 80% and stops accepting new turns at its budget (raise it here to
          continue — human decision, not silent). The daily budget warns at spawn.
        </div>
      </div>
      {inf.update?.update_available && (
        <div className="rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-[12px] text-sky-200">
          <b>New version available: {inf.update.latest}</b>
          <span className="text-mut"> (installed: {inf.update.local_version})</span>
          {inf.tier ? (
            <div className="mt-1 text-[11.5px]">Managed instance: <b>Operate › Infra → My fleet</b> → "⬆ update" (admin).</div>
          ) : (
            <div className="mt-1 text-[11.5px]">
              Self-hosted — rerun the installer from the parent directory of <code>sokkan/</code>:
              <code className="mt-1 block select-all rounded bg-black/40 px-1.5 py-0.5 font-mono text-[11px] text-sky-100">curl -fsSL https://sokkan.ch/install.sh | sh</code>
              It detects the existing install and updates it (your <code>.env</code> and data are preserved).
            </div>
          )}
        </div>
      )}
      <div className="text-[10.5px] text-mut">Hosted and operated by NINABOT — sovereign Swiss infrastructure.</div>
    </div>
  );
}

// ---------- Révocation (3.2 lot 6) ----------
function RevokePanel({ st, msg, onRevoke, onReinstate }: {
  st: RevocationState | null; msg: string; onRevoke: (email: string) => void; onReinstate: (email: string) => void;
}) {
  const [who, setWho] = useState("");
  return (
    <div className="mt-3 space-y-2 rounded-lg border border-red-500/30 bg-red-500/5 p-2">
      <div className="text-[11.5px] font-medium text-red-200">Revoke access</div>
      <div className="text-[10.5px] text-mut">Also for people who come in through an SSO team and are not listed above.
        {st?.scim.enabled ? <> SCIM provisioning is on (<code className="text-slate-300">{st.scim.url}</code>).</> : <> SCIM is off (SOKKAN_SCIM_TOKEN).</>}</div>
      <div className="flex items-center gap-2">
        <input value={who} onChange={(e) => setWho(e.target.value)} placeholder="email@…"
          className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-red-400/50" />
        <button onClick={() => { if (who.trim()) onRevoke(who.trim()); }}
          className="rounded bg-red-600/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-red-600">Revoke now</button>
      </div>
      {msg && <div className="text-[11px] text-slate-300">{msg}</div>}
      {(st?.disabled ?? []).map((d) => (
        <div key={d.email} className="flex items-center gap-2 text-[11px]">
          <span className="truncate text-slate-200">{d.email}</span>
          <span className="truncate text-mut">disabled {new Date(d.disabled_at * 1000).toLocaleString()} · {d.reason}</span>
          <button onClick={() => onReinstate(d.email)} className="ml-auto rounded px-1.5 text-sea hover:underline">reinstate</button>
        </div>
      ))}
    </div>
  );
}

// ---------- Membres (IAM) ----------
function Members() {
  const isAdmin = useCan("admin");
  const [users, setUsers] = useState<IamUser[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("dev");
  // 3.2 lot 6 : « Revoke now » (feature `revocation`)
  const revocation = !!useFeatures().revocation;
  const [rev, setRev] = useState<RevocationState | null>(null);
  const [revMsg, setRevMsg] = useState("");
  const reload = () => {
    iamUsers().then(setUsers).catch(() => setUsers([]));
    if (revocation) adminRevocation().then(setRev).catch(() => setRev(null));
  };
  const revokeNow = (who: string) => {
    if (!window.confirm(`Revoke ${who} now? Their cockpit sessions end, live sessions stop, agents they own are paused, forge tokens are erased.`)) return;
    adminRevokeNow(who).then((r) => {
      setRevMsg(`${r.email}: ${r.sessions_stopped} session(s) stopped, ${r.agents_paused} agent(s) paused, ${r.forge_tokens_erased} forge token(s) erased.`);
      reload();
    }).catch((x) => setRevMsg(String(x)));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (isAdmin) reload(); }, [isAdmin, revocation]);
  if (!isAdmin) return <div className="text-[12px] text-mut">Member management is restricted to administrators.</div>;
  return (
    <div className="space-y-2">
      <div className="text-[10.5px] text-mut">Roles are internal to SOKKAN. viewer (read-only) · dev (work) · admin (manages members) · owner.</div>
      {users.map((u) => (
        <div key={u.email} className="flex items-center gap-2 rounded-lg border border-line bg-panel2/50 p-2">
          <div className="min-w-0"><div className="truncate text-[12.5px] text-slate-100">{u.name}</div>
            <div className="truncate text-[10.5px] text-mut">{u.email}</div></div>
          <select value={u.role} disabled={u.role === "owner"} onChange={(e) => iamUpsert(u.email, e.target.value, u.name).then(reload)}
            className="ml-auto rounded border border-line bg-panel2 px-1.5 py-0.5 text-[11.5px] text-slate-200 disabled:opacity-50">
            {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select>
          {revocation && u.role !== "owner" && (
            <button onClick={() => revokeNow(u.email)}
              className="rounded border border-red-500/40 px-1.5 py-0.5 text-[10.5px] text-red-300 hover:bg-red-500/10"
              title="Revoke now: sessions closed, agents paused, forge tokens erased">revoke now</button>)}
          <button disabled={u.role === "owner"} onClick={() => iamDelete(u.email).then(reload)}
            className="rounded px-1 text-mut hover:text-red-400 disabled:opacity-30" title="remove">✕</button>
        </div>
      ))}
      {revocation && <RevokePanel st={rev} msg={revMsg} onRevoke={revokeNow}
        onReinstate={(e) => adminReinstate(e).then(reload).catch((x) => setRevMsg(String(x)))} />}
      <div className="flex items-center gap-2 pt-1">
        <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email@…"
          className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
        <select value={role} onChange={(e) => setRole(e.target.value)} className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select>
        <button onClick={() => { if (email.trim()) iamUpsert(email.trim(), role).then(() => { setEmail(""); reload(); }); }}
          className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">+ member</button>
      </div>
    </div>
  );
}

// ---------- Modèle (LLM) ----------
function Model() {
  const isAdmin = useCan("admin");
  const [st, setSt] = useState<LlmStatus | null>(null);
  const [use, setUse] = useState<LlmUsage | null>(null);
  const [choice, setChoice] = useState<"api" | "sub" | "custom" | null>(null);
  const [val, setVal] = useState(""); const [busy, setBusy] = useState(false); const [err, setErr] = useState("");
  const [cUrl, setCUrl] = useState(""); const [cModel, setCModel] = useState(""); const [cSmall, setCSmall] = useState("");
  const [tiers, setTiers] = useState<LlmTier[]>([]);
  useEffect(() => { llmStatus().then(setSt).catch(() => {}); llmUsage().then(setUse).catch(() => setUse(null));
    llmTiers().then((t) => setTiers(t.tiers)).catch(() => {}); }, []);
  const pickTier = (id: string) => llmSetTier(id).then(() => llmStatus().then(setSt)).catch(() => {});
  const canSave = choice === "custom" ? !!(cUrl.trim() && val.trim() && cModel.trim()) : !!val.trim();
  const save = () => { setErr(""); if (!canSave) return; setBusy(true);
    (choice === "custom" ? llmSetCustom(cUrl.trim(), val.trim(), cModel.trim(), cSmall.trim())
      : choice === "api" ? llmSetApiKey(val.trim()) : llmSetSubscription(val.trim()))
      .then((s) => { setSt(s); setChoice(null); setVal(""); setCUrl(""); setCModel(""); setCSmall(""); })
      .catch((e) => setErr(String(e))).finally(() => setBusy(false)); };
  if (!st) return null;
  const included = st.mode === "included";
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-line bg-panel2/50 p-3 text-[12.5px]">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${st.configured ? "bg-emerald-500" : "bg-amber-400"}`} />
          <span className="text-slate-200">{included ? `Managed inference — SOKKAN ${st.model?.replace("sokkan-", "") ?? "ship"} · sovereign EU, prepaid`
            : st.byok_kind === "api_key" ? "Your Anthropic API key"
            : st.byok_kind === "subscription" ? "Your Claude Pro/Max subscription"
            : st.mode === "custom" ? `Custom endpoint — ${st.model}${st.base_url ? ` (${st.base_url.replace(/^https?:\/\//, "")})` : ""}`
            : st.mode === "env" ? "Key configured (environment)"
            : st.mode === "cli-login" ? "Claude login of the server (interactive sessions; agents only with agents_cli_login)"
            : "No model configured"}</span>
        </div>
        {included && use && (
          <div className="mt-2.5 space-y-2">
            <div className="flex items-baseline justify-between">
              <span className="text-[11px] text-mut">Inference credits</span>
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
              <div className="flex justify-between text-[11px] text-mut"><span>Today (protection cap)</span>
                <span className="text-slate-300">{fmt(use.used_today)} / {use.daily_quota_tokens ? fmt(use.daily_quota_tokens) : "∞"} tokens</span></div>
              <div className="mt-1"><Bar used={use.used_today} quota={use.daily_quota_tokens} /></div>
            </div>
            {use.spent_month_centimes != null && (
              <div className="flex justify-between text-[11px] text-mut"><span>Spent this month</span>
                <span className="text-slate-300">{((use.spent_month_centimes ?? 0) / 100).toFixed(2)} CHF
                  <span className="text-mut"> · today {((use.spent_today_centimes ?? 0) / 100).toFixed(2)}</span></span></div>
            )}
            {use.escalation_franchise_tokens != null && (
              <div title="When your tier's provider is down, requests escalate to the Deep tier. The first tokens served that way each month are billed at YOUR tier's price — this gauge keeps us honest: gratuitous escalation would cost us, not you.">
                <div className="flex justify-between text-[11px] text-mut"><span>Deep escalation — billed at your tier</span>
                  <span className="text-slate-300">{fmt(use.escalated_month_tokens ?? 0)} / {fmt(use.escalation_franchise_tokens)} tokens</span></div>
                <div className="mt-1"><Bar used={use.escalated_month_tokens ?? 0} quota={use.escalation_franchise_tokens} /></div>
              </div>
            )}
            {!!use.per_user?.length && (
              <div>
                <div className="text-[11px] text-mut">This month's usage by user</div>
                {use.per_user.map((u2) => (
                  <div key={u2.user} className="flex justify-between text-[11px]">
                    <span className="text-slate-300">{u2.user || "(unattributed)"}</span>
                    <span className="text-mut">{fmt(u2.input_tokens)} in · {fmt(u2.output_tokens)} out</span>
                  </div>
                ))}
              </div>
            )}
            <div className="text-[10px] text-mut">
              Billed per token (rates shown from 4 CHF/Mtok in · 20 CHF/Mtok out, context tiers).
              Balance exhausted = requests refused, nothing billed beyond that.
            </div>
          </div>
        )}
      </div>
      {included ? (
        <div className="space-y-2">
          {isAdmin && tiers.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-[11px] text-mut">Coding tier for your sessions</div>
              {tiers.map((t) => {
                const on = (st.model ?? "sokkan-ship") === t.id;
                return (
                  <button key={t.id} onClick={() => pickTier(t.id)}
                    className={`block w-full rounded-lg border p-2.5 text-left ${on ? "border-sea bg-sea/10" : "border-line hover:border-line/80"}`}>
                    <div className="flex items-center justify-between">
                      <span className="text-[13px] font-semibold text-slate-100">{t.label}
                        {on && <span className="ml-2 text-[10px] text-sea">● active</span>}</span>
                      <span className="text-[11px] text-mut">{t.chf_per_mtok_in.toFixed(2)} / {t.chf_per_mtok_out.toFixed(2)} CHF/M</span>
                    </div>
                    <div className="text-[11px] text-mut">{t.description}</div>
                  </button>
                );
              })}
            </div>
          )}
          <div className="text-[12px] text-mut">
            Managed inference: <b className="text-slate-300">SOKKAN Inference</b> — sovereign EU,
            served through our gateway, prepaid with credits. To switch to your own key (BYOK), contact us.
          </div>
        </div>
      ) : !isAdmin ? (
        <div className="text-[12px] text-mut">Model configuration is restricted to administrators.</div>
      ) : (
        <>
          <div className="text-[11px] text-mut">How your sessions access the model:</div>
          <button onClick={() => { setChoice("api"); setVal(""); }} className={`block w-full rounded-lg border p-3 text-left ${choice === "api" ? "border-sea bg-sea/10" : "border-line hover:border-line/80"}`}>
            <div className="text-[13px] text-slate-100">Anthropic API key <span className="text-mut">(BYOK)</span></div>
            <div className="text-[11px] text-mut">Sessions go straight to Anthropic, billed to your account, no limits. The key stays on your VM.</div></button>
          <button onClick={() => { setChoice("sub"); setVal(""); }} className={`block w-full rounded-lg border p-3 text-left ${choice === "sub" ? "border-sea bg-sea/10" : "border-line hover:border-line/80"}`}>
            <div className="text-[13px] text-slate-100">Claude Pro / Max subscription</div>
            <div className="text-[11px] text-mut">On your machine: <code className="text-slate-300">claude setup-token</code> → paste the token. Uses your subscription.</div></button>
          <button onClick={() => { setChoice("custom"); setVal(""); }} className={`block w-full rounded-lg border p-3 text-left ${choice === "custom" ? "border-sea bg-sea/10" : "border-line hover:border-line/80"}`}>
            <div className="text-[13px] text-slate-100">Other provider <span className="text-mut">(Anthropic-compatible endpoint)</span></div>
            <div className="text-[11px] text-mut">Kimi (Moonshot), GLM (Z.AI), DeepSeek, or a local proxy (LiteLLM → Ollama). Base URL + key + model.</div></button>
          <div className="rounded-lg border border-line/60 bg-panel2/30 p-3 opacity-70">
            <div className="text-[13px] text-slate-300">Managed inference (Qwen3 Coder · Frankfurt EU)</div>
            <div className="text-[11px] text-mut">Prepaid with credits, billed per token. Chosen at signup.</div></div>
          {choice && (
            <div className="rounded-lg border border-line bg-panel2/40 p-3">
              {choice === "custom" && (
                <div className="mb-2 space-y-1.5">
                  <div className="flex flex-wrap gap-1.5">
                    {([["Moonshot (Kimi)", "https://api.moonshot.ai/anthropic", "kimi-k2-0905-preview"],
                       ["Z.AI (GLM)", "https://api.z.ai/api/anthropic", "glm-4.6"],
                       ["DeepSeek", "https://api.deepseek.com/anthropic", "deepseek-chat"],
                       ["Local proxy", "http://host.docker.internal:4000", ""]] as const).map(([label, url, model]) => (
                      <button key={label} onClick={() => { setCUrl(url); setCModel(model); }}
                        className="rounded-full border border-line px-2 py-0.5 text-[10.5px] text-mut hover:border-sea/60 hover:text-slate-200">{label}</button>
                    ))}
                  </div>
                  <input value={cUrl} onChange={(e) => setCUrl(e.target.value)} placeholder="base URL — https://api.moonshot.ai/anthropic"
                    className="w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
                  <div className="flex gap-1.5">
                    <input value={cModel} onChange={(e) => setCModel(e.target.value)} placeholder="model — kimi-k2-0905-preview"
                      className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
                    <input value={cSmall} onChange={(e) => setCSmall(e.target.value)} placeholder="small/fast model (optional)"
                      className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
                  </div>
                </div>
              )}
              <input value={val} onChange={(e) => setVal(e.target.value)} type="password"
                placeholder={choice === "api" ? "sk-ant-…" : choice === "custom" ? "API key for that endpoint…" : "token from claude setup-token…"}
                className="w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
              <div className="mt-2 flex items-center gap-2">
                <button disabled={busy || !canSave} onClick={save} className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-50">save</button>
                <button onClick={() => { setChoice(null); setVal(""); }} className="text-[12px] text-mut hover:text-slate-200">cancel</button>
                <span className="ml-auto text-[10px] text-mut">stays on your VM, never transmitted</span></div>
              {choice === "custom" && (
                <div className="mt-1.5 text-[10px] text-mut">
                  Any endpoint speaking the Anthropic Messages API works — sessions still run the Claude Code engine,
                  pointed at your provider. Quality varies by model; Anthropic models remain the reference.
                </div>
              )}
              {err && <div className="mt-1 text-[11px] text-red-400">{err}</div>}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ---------- Notifications ----------
function Notifications() {
  const isAdmin = useCan("admin");
  const [st, setSt] = useState<NotifyStatus | null>(null);
  const [tgToken, setTgToken] = useState(""); const [tgChat, setTgChat] = useState("");
  const [wh, setWh] = useState(""); const [msg, setMsg] = useState(""); const [err, setErr] = useState("");
  useEffect(() => { notifyStatus().then(setSt).catch(() => {}); }, []);
  if (!st) return null;
  const saveTelegram = () => {
    setErr(""); setMsg("");
    notifySet({ telegram_bot_token: tgToken.trim(), telegram_chat_id: tgChat.trim() })
      .then(setSt).then(() => { setMsg("Telegram saved."); setTgToken(""); setTgChat(""); }).catch((e) => setErr(String(e)));
  };
  const saveWebhook = () => {
    setErr(""); setMsg("");
    notifySet({ webhook_url: wh.trim() }).then(setSt).then(() => setMsg("Webhook saved.")).catch((e) => setErr(String(e)));
  };
  const toggleHitl = (v: boolean) => notifySet({ hitl_enabled: v }).then(setSt).catch((e) => setErr(String(e)));
  const test = () => { setErr(""); setMsg(""); notifyTest().then((r) => setMsg("Sent: " + Object.entries(r.sent).map(([k, v]) => `${k} ${v}`).join(", "))).catch((e) => setErr(String(e))); };
  if (!isAdmin) return <div className="text-[12px] text-mut">Notification settings are restricted to administrators.</div>;
  return (
    <div className="space-y-3 text-[12.5px]">
      <div className="text-[10.5px] text-mut">Get pinged when a session is waiting for your approval (you launched it and walked away), and route production alerts here. Channels stay on this instance.</div>
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="flex items-center justify-between">
          <div><b>HITL push</b><div className="text-[10.5px] text-mut">Ping after a permission stays pending ~{st.hitl_delay_s}s.</div></div>
          <button onClick={() => toggleHitl(!st.hitl_enabled)}
            className={`rounded-full px-3 py-1 text-[11px] ${st.hitl_enabled ? "bg-emerald-600/30 text-emerald-200" : "bg-panel2 text-mut"}`}>
            {st.hitl_enabled ? "on" : "off"}
          </button>
        </div>
      </div>
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="mb-1.5 flex items-center gap-2"><b>Telegram</b>{st.telegram && <span className="rounded bg-emerald-600/25 px-1.5 py-px text-[10px] text-emerald-200">configured</span>}</div>
        <input value={tgToken} onChange={(e) => setTgToken(e.target.value)} type="password" placeholder="bot token (from @BotFather)"
          className="mb-1.5 w-full rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
        <div className="flex gap-1.5">
          <input value={tgChat} onChange={(e) => setTgChat(e.target.value)} placeholder="chat id"
            className="flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
          <button onClick={saveTelegram} className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">save</button>
        </div>
      </div>
      <div className="rounded-lg border border-line bg-panel2/40 p-3">
        <div className="mb-1.5 flex items-center gap-2"><b>Webhook</b>{st.webhook && <span className="rounded bg-emerald-600/25 px-1.5 py-px text-[10px] text-emerald-200">configured</span>}</div>
        <div className="flex gap-1.5">
          <input value={wh} onChange={(e) => setWh(e.target.value)} placeholder="https://your-endpoint/hook"
            className="flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
          <button onClick={saveWebhook} className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">save</button>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button onClick={test} disabled={!st.telegram && !st.webhook}
          className="rounded border border-line px-3 py-1 text-[12px] text-slate-200 hover:bg-panel2 disabled:opacity-40">send test</button>
        {msg && <span className="text-[11px] text-emerald-300">{msg}</span>}
        {err && <span className="text-[11px] text-red-400">{err}</span>}
      </div>
    </div>
  );
}

// ---------- Secrets (vault) ----------
function Secrets() {
  const isAdmin = useCan("admin");
  const [names, setNames] = useState<string[]>([]);
  const [scope, setScope] = useState<{ project?: string; enabled?: boolean }>({});
  const [name, setName] = useState(""); const [val, setVal] = useState("");
  const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (isAdmin) vaultList().then((r) => { setNames(r.names); setScope({ project: r.project, enabled: r.enabled }); }).catch(() => {});
  }, [isAdmin]);
  if (!isAdmin) return <div className="text-[12px] text-mut">Secrets are restricted to administrators.</div>;
  if (scope.enabled === false) return (
    <div className="space-y-3"><SecretsProvider /><div className="text-[12px] text-mut">
      Project <span className="font-mono text-slate-300">{scope.project}</span> has no vault: per-project vaults
      are off on this instance (feature <span className="font-mono">project_vault_budgets</span>), or this is the
      read-only <span className="font-mono">shared</span> project. Its sessions and agents receive no secret.
    </div></div>);
  const add = () => {
    setErr(""); setBusy(true);
    vaultSet(name.trim(), val).then((r) => { setNames(r.names); setName(""); setVal(""); }).catch((e) => setErr(String(e))).finally(() => setBusy(false));
  };
  return (
    <div className="space-y-3 text-[12.5px]">
      <SecretsProvider />
      <div className="text-[10.5px] text-mut">
        Secrets are encrypted at rest and injected into your sessions as environment variables
        (<span className="font-mono text-slate-300">$NAME</span>) — your agents use them to operate prod without the value
        ever showing in the UI or going to the model. They stay on this instance, or in the OpenBao / Vault it is configured with.
        {scope.project && <> Vault of project <span className="font-mono text-slate-300">{scope.project}</span>: only
          its sessions and agents can use these names.</>}
      </div>
      <div className="space-y-1.5">
        {names.map((n) => (
          <div key={n} className="flex items-center gap-2 rounded-lg border border-line bg-panel2/50 p-2">
            <span className="font-mono text-[12px] text-slate-100">{n}</span>
            <span className="font-mono text-[11px] text-mut">= ••••••••</span>
            <button onClick={() => vaultDelete(n).then((r) => setNames(r.names))}
              className="ml-auto inline-flex h-6 w-6 items-center justify-center rounded text-mut hover:bg-panel2 hover:text-red-400 focus-visible:outline focus-visible:outline-1 focus-visible:outline-sea"
              title={`delete ${n}`} aria-label={`delete secret ${n}`}>✕</button>
          </div>
        ))}
        {names.length === 0 && <div className="text-[12px] text-mut">No secrets yet.</div>}
      </div>
      <div className="flex items-center gap-1.5 pt-1">
        <input value={name} onChange={(e) => setName(e.target.value.toUpperCase())} placeholder="NAME"
          className="w-40 rounded border border-line bg-[#0b0f16] px-2 py-1 font-mono text-[12px] text-slate-100 outline-none focus:border-sea/50" />
        <input value={val} onChange={(e) => setVal(e.target.value)} type="password" placeholder="value"
          className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
        <button onClick={add} disabled={busy || !name.trim() || !val}
          className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-40">save</button>
      </div>
      {err && <div className="text-[11px] text-red-400">{err}</div>}
    </div>
  );
}

// 3.2.2 — the former « Profile & organization » dialog is now the Setup plane: these
// pages are its sub-tabs (Setup.tsx). Organization keeps an inner nav for its sections.
const ORG_NAV: [OrgSection, string, boolean][] = [
  ["org", "Organization", false], ["members", "Members", false], ["projects", "Projects & teams", true],
  ["classification", "Classification", false], ["teams", "Teams", true], ["features", "Features", true],
];

export function OrganizationPage({ section }: { section?: string }) {
  const me = useMe();
  const instAdmin = ["admin", "owner"].includes(me?.instance_role || me?.role || "");
  const feats = useFeatures();
  const featProblems = feats.registry?.problems.length ?? 0;
  // 3.2.2 Captains demo: the visitor reads the fictional members and projects
  const demo = !!feats.demo_captains && !instAdmin;
  const nav = ORG_NAV.filter(([k, , adminOnly]) => !adminOnly || instAdmin || (demo && k === "projects"));
  const [sec, setSec] = useState<OrgSection>(() =>
    (nav.find(([k]) => k === section)?.[0] ?? "org"));
  // a deep-linked section (?section=projects) may only exist once the role / features are
  // known (first render: not yet) — open it as soon as it appears
  const avail = nav.map(([k]) => k).join(",");
  const [asked, setAsked] = useState(section);
  useEffect(() => {
    if (asked && nav.some(([k]) => k === asked)) { setSec(asked as OrgSection); setAsked(undefined); }
  }, [avail]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="flex flex-col gap-3 md:flex-row">
      <nav aria-label="Organization sections" className="flex shrink-0 flex-wrap gap-1 md:w-44 md:flex-col md:flex-nowrap">
        {nav.map(([k, label]) => (
          <button key={k} onClick={() => setSec(k)} aria-current={sec === k ? "page" : undefined}
            className={`ui-focus shrink-0 rounded-md border-l-2 px-2 py-1.5 text-left text-[12.5px] ${sec === k
              ? "border-sea bg-panel2 font-medium text-slate-100" : "border-transparent text-mut hover:text-slate-200"}`}>{label}
            {k === "features" && featProblems > 0 && (
              <span title="features asked for but off" className="ml-1.5 rounded-full bg-red-500/80 px-1.5 text-[10px] text-white">{featProblems}<span className="sr-only"> problem(s)</span></span>
            )}</button>
        ))}
      </nav>
      <div className="min-w-0 flex-1">
        {sec === "org" ? <Org /> : sec === "members" ? (demo ? <DemoMembers /> : <Members />)
          : sec === "projects" ? (demo ? <DemoProjects /> : <ProjectsAdmin />)
          : sec === "features" ? <FeaturesAdmin /> : sec === "classification" ? <ClassificationAdmin instanceAdmin={instAdmin} />
          : <TeamsAdmin />}
      </div>
    </div>
  );
}

/** 3.4.2 — the memory engine: which index serves, and what it allows. On the 2.x index
 *  (sqlite) a project other than `default` has no memory and no level — writes are refused
 *  rather than lost, so the admin must be able to read it here. */
export function MemoryStoreState() {
  const [st, setSt] = useState<MemStore | null | undefined>(undefined);
  useEffect(() => { memoryStats().then((s) => setSt(s.store ?? null)).catch(() => setSt(null)); }, []);
  if (!st) return null;
  const on = st.mode === "postgres";
  return (
    <section aria-label="Memory store" className="rounded-lg border border-line bg-panel2/50 p-3 text-[12.5px]">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${on ? "bg-emerald-500" : st.migrating ? "bg-sky-400" : "bg-amber-400"}`} />
        <span className="text-slate-200">
          {on ? "Memory store 3.0 (Postgres + pgvector) — project memory and classification on"
            : st.migrating ? "Memory: 2.x index (sqlite) while the 3.0 store is being built — project memory and classification once it serves"
            : "Memory: 2.x index (sqlite) — project memory and classification off"}
        </span>
      </div>
      {!on && !st.migrating && (
        <div className="mt-1.5 text-[11.5px] text-mut">
          Only the <code>default</code> project has a memory here; a decision captured in Teams, an agent deliverable or a note of a session
          for another project (or above the default level) is refused, not written. Enable the store:{" "}
          <code>CORTHEXIS_DATABASE_URL</code> + <code>CORTHEXIS_MEMORY_BACKEND=auto</code>, then restart — the migration runs by itself (
          <a href="https://github.com/ninabot-ch/sokkan/blob/main/docs/UPGRADE.md#turn-the-store-on-later-sqlite-mode" target="_blank" rel="noreferrer" className="underline hover:text-slate-200">UPGRADE.md</a>).
        </div>
      )}
    </section>
  );
}

/** Setup › Engines — « Connect your AI » and the instance model keys on ONE page: with
 *  `connect_ai` on, each engine card carries its instance key for the admin (ConnectAI);
 *  without it, the model setup and, for the admin with `byok_admin`, the keys list. */
export function EnginesPage() {
  const me = useMe();
  const instAdmin = ["admin", "owner"].includes(me?.instance_role || me?.role || "");
  const connectOn = useFeatureOn("connect_ai");
  const byokOn = useFeatureOn("byok_admin");
  const demo = !!useFeatures().demo_captains && !instAdmin;
  if (connectOn) return (
    <>
      {demo && <ReadOnlyNote>The engines this instance allows. Keys are never shown — not even their last characters.</ReadOnlyNote>}
      <ConnectAI legacy={<Model />} />
      <div className="mt-4"><MemoryStoreState /></div>
    </>
  );
  return (
    <div className="space-y-4">
      <Model />
      <MemoryStoreState />
      {instAdmin && byokOn && (
        <section aria-label="Instance model keys" className="border-t border-line pt-3">
          <h3 className="mb-2 text-[12.5px] font-medium text-slate-100">Instance keys</h3>
          <ModelKeys />
        </section>
      )}
    </div>
  );
}

/** Setup › My account — identity, sign out, and the linked accounts (GitLab) when on. */
export function AccountPage() {
  const gitlab = useFeatures().registry?.items.find((i) => i.id === "gitlab")?.enabled ?? false;
  return (
    <div className="space-y-4">
      <Account />
      {gitlab && (
        <section id="linked" aria-label="Linked accounts" className="border-t border-line pt-3">
          <h3 className="mb-2 text-[12.5px] font-medium text-slate-100">Linked accounts</h3>
          <LinkedAccounts />
        </section>
      )}
    </div>
  );
}

export { Notifications, Secrets };
