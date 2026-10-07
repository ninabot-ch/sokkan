"use client";
// 3.2 `byok_admin` (lot 7) — Profile → Model keys: the instance admin sets, replaces or
// deletes the model provider keys. Stored encrypted with the vault key; never shown again
// (…last 4, when, who); optional validity test; Anthropic key used by the sessions and
// pushed to the SOKKAN gateway when one is configured.
import { useEffect, useState } from "react";
import {
  modelKeyDelete, modelKeySet, modelKeyTest, modelKeys, when, type ModelKey, type ModelKeysView,
} from "@/lib/uifeatures";

function TestChip({ k }: { k: ModelKey }) {
  if (k.test_ok === null || k.test_ok === undefined)
    return <span className="ui-chip ui-c-off"><span aria-hidden>•</span>{k.tested_at ? k.test_detail || "not tested" : "not tested"}</span>;
  return k.test_ok
    ? <span className="ui-chip ui-c-ok"><span aria-hidden>✓</span>valid · {when(k.tested_at)}</span>
    : <span className="ui-chip ui-c-warn"><span aria-hidden>!</span>{k.test_detail}</span>;
}

export default function ModelKeys() {
  const [v, setV] = useState<ModelKeysView | null>(null);
  const [prov, setProv] = useState("anthropic");
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [test, setTest] = useState(true);
  const [use, setUse] = useState(true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const reload = () => modelKeys().then(setV).catch((e) => setMsg({ ok: false, text: String(e.message || e) }));
  useEffect(() => { reload(); }, []);
  if (!v) return <div className="text-[12px] text-mut">{msg?.text ?? "…"}</div>;

  const p = v.providers.find((x) => x.id === prov);
  const existing = v.keys.find((k) => k.provider === prov && k.scope === "instance");
  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await modelKeySet(prov, key.trim(), { test: test && !!p?.testable, use_for_sessions: use, base_url: baseUrl.trim() });
      const parts = [`Key ${r.key.masked} saved, encrypted.`];
      if (r.test) parts.push(`Test: ${r.test.detail}.`);
      if (r.sessions) parts.push(r.sessions[0].toUpperCase() + r.sessions.slice(1) + ".");
      if (v.gateway.configured && prov === "anthropic") parts.push(r.gateway.pushed ? `Pushed to the gateway (${v.gateway.client}).` : `Gateway: ${r.gateway.detail}.`);
      setMsg({ ok: r.test?.ok !== false, text: parts.join(" ") });
      setKey(""); setBaseUrl("");
      reload();
    } catch (e) { setMsg({ ok: false, text: String((e as Error).message || e) }); } finally { setBusy(false); }
  };
  const del = async (provider: string) => {
    setConfirmDel(null);
    try {
      const r = await modelKeyDelete(provider);
      setMsg({ ok: true, text: `Key deleted.${r.gateway.pushed ? " Removed from the gateway too." : ""}` });
      reload();
    } catch (e) { setMsg({ ok: false, text: String((e as Error).message || e) }); }
  };
  const retest = (provider: string) => modelKeyTest(provider)
    .then((r) => { setMsg({ ok: r.ok !== false, text: `Test: ${r.detail}.` }); reload(); })
    .catch((e) => setMsg({ ok: false, text: String(e.message || e) }));

  return (
    <div className="space-y-3 text-[12.5px]">
      <div className="rounded-lg border border-line bg-panel2/40 p-3 text-[11.5px] text-mut">
        Keys are stored <b className="text-slate-300">encrypted with the instance vault key</b> and are never shown again —
        only their last 4 characters, when and by whom. A test calls the provider once; the key is never logged.
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          <span className="ui-chip ui-c-read">scope: instance</span>
          <span className="ui-chip ui-c-off" title="the field exists; BYOK per project is planned">per project: planned</span>
          <span className={`ui-chip ${v.gateway.configured ? "ui-c-ok" : "ui-c-off"}`}>
            {v.gateway.configured ? `gateway: ${v.gateway.client}` : "no SOKKAN gateway"}</span>
          <span className={`ui-chip ${v.sessions.key_ref ? "ui-c-ok" : "ui-c-off"}`}>
            sessions: {v.sessions.key_ref ? `key ${v.sessions.key_ref}` : v.sessions.mode}</span>
        </div>
      </div>

      <div className="space-y-1.5">
        <div className="text-[11px] text-mut">Keys of this instance</div>
        {!v.keys.length && <div className="rounded-lg border border-dashed border-line p-3 text-[12px] text-mut">No key yet.</div>}
        {v.keys.map((k) => (
          <div key={`${k.scope}:${k.provider}`} className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel2/40 px-3 py-2">
            <span className="text-slate-100">{k.label}</span>
            <code className="rounded bg-ink px-1.5 text-[11.5px] text-slate-300" aria-label={`key ending ${k.masked.slice(1)}`}>{k.masked}</code>
            <TestChip k={k} />
            {k.push_error && <span className="ui-chip ui-c-warn"><span aria-hidden>!</span>{k.push_error}</span>}
            {k.pushed_at && <span className="ui-chip ui-c-ok"><span aria-hidden>⇡</span>on the gateway</span>}
            <span className="w-full text-[10.5px] text-mut">set by {k.set_by} · {when(k.set_at)}{k.base_url ? ` · ${k.base_url}` : ""}</span>
            <span className="ml-auto flex gap-1">
              <button onClick={() => { setProv(k.provider); setKey(""); document.getElementById("mk-key")?.focus(); }}
                className="ui-focus rounded border border-line px-2 py-0.5 text-[11px] text-mut hover:text-slate-200">replace</button>
              {v.providers.find((x) => x.id === k.provider)?.testable && (
                <button onClick={() => retest(k.provider)} className="ui-focus rounded border border-line px-2 py-0.5 text-[11px] text-mut hover:text-slate-200">test</button>)}
              {confirmDel === k.provider ? (
                <>
                  <button onClick={() => del(k.provider)} className="ui-focus rounded bg-red-600/80 px-2 py-0.5 text-[11px] text-white">confirm delete</button>
                  <button onClick={() => setConfirmDel(null)} className="ui-focus rounded px-1 text-[11px] text-mut">cancel</button>
                </>
              ) : (
                <button onClick={() => setConfirmDel(k.provider)} className="ui-focus rounded border border-red-500/40 px-2 py-0.5 text-[11px] text-red-300 hover:bg-red-500/10">delete</button>
              )}
            </span>
          </div>
        ))}
      </div>

      <div className="space-y-2 rounded-lg border border-line p-3">
        <div className="text-[12.5px] text-slate-100">{existing ? `Replace the ${p?.label} key` : "Add a key"}</div>
        <div className="flex gap-2">
          <label className="sr-only" htmlFor="mk-prov">Provider</label>
          <select id="mk-prov" value={prov} onChange={(e) => setProv(e.target.value)}
            className="ui-focus rounded border border-line bg-panel2 px-2 py-1.5 text-slate-100">
            {v.providers.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
          </select>
          <label className="sr-only" htmlFor="mk-key">Key</label>
          <input id="mk-key" type="password" autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)}
            placeholder={p?.hint} className="ui-focus min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-slate-100 outline-none" />
        </div>
        {(prov === "custom" || prov === "openrouter" || prov === "sokkan_router") && (
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="base URL (optional) — https://…" aria-label="base URL"
            className="ui-focus w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-slate-100 outline-none" />
        )}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11.5px] text-slate-300">
          <label className={`flex items-center gap-1.5 ${p?.testable ? "" : "opacity-50"}`}>
            <input type="checkbox" checked={test && !!p?.testable} disabled={!p?.testable} onChange={(e) => setTest(e.target.checked)} className="ui-focus accent-sky-500" />
            test it now</label>
          {prov === "anthropic" && (
            <label className={`flex items-center gap-1.5 ${v.sessions.operator_managed ? "opacity-50" : ""}`}>
              <input type="checkbox" checked={use && !v.sessions.operator_managed} disabled={v.sessions.operator_managed}
                onChange={(e) => setUse(e.target.checked)} className="ui-focus accent-sky-500" />
              sessions use this key</label>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button onClick={save} disabled={busy || key.trim().length < 8}
            className="ui-focus rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-40">
            {busy ? "saving…" : existing ? "replace" : "save"}</button>
          <span className="text-[10.5px] text-mut">{existing ? `current: ${existing.masked}` : "encrypted at rest"}</span>
        </div>
      </div>
      <div aria-live="polite">
        {msg && <div role={msg.ok ? "status" : "alert"} className={`text-[11.5px] ${msg.ok ? "text-emerald-300" : "text-amber-300"}`}>{msg.text}</div>}
      </div>
    </div>
  );
}
