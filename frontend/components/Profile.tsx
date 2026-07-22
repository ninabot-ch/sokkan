"use client";
import { useEffect, useState } from "react";
import { useMe, useCan } from "@/lib/me";
import {
  instanceInfo, instanceRename, iamUsers, iamUpsert, iamDelete,
  llmCredit, llmStatus, llmUsage, llmSetApiKey, llmSetSubscription,
  notifyStatus, notifySet, notifyTest,
  vaultList, vaultSet, vaultDelete,
  type InstanceInfo, type LlmStatus, type LlmUsage, type NotifyStatus,
} from "@/lib/api";
import type { IamUser } from "@/lib/types";

const ROLES = ["viewer", "dev", "admin", "owner"];
const fmt = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}k` : `${n}`;
type Section = "account" | "org" | "members" | "model" | "notify" | "secrets";

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
      {inf.update?.update_available && (
        <div className="rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-[12px] text-sky-200">
          <b>New version available: {inf.update.latest}</b>
          <span className="text-mut"> (installed: {inf.update.local_version})</span>
          {inf.tier ? (
            <div className="mt-1 text-[11.5px]">Managed instance: <b>Infra → My fleet</b> tab → "⬆ update" (admin).</div>
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

// ---------- Membres (IAM) ----------
function Members() {
  const isAdmin = useCan("admin");
  const [users, setUsers] = useState<IamUser[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("dev");
  const reload = () => iamUsers().then(setUsers).catch(() => setUsers([]));
  useEffect(() => { if (isAdmin) reload(); }, [isAdmin]);
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
          <button disabled={u.role === "owner"} onClick={() => iamDelete(u.email).then(reload)}
            className="rounded px-1 text-mut hover:text-red-400 disabled:opacity-30" title="remove">✕</button>
        </div>
      ))}
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
          <span className="text-slate-200">{included ? "Managed inference — Qwen3 Coder (Frankfurt EU), prepaid"
            : st.byok_kind === "api_key" ? "Your Anthropic API key"
            : st.byok_kind === "subscription" ? "Your Claude Pro/Max subscription"
            : st.mode === "env" ? "Key configured (environment)" : "No model configured"}</span>
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
        <div className="text-[12px] text-mut">
          Managed inference: <b className="text-slate-300">Qwen3 Coder</b> models, served from
          Frankfurt (EU), prepaid with credits. To switch to your own key (BYOK), contact us.
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
          <div className="rounded-lg border border-line/60 bg-panel2/30 p-3 opacity-70">
            <div className="text-[13px] text-slate-300">Managed inference (Qwen3 Coder · Frankfurt EU)</div>
            <div className="text-[11px] text-mut">Prepaid with credits, billed per token. Chosen at signup.</div></div>
          {choice && (
            <div className="rounded-lg border border-line bg-panel2/40 p-3">
              <input value={val} onChange={(e) => setVal(e.target.value)} type="password"
                placeholder={choice === "api" ? "sk-ant-…" : "token from claude setup-token…"}
                className="w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
              <div className="mt-2 flex items-center gap-2">
                <button disabled={busy} onClick={save} className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-50">save</button>
                <button onClick={() => { setChoice(null); setVal(""); }} className="text-[12px] text-mut hover:text-slate-200">cancel</button>
                <span className="ml-auto text-[10px] text-mut">stays on your VM, never transmitted</span></div>
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
  const [name, setName] = useState(""); const [val, setVal] = useState("");
  const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { if (isAdmin) vaultList().then((r) => setNames(r.names)).catch(() => {}); }, [isAdmin]);
  if (!isAdmin) return <div className="text-[12px] text-mut">Secrets are restricted to administrators.</div>;
  const add = () => {
    setErr(""); setBusy(true);
    vaultSet(name.trim(), val).then((r) => { setNames(r.names); setName(""); setVal(""); }).catch((e) => setErr(String(e))).finally(() => setBusy(false));
  };
  return (
    <div className="space-y-3 text-[12.5px]">
      <div className="text-[10.5px] text-mut">
        Secrets are encrypted at rest and injected into your sessions as environment variables
        (<span className="font-mono text-slate-300">$NAME</span>) — your agents use them to operate prod without the value
        ever showing in the UI or going to the model. They never leave this instance.
      </div>
      <div className="space-y-1.5">
        {names.map((n) => (
          <div key={n} className="flex items-center gap-2 rounded-lg border border-line bg-panel2/50 p-2">
            <span className="font-mono text-[12px] text-slate-100">{n}</span>
            <span className="font-mono text-[11px] text-mut">= ••••••••</span>
            <button onClick={() => vaultDelete(n).then((r) => setNames(r.names))}
              className="ml-auto rounded px-1 text-mut hover:text-red-400" title="delete">✕</button>
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

export default function Profile({ onClose }: { onClose: () => void }) {
  const [sec, setSec] = useState<Section>("account");
  const nav: [Section, string][] = [["account", "My account"], ["org", "Organization"], ["members", "Members"], ["model", "Model"], ["notify", "Notifications"], ["secrets", "Secrets"]];
  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/60 p-4 pt-14" onClick={onClose}>
      <div className="flex w-full max-w-2xl overflow-hidden rounded-2xl border border-line bg-panel shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="w-44 shrink-0 border-r border-line bg-panel2/40 p-2">
          <div className="px-2 py-1.5 text-[13px] font-semibold text-slate-100">Profile & organization</div>
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
            {sec === "account" ? <Account /> : sec === "org" ? <Org /> : sec === "members" ? <Members /> : sec === "model" ? <Model /> : sec === "notify" ? <Notifications /> : <Secrets />}
          </div>
        </div>
      </div>
    </div>
  );
}
