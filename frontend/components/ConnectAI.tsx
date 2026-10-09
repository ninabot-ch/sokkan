"use client";
// Setup › Engines (3.2.2: « Connect your AI » and « Model keys » on ONE page; feature
// `connect_ai`). Engine cards with an avatar; for the admin, each card carries its instance
// key (…last4, set by, when · Replace / Remove / Test — the same records as the Model keys
// API). Personal mode (connect anything, SOKKAN Router preselected) or governed mode
// (the admin sets allowed engines, zones and tiers; people only see those; choice per
// project). A connected engine can drive a Crew card (model `engine:<id>`).
import { useEffect, useState } from "react";
import {
  connectDefault, connectEngine, connectPolicy, connectProject, connectView, disconnectEngine, when,
  engineKeyDelete, engineKeyTest,
  type ConnectView, type Engine, type EnginePolicy, type ModelKey,
} from "@/lib/uifeatures";

const AVATAR: Record<string, [string, string]> = {
  sokkan_router: ["#D4A017", "#b45309"], claude: ["#d97757", "#9a3412"], openai: ["#10a37f", "#065f46"],
  gemini: ["#4285f4", "#7c3aed"], openrouter: ["#6366f1", "#312e81"], ollama: ["#64748b", "#1e293b"],
  magnitude: ["#0ea5e9", "#0f766e"],
};

function Avatar({ e }: { e: Engine }) {
  const [a1, a2] = AVATAR[e.id] ?? ["#3b82f6", "#1e3a8a"];
  return <span aria-hidden className="engine-avatar" style={{ ["--a1" as string]: a1, ["--a2" as string]: a2 }}>{e.avatar}</span>;
}

function Status({ e, governed }: { e: Engine; governed: boolean }) {
  if (governed && !e.allowed) return <span className="ui-chip ui-c-off"><span aria-hidden>⦸</span>not allowed</span>;
  if (e.is_default) return <span className="ui-chip ui-c-ok"><span aria-hidden>★</span>sessions use it</span>;
  if (e.connected) return <span className="ui-chip ui-c-ok"><span aria-hidden>✓</span>connected</span>;
  return <span className="ui-chip ui-c-off"><span aria-hidden>○</span>not connected</span>;
}

function KeyTest({ k }: { k: ModelKey }) {
  if (k.test_ok === null || k.test_ok === undefined)
    return <span className="ui-chip ui-c-off"><span aria-hidden>•</span>{k.tested_at ? k.test_detail || "not tested" : "not tested"}</span>;
  return k.test_ok
    ? <span className="ui-chip ui-c-ok"><span aria-hidden>✓</span>valid · {when(k.tested_at)}</span>
    : <span className="ui-chip ui-c-warn"><span aria-hidden>!</span>{k.test_detail}</span>;
}

/** The instance key(s) of an engine, for the admin: « key …xxxx, set by X on date ». */
function EngineKeys({ e, onReplace, onDone }: {
  e: Engine; onReplace: (provider: string) => void; onDone: (nv: ConnectView | null, msg: string) => void;
}) {
  const [confirm, setConfirm] = useState<string | null>(null);
  const [err, setErr] = useState("");
  if (!e.keys?.length) return null;
  return (
    <div className="space-y-1.5" role="group" aria-label={`${e.label} instance key`}>
      {e.keys.map((k) => (
        <div key={k.provider} className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-ink/40 px-2.5 py-1.5">
          <span className="text-[11.5px] text-mut">{e.keys!.length > 1 ? k.label : "instance key"}</span>
          <code className="rounded bg-ink px-1.5 text-[11.5px] text-slate-200" aria-label={`key ending ${k.masked.slice(1)}`}>key {k.masked}</code>
          <KeyTest k={k} />
          {k.pushed_at && <span className="ui-chip ui-c-ok"><span aria-hidden>⇡</span>on the gateway</span>}
          {k.push_error && <span className="ui-chip ui-c-warn"><span aria-hidden>!</span>{k.push_error}</span>}
          <span className="w-full text-[10.5px] text-mut">set by {k.set_by} on {when(k.set_at)}</span>
          <span className="ml-auto flex gap-1">
            <button onClick={() => onReplace(k.provider)} className="ui-focus rounded border border-line px-2 py-0.5 text-[11px] text-mut hover:text-slate-200">Replace</button>
            {k.testable && (
              <button onClick={() => engineKeyTest(e.id, k.provider).then((r) => onDone(null, `Test: ${r.detail}.`)).catch((x) => setErr(String(x.message || x)))}
                className="ui-focus rounded border border-line px-2 py-0.5 text-[11px] text-mut hover:text-slate-200">Test</button>)}
            {confirm === k.provider ? (
              <>
                <button onClick={() => { setConfirm(null); engineKeyDelete(e.id, k.provider).then((r) => onDone(r.view, `Key ${k.masked} removed${r.gateway.pushed ? " (and from the gateway)" : ""}.`)).catch((x) => setErr(String(x.message || x))); }}
                  className="ui-focus rounded bg-red-600/80 px-2 py-0.5 text-[11px] text-white">confirm remove</button>
                <button onClick={() => setConfirm(null)} className="ui-focus rounded px-1 text-[11px] text-mut">cancel</button>
              </>
            ) : (
              <button onClick={() => setConfirm(k.provider)} className="ui-focus rounded border border-red-500/40 px-2 py-0.5 text-[11px] text-red-300 hover:bg-red-500/10">Remove</button>
            )}
          </span>
        </div>
      ))}
      {err && <div role="alert" className="text-[11.5px] text-red-400">{err}</div>}
    </div>
  );
}

function EngineForm({ e, v, onDone }: { e: Engine; v: ConnectView; onDone: (nv: ConnectView | null, msg: string) => void }) {
  const c = e.connection;
  const [auth, setAuth] = useState<string>(c?.auth ?? e.auths[0]);
  const [key, setKey] = useState("");
  const [base, setBase] = useState(c?.base_url || e.default_base_url);
  const tiers = v.mode === "governed" && e.id === "sokkan_router" ? v.policy?.tiers ?? [] : [];
  const [model, setModel] = useState(c?.model || tiers[0] || "");
  const [small, setSmall] = useState(c?.small_model ?? "");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (f: () => Promise<ConnectView>, msg: string) => {
    setBusy(true); setErr("");
    try { onDone(await f(), msg); setKey(""); } catch (x) { setErr(String((x as Error).message || x)); } finally { setBusy(false); }
  };
  const needKey = (auth === "key" || auth === "login") && !c?.masked;
  const ro = !v.can_admin;
  return (
    <div className="space-y-2 rounded-xl border border-sea/40 bg-panel2/40 p-3" role="region" aria-label={`${e.label} settings`}>
      <div className="flex items-center gap-2">
        <Avatar e={e} />
        <div>
          <div className="text-[13.5px] font-semibold text-slate-100">{e.label}</div>
          <div className="text-[11px] text-mut">{e.blurb}</div>
        </div>
      </div>
      {!ro && <EngineKeys e={e} onDone={onDone}
        onReplace={(prov) => { setAuth(prov === "claude_login" ? "login" : e.auths.includes("key") ? "key" : e.auths[0]); setKey("");
          requestAnimationFrame(() => document.getElementById(`key-${e.id}`)?.focus()); }} />}
      {e.id === "sokkan_router" && v.welcome_url && (
        <a href={v.welcome_url} target="_blank" rel="noreferrer"
          className="ui-focus inline-flex items-center gap-1 rounded-full border border-brass/50 bg-brass/10 px-2.5 py-0.5 text-[11.5px] text-brass hover:bg-brass/20">
          🎁 Welcome credit — create your SOKKAN Router account ↗</a>
      )}
      {ro ? (
        <div className="text-[11.5px] text-mut">{e.connected ? `Connected by ${c?.by} · ${when(c?.at)}${c?.model ? ` · model ${c.model}` : ""}` : "Not connected yet."} Connecting engines is for the instance administrators.</div>
      ) : (
        <>
          {e.auths.length > 1 && (
            <fieldset className="flex gap-2">
              <legend className="sr-only">How to connect</legend>
              {e.auths.map((a) => (
                <label key={a} className={`flex cursor-pointer items-center gap-1.5 rounded-lg border px-2 py-1 text-[11.5px] ${auth === a ? "border-sea bg-sea/10 text-slate-100" : "border-line text-mut"}`}>
                  <input type="radio" name={`auth-${e.id}`} checked={auth === a} onChange={() => setAuth(a)} className="ui-focus accent-sky-500" />
                  {a === "key" ? "API key" : a === "login" ? "Login (subscription)" : "No key (local)"}
                </label>
              ))}
            </fieldset>
          )}
          {auth === "login" && (
            <div role="note" className="rounded-lg border border-amber-400/40 bg-amber-400/5 p-2 text-[11px] text-amber-200">
              ⚠ {v.login_note} On your machine: <code className="text-slate-200">claude setup-token</code>, then paste the token.
            </div>
          )}
          {(auth === "key" || auth === "login") && (
            <input id={`key-${e.id}`} type="password" autoComplete="off" spellCheck={false} value={key} onChange={(x) => setKey(x.target.value)}
              aria-label={auth === "login" ? "login token" : "API key"}
              placeholder={c?.masked ? `${c.masked} — leave empty to keep it` : auth === "login" ? "token from claude setup-token" : "API key"}
              className="ui-focus w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none" />
          )}
          {e.bridge === "anthropic" && (
            <input value={base} onChange={(x) => setBase(x.target.value)} aria-label="base URL"
              placeholder="base URL — an endpoint speaking the Anthropic Messages API"
              className="ui-focus w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none" />
          )}
          {e.id !== "claude" && (
            <div className="flex gap-2">
              {tiers.length ? (
                <select value={model} onChange={(x) => setModel(x.target.value)} aria-label="tier"
                  className="ui-focus min-w-0 flex-1 rounded border border-line bg-panel2 px-2 py-1.5 text-[12px] text-slate-100">
                  {tiers.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              ) : (
                <input value={model} onChange={(x) => setModel(x.target.value)} aria-label="model" placeholder="model"
                  className="ui-focus min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none" />
              )}
              <input value={small} onChange={(x) => setSmall(x.target.value)} aria-label="small model" placeholder="small/fast model (optional)"
                className="ui-focus min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-[12px] text-slate-100 outline-none" />
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <button disabled={busy || (needKey && key.trim().length < 8)}
              onClick={() => run(() => connectEngine(e.id, { auth, key: key.trim(), base_url: base.trim(), model: model.trim(), small_model: small.trim() }), `${e.label} connected.`)}
              className="ui-focus rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-40">
              {e.connected ? "update" : "connect"}</button>
            {e.connected && !e.is_default && !v.operator_managed && (
              <button disabled={busy} onClick={() => run(() => connectDefault(e.id), `Sessions now run on ${e.label}.`)}
                className="ui-focus rounded border border-emerald-500/50 px-3 py-1 text-[12px] text-emerald-300 hover:bg-emerald-500/10">★ use for sessions</button>
            )}
            {e.connected && (
              <button disabled={busy} onClick={() => run(() => disconnectEngine(e.id), `${e.label} disconnected.`)}
                className="ui-focus rounded border border-red-500/40 px-3 py-1 text-[12px] text-red-300 hover:bg-red-500/10">disconnect</button>
            )}
          </div>
        </>
      )}
      {e.connected && <div className="text-[10.5px] text-mut">Crew: pick « {e.label} » as a card&apos;s engine (<code>{e.crew_value}</code>).</div>}
      {err && <div role="alert" className="text-[11.5px] text-red-400">{err}</div>}
    </div>
  );
}

function PolicyEditor({ v, onDone }: { v: ConnectView; onDone: (nv: ConnectView, msg: string) => void }) {
  const p0 = v.policy ?? { allowed: [], zones: {}, allowed_zones: [], tiers: [], per_project: false };
  const [p, setP] = useState<EnginePolicy>(p0);
  const [tiers, setTiers] = useState(p0.tiers.join(", "));
  const [err, setErr] = useState("");
  const toggle = <T,>(xs: T[], x: T) => (xs.includes(x) ? xs.filter((y) => y !== x) : [...xs, x]);
  const save = () => connectPolicy({ ...p, tiers: tiers.split(",").map((t) => t.trim()).filter(Boolean) })
    .then((nv) => onDone(nv, "Policy saved — people now see the allowed engines only.")).catch((x) => setErr(String(x.message || x)));
  return (
    <details className="rounded-xl border border-line bg-panel2/30 p-3" open={!p0.allowed.length}>
      <summary className="ui-focus cursor-pointer text-[12.5px] font-medium text-slate-100">Policy — allowed engines, zones, tiers</summary>
      <div className="mt-2 space-y-2 text-[12px]">
        <table className="w-full text-left">
          <thead><tr className="text-[10.5px] text-mut"><th className="py-1 font-normal">engine</th><th className="font-normal">allowed</th><th className="font-normal">zone</th></tr></thead>
          <tbody>
            {v.engines.map((e) => (
              <tr key={e.id} className="border-t border-line/60">
                <td className="py-1 text-slate-200">{e.label}</td>
                <td><input type="checkbox" aria-label={`allow ${e.label}`} checked={p.allowed.includes(e.id)}
                  onChange={() => setP({ ...p, allowed: toggle(p.allowed, e.id) })} className="ui-focus accent-sky-500" /></td>
                <td>
                  <select aria-label={`zone of ${e.label}`} value={p.zones[e.id] ?? (e.zone || "")}
                    onChange={(x) => setP({ ...p, zones: { ...p.zones, [e.id]: x.target.value } })}
                    className="ui-focus rounded border border-line bg-panel2 px-1 py-0.5 text-[11.5px] text-slate-200">
                    <option value="">—</option>
                    {v.zones.map((z) => <option key={z} value={z}>{z}</option>)}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <fieldset className="flex flex-wrap items-center gap-2">
          <legend className="mb-1 text-[11px] text-mut">Allowed zones (none ticked = any zone)</legend>
          {v.zones.map((z) => (
            <label key={z} className="flex items-center gap-1 text-slate-300">
              <input type="checkbox" checked={p.allowed_zones.includes(z)} onChange={() => setP({ ...p, allowed_zones: toggle(p.allowed_zones, z) })} className="ui-focus accent-sky-500" />{z}
            </label>
          ))}
        </fieldset>
        <label className="block">
          <span className="mb-1 block text-[11px] text-mut">SOKKAN tiers allowed (comma-separated, empty = any)</span>
          <input value={tiers} onChange={(x) => setTiers(x.target.value)} placeholder="sokkan-ship, sokkan-deep"
            className="ui-focus w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-slate-100 outline-none" />
        </label>
        <label className="flex items-center gap-1.5 text-slate-300">
          <input type="checkbox" checked={p.per_project} onChange={(x) => setP({ ...p, per_project: x.target.checked })} className="ui-focus accent-sky-500" />
          a project maintainer may pick the project&apos;s engine
        </label>
        <button onClick={save} className="ui-focus rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">save policy</button>
        {err && <div role="alert" className="text-[11.5px] text-red-400">{err}</div>}
      </div>
    </details>
  );
}

export default function ConnectAI({ legacy }: { legacy?: React.ReactNode }) {
  const [v, setV] = useState<ConnectView | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  useEffect(() => {
    connectView().then((nv) => {
      setV(nv);
      setSel(nv.default ?? nv.engines.find((e) => e.preselected)?.id ?? null);
    }).catch((e) => setMsg(String(e.message || e)));
  }, []);
  if (!v) return <div className="text-[12px] text-mut">{msg || "…"}</div>;
  const governed = v.mode === "governed";
  const shown = v.engines.filter((e) => e.allowed || (governed && v.can_admin));
  const cur = shown.find((e) => e.id === sel) ?? null;
  const done = (nv: ConnectView | null, m: string) => {
    connectView().then(setV).catch(() => { if (nv) setV(nv); });
    setMsg(m);
  };
  return (
    <div className="space-y-3 text-[12.5px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`ui-chip ${governed ? "ui-c-approval" : "ui-c-read"}`}>
          <span aria-hidden>{governed ? "⛨" : "◎"}</span>{governed ? "Governed mode" : "Personal mode"}</span>
        <span className="text-[11.5px] text-mut">{governed
          ? (v.can_admin ? "You choose which engines, zones and tiers people may use (policy below), then connect them."
            : "Your administrator chooses which engines, zones and tiers are allowed.")
          : v.can_admin ? "Connect the engines you like. SOKKAN Router is a good start."
            : "The engines this instance can use. Connecting one is for the instance administrators."}</span>
      </div>
      {v.operator_managed && (
        <div className="rounded-lg border border-line bg-panel2/40 p-2 text-[11.5px] text-mut">This instance uses managed inference (operated by NINABOT): sessions keep the gateway; engines below serve Crew cards.</div>
      )}
      {governed && v.can_admin && !v.engines.some((e) => e.allowed) && (v.llm?.configured ? (
        // 3.4.6: with an instance engine (key, gateway or CLI login) sessions DO run — the policy
        // only decides which engines people may pick (Crew cards, per project)
        <div role="status" className="rounded-lg border border-line bg-panel2/40 p-2.5 text-[12px] text-slate-200">
          Sessions run on the instance engine{v.llm.mode === "cli-login" ? " (Claude CLI login)" : ""}. No engine
          is allowed in the policy yet, so people cannot pick one for a Crew card or a project: tick the ones they
          may use and save.</div>
      ) : (
        <div role="status" className="rounded-lg border border-brass/40 bg-brass/5 p-2.5 text-[12px] text-slate-200">
          <b>Start here:</b> no engine is allowed yet, so nothing can run. Tick at least one engine in the policy and
          save; then pick it below to connect it (key or login).</div>
      ))}
      {governed && v.can_admin && <PolicyEditor v={v} onDone={done} />}
      {governed && !shown.length && (
        <div className="rounded-lg border border-dashed border-line p-3 text-[12px] text-mut">No engine allowed yet — ask your administrator.</div>
      )}
      <div role="listbox" aria-label="AI engines" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {shown.map((e) => (
          <button key={e.id} role="option" aria-selected={sel === e.id} data-selected={sel === e.id}
            onClick={() => setSel(e.id)}
            className={`engine-card ui-focus flex items-start gap-2.5 rounded-xl border border-line bg-panel2/40 p-2.5 text-left hover:border-sea/50 ${governed && !e.allowed ? "border-dashed" : ""}`}>
            <Avatar e={e} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-semibold text-slate-100">{e.label}</span>
              <span className="block truncate text-[10.5px] text-mut">{e.vendor}{e.zone ? ` · zone ${e.zone}` : ""}</span>
              <span className="mt-1 flex flex-wrap gap-1"><Status e={e} governed={governed} />
                {e.recommended && !governed && <span className="ui-chip ui-c-approval"><span aria-hidden>★</span>recommended</span>}
                {e.keys?.map((k) => <span key={k.provider} className="ui-chip ui-c-read"><span aria-hidden>⚿</span>key {k.masked}</span>)}</span>
            </span>
          </button>
        ))}
      </div>
      {cur && <EngineForm key={cur.id} e={cur} v={v} onDone={done} />}
      {v.project.can_choose && v.project.slug && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line p-2.5">
          <label htmlFor="proj-engine" className="text-[12px] text-slate-200">Engine of project <b>{v.project.slug}</b></label>
          <select id="proj-engine" value={v.project.engine ?? ""}
            onChange={(x) => connectProject(v.project.slug!, x.target.value || null).then(() => done(v, "Project engine saved.")).catch((er) => setMsg(String(er.message || er)))}
            className="ui-focus rounded border border-line bg-panel2 px-2 py-1 text-[12px] text-slate-100">
            <option value="">instance default</option>
            {v.engines.filter((e) => e.allowed && e.connected).map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
          </select>
        </div>
      )}
      <div aria-live="polite" className="text-[11.5px] text-emerald-300">{msg}</div>
      {legacy && (
        <details className="rounded-xl border border-line p-3">
          <summary className="ui-focus cursor-pointer text-[12px] text-mut">Current model setup (advanced)</summary>
          <div className="mt-2">{legacy}</div>
        </details>
      )}
    </div>
  );
}
