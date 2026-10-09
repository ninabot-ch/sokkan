"use client";
// 3.5 — Operate › Alerts › Settings: where the data comes from (sources), who gets told (channels),
// and the silences in force. Every item can be TESTED in one click before a rule relies on it.
// Secrets are write-only: the API answers « set », never the value.
import { useEffect, useState } from "react";
import {
  createChannel, createSource, deleteChannel, deleteSilence, deleteSource, listChannels, listSilences, listSources,
  testChannel, testSource, testSourceDraft, updateChannel, updateSource,
} from "@/lib/alerting";
import { since, type AlertSource, type Channel, type ChannelKindDef, type Rule, type Silence, type SourceIn, type SourceKind, type TestResult } from "@/lib/alertingModel";
import { Banner, Field, Seg, btn, fmtTime, inputCls } from "./bits";

type Tab = "sources" | "channels" | "silences";

export default function Settings({ rules, canManage, initial = "channels", onBack }: {
  rules: Rule[]; canManage: boolean; initial?: Tab; onBack: () => void;
}) {
  const [tab, setTab] = useState<Tab>(initial);
  return (
    <div className="mx-auto max-w-4xl space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={onBack} className={btn.small}>← Alerts</button>
        <h2 className="text-[14px] font-semibold text-slate-100">Alert settings</h2>
        <div className="ml-auto"><Seg label="settings section" value={tab} onChange={setTab}
          opts={[{ id: "channels", label: "Channels" }, { id: "sources", label: "Sources" }, { id: "silences", label: "Silences" }]} /></div>
      </div>
      {!canManage && tab !== "silences" && <Banner tone="info">Read only — a maintainer of the project manages sources and channels.</Banner>}
      {tab === "sources" && <Sources canManage={canManage} />}
      {tab === "channels" && <Channels canManage={canManage} />}
      {tab === "silences" && <Silences rules={rules} />}
    </div>
  );
}

// ---------------------------------------------------------------- sources

const KIND_LABEL: Record<SourceKind, string> = {
  prometheus: "Prometheus", loki: "Loki", elasticsearch: "Elasticsearch", opensearch: "OpenSearch", sokkan: "SOKKAN", external: "External alerts",
};
const KIND_HINT: Record<SourceKind, string> = {
  prometheus: "metrics (PromQL)", loki: "logs (LogQL)", elasticsearch: "logs & documents", opensearch: "logs & documents",
  sokkan: "the cockpit's own figures: costs, agent runs, audit", external: "alerts POSTed by Grafana, a CI…",
};

export function StatusDot({ ok, title }: { ok: boolean | null | undefined; title?: string }) {
  const c = ok === true ? "bg-emerald-400" : ok === false ? "bg-red-400" : "bg-slate-500";
  return <span className="inline-flex items-center gap-1" title={title}>
    <span aria-hidden className={`inline-block h-2 w-2 rounded-full ${c}`} />
    <span className="sr-only">{ok === true ? "reachable" : ok === false ? "unreachable" : "not checked"}</span>
  </span>;
}

function Sources({ canManage }: { canManage: boolean }) {
  const [list, setList] = useState<AlertSource[] | null>(null);
  const [err, setErr] = useState("");
  const [edit, setEdit] = useState<AlertSource | "new" | null>(null);
  const [tests, setTests] = useState<Record<number, TestResult | "…">>({});
  const load = () => listSources().then(setList).catch((e) => setErr(String(e.message || e)));
  useEffect(() => { load(); }, []);
  const test = (id: number) => { setTests((t) => ({ ...t, [id]: "…" })); testSource(id).then((r) => setTests((t) => ({ ...t, [id]: r }))).catch((e) => setTests((t) => ({ ...t, [id]: { ok: false, error: String(e.message || e) } }))); };
  if (err) return <Banner tone="error">{err}</Banner>;
  if (!list) return <div className="text-[12px] text-mut">Loading…</div>;
  return (
    <div className="space-y-2">
      {list.map((s) => {
        const t = tests[s.id];
        const ok = t && t !== "…" ? t.ok : s.status?.ok;
        return (
          <div key={s.id} className="rounded-xl border border-line bg-panel2/40 p-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <StatusDot ok={ok} title={s.status?.error || undefined} />
              <span className="text-[13px] font-medium text-slate-100">{s.name}</span>
              <span className="rounded border border-line px-1.5 text-[10.5px] text-mut">{KIND_LABEL[s.kind]}</span>
              {s.builtin && <span className="text-[10.5px] text-mut">built in</span>}
              {s.scope === "instance" && <span className="text-[10.5px] text-mut">· all projects</span>}
              <span className="ml-auto flex gap-1.5">
                {s.kind !== "external" && <button type="button" className={btn.small} onClick={() => test(s.id)} disabled={t === "…"}>{t === "…" ? "testing…" : "Test"}</button>}
                {canManage && !s.builtin && <button type="button" className={btn.small} onClick={() => setEdit(s)}>Edit</button>}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-mut">
              <span>{KIND_HINT[s.kind]}</span>
              {s.url && <span className="truncate font-mono">{s.url}</span>}
              {s.options?.index && <span className="font-mono">index {s.options.index}</span>}
              {s.status?.latency_ms != null && <span>{s.status.latency_ms} ms</span>}
            </div>
            {t && t !== "…" && <div className={`mt-1 text-[11.5px] ${t.ok ? "text-emerald-300" : "text-red-300"}`}>{t.ok ? `✓ ${t.detail || "reachable"}${t.latency_ms ? ` · ${t.latency_ms} ms` : ""}` : `✕ ${t.error || t.detail || "unreachable"}`}</div>}
            {edit !== "new" && edit?.id === s.id && <SourceEditor src={s} onDone={() => { setEdit(null); load(); }} />}
          </div>
        );
      })}
      {canManage && (edit === "new" ? <div className="rounded-xl border border-sea/40 bg-panel2/40 p-2.5"><SourceEditor onDone={() => { setEdit(null); load(); }} /></div>
        : <button type="button" className={btn.ghost} onClick={() => setEdit("new")}>+ Add a source (Elasticsearch, OpenSearch, another Prometheus or Loki)</button>)}
    </div>
  );
}

function SourceEditor({ src, onDone }: { src?: AlertSource; onDone: () => void }) {
  const [f, setF] = useState<SourceIn>({
    name: src?.name || "", kind: src?.kind || "elasticsearch", url: src?.url || "",
    auth: { kind: src?.auth?.kind || "none", user: src?.auth?.user || "", secret: "" },
    options: { index: src?.options?.index || "logs-*", time_field: src?.options?.time_field || "@timestamp", message_field: src?.options?.message_field || "message" },
  });
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<TestResult | null>(null);
  const [err, setErr] = useState("");
  const es = f.kind === "elasticsearch" || f.kind === "opensearch";
  const body = (): SourceIn => ({ ...f, auth: { ...f.auth, ...(f.auth.secret ? {} : { secret: undefined }) } });
  const save = async () => {
    setBusy(true); setErr("");
    try { if (src) await updateSource(src.id, body()); else await createSource(body()); onDone(); }
    catch (e) { setErr(String((e as Error).message || e)); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!src || !confirm(`Delete the source « ${src.name} »? Rules that use it must be changed first.`)) return;
    try { await deleteSource(src.id); onDone(); } catch (e) { setErr(String((e as Error).message || e)); }
  };
  const test = async () => { setRes(null); setBusy(true); try { setRes(await testSourceDraft(body())); } catch (e) { setRes({ ok: false, error: String((e as Error).message || e) }); } finally { setBusy(false); } };
  return (
    <div className="mt-2 space-y-2.5 border-t border-line pt-2.5">
      <div className="grid gap-2.5 sm:grid-cols-2">
        <Field label="Name" htmlFor="se-name"><input id="se-name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Production logs" className={inputCls} /></Field>
        <Field label="Type" htmlFor="se-kind">
          <select id="se-kind" value={f.kind} disabled={!!src} onChange={(e) => setF({ ...f, kind: e.target.value as SourceKind })} className={inputCls}>
            {(["elasticsearch", "opensearch", "prometheus", "loki"] as SourceKind[]).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
          </select>
        </Field>
      </div>
      <Field label="URL" htmlFor="se-url" hint="reachable from the SOKKAN server (private address is fine)">
        <input id="se-url" value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder={es ? "https://elastic.internal:9200" : "http://prometheus:9090"} className={`${inputCls} font-mono`} />
      </Field>
      {es && (
        <div className="grid gap-2.5 sm:grid-cols-3">
          <Field label="Default index" htmlFor="se-idx"><input id="se-idx" value={f.options.index || ""} onChange={(e) => setF({ ...f, options: { ...f.options, index: e.target.value } })} className={`${inputCls} font-mono`} /></Field>
          <Field label="Time field" htmlFor="se-tf"><input id="se-tf" value={f.options.time_field || ""} onChange={(e) => setF({ ...f, options: { ...f.options, time_field: e.target.value } })} className={`${inputCls} font-mono`} /></Field>
          <Field label="Message field" htmlFor="se-mf"><input id="se-mf" value={f.options.message_field || ""} onChange={(e) => setF({ ...f, options: { ...f.options, message_field: e.target.value } })} className={`${inputCls} font-mono`} /></Field>
        </div>
      )}
      <div className="grid gap-2.5 sm:grid-cols-3">
        <Field label="Authentication" htmlFor="se-auth">
          <select id="se-auth" value={f.auth.kind} onChange={(e) => setF({ ...f, auth: { ...f.auth, kind: e.target.value as SourceIn["auth"]["kind"] } })} className={inputCls}>
            <option value="none">none</option><option value="basic">user + password</option><option value="apikey">API key</option><option value="bearer">bearer token</option>
          </select>
        </Field>
        {f.auth.kind === "basic" && <Field label="User" htmlFor="se-user"><input id="se-user" value={f.auth.user || ""} onChange={(e) => setF({ ...f, auth: { ...f.auth, user: e.target.value } })} className={inputCls} autoComplete="off" /></Field>}
        {f.auth.kind !== "none" && (
          <Field label={f.auth.kind === "basic" ? "Password" : "Key"} htmlFor="se-secret" hint={src?.auth?.secret_set ? "set — leave empty to keep it" : "stored in the project vault, never shown again"}>
            <input id="se-secret" type="password" value={f.auth.secret || ""} onChange={(e) => setF({ ...f, auth: { ...f.auth, secret: e.target.value } })} className={inputCls} autoComplete="new-password"
              placeholder={src?.auth?.secret_set ? "•••••• (unchanged)" : ""} />
          </Field>
        )}
      </div>
      {res && <Banner tone={res.ok ? "ok" : "error"}>{res.ok ? `✓ Reachable — ${res.detail || "ok"}${res.latency_ms ? ` · ${res.latency_ms} ms` : ""}` : `✕ ${res.error || res.detail || "unreachable"}`}</Banner>}
      {err && <Banner tone="error">{err}</Banner>}
      <div className="flex flex-wrap gap-2">
        <button type="button" className={btn.ghost} onClick={test} disabled={busy || !f.url}>Test the connection</button>
        <button type="button" className={btn.primary} onClick={save} disabled={busy || !f.name.trim() || !f.url.trim()}>{src ? "Save" : "Add the source"}</button>
        <button type="button" className={btn.small} onClick={onDone}>Cancel</button>
        {src && <button type="button" className={`${btn.danger} ml-auto`} onClick={remove}>Delete</button>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- channels

const CH_ICON: Record<string, string> = { telegram: "✈", teams: "T", slack: "#", email: "@", webhook: "⇢", pagerduty: "⚑" };

export function ChannelBadge({ c }: { c: Pick<Channel, "kind" | "name"> }) {
  return <span className="inline-flex items-center gap-1.5">
    <span aria-hidden className="inline-flex h-5 w-5 items-center justify-center rounded-md bg-panel2 text-[11px] text-slate-200 ring-1 ring-line">{CH_ICON[c.kind] || "•"}</span>
    <span className="truncate">{c.name}</span>
  </span>;
}

function Channels({ canManage }: { canManage: boolean }) {
  const [data, setData] = useState<{ channels: Channel[]; kinds: ChannelKindDef[] } | null>(null);
  const [err, setErr] = useState("");
  const [edit, setEdit] = useState<Channel | "new" | null>(null);
  const [tests, setTests] = useState<Record<number, string>>({});
  const load = () => listChannels().then(setData).catch((e) => setErr(String(e.message || e)));
  useEffect(() => { load(); }, []);
  const test = (id: number) => { setTests((t) => ({ ...t, [id]: "…" })); testChannel(id).then((r) => setTests((t) => ({ ...t, [id]: r.ok ? `✓ sent — ${r.detail || "check the channel"}` : `✕ ${r.detail || "failed"}` }))).catch((e) => setTests((t) => ({ ...t, [id]: `✕ ${e.message || e}` }))); };
  if (err) return <Banner tone="error">{err}</Banner>;
  if (!data) return <div className="text-[12px] text-mut">Loading…</div>;
  return (
    <div className="space-y-2">
      {data.channels.length === 0 && edit !== "new" && (
        <div className="rounded-xl border border-dashed border-line p-4 text-center text-[12.5px] text-mut">
          No channel yet. An alert that tells nobody is just a log line — add Telegram, Teams, Slack, e-mail or a webhook.
        </div>
      )}
      {data.channels.map((c) => (
        <div key={c.id} className="rounded-xl border border-line bg-panel2/40 p-2.5">
          <div className="flex flex-wrap items-center gap-2 text-[13px] text-slate-100">
            <ChannelBadge c={c} />
            <span className="rounded border border-line px-1.5 text-[10.5px] text-mut">{data.kinds.find((k) => k.kind === c.kind)?.label || c.kind}</span>
            {!c.enabled && <span className="text-[10.5px] text-amber-300">off</span>}
            {c.scope === "instance" && <span className="text-[10.5px] text-mut">instance</span>}
            {c.last_test && <span className={`text-[10.5px] ${c.last_test.ok ? "text-emerald-300" : "text-red-300"}`}>{c.last_test.ok ? "✓" : "✕"} tested {since(c.last_test.at)} ago</span>}
            <span className="ml-auto flex gap-1.5">
              <button type="button" className={btn.small} onClick={() => test(c.id)} disabled={tests[c.id] === "…"}>{tests[c.id] === "…" ? "sending…" : "Send a test"}</button>
              {canManage && !c.builtin && <button type="button" className={btn.small} onClick={() => setEdit(c)}>Edit</button>}
            </span>
          </div>
          {tests[c.id] && tests[c.id] !== "…" && <div className={`mt-1 text-[11.5px] ${tests[c.id].startsWith("✓") ? "text-emerald-300" : "text-red-300"}`}>{tests[c.id]}</div>}
          {edit !== "new" && edit?.id === c.id && <ChannelEditor kinds={data.kinds} ch={c} onDone={() => { setEdit(null); load(); }} />}
        </div>
      ))}
      {canManage && (edit === "new" ? <div className="rounded-xl border border-sea/40 bg-panel2/40 p-2.5"><ChannelEditor kinds={data.kinds} onDone={() => { setEdit(null); load(); }} /></div>
        : <button type="button" className={btn.ghost} onClick={() => setEdit("new")}>+ Add a channel</button>)}
    </div>
  );
}

/** Add / edit a channel — also used inline by the wizard (step « Who to tell »). */
export function ChannelEditor({ kinds, ch, onDone, onCreated }: {
  kinds: ChannelKindDef[]; ch?: Channel; onDone: () => void; onCreated?: (c: Channel) => void;
}) {
  const [kind, setKind] = useState<string>(ch?.kind || kinds[0]?.kind || "telegram");
  const [name, setName] = useState(ch?.name || "");
  const [enabled, setEnabled] = useState(ch?.enabled ?? true);
  const [cfg, setCfg] = useState<Record<string, string>>(() => {
    const o: Record<string, string> = {};
    for (const [k, v] of Object.entries(ch?.config || {})) if (typeof v === "string") o[k] = v;
    return o;
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const def = kinds.find((k) => k.kind === kind);
  const save = async () => {
    setBusy(true); setErr("");
    const config: Record<string, string> = {};
    for (const f of def?.fields || []) if (cfg[f.key] !== undefined && (cfg[f.key] !== "" || !f.secret)) config[f.key] = cfg[f.key];
    const body = { name: name.trim() || def?.label || kind, kind, config, enabled };
    try {
      if (ch) await updateChannel(ch.id, body);
      else { const c = await createChannel(body); onCreated?.(c); }
      onDone();
    } catch (e) { setErr(String((e as Error).message || e)); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!ch || !confirm(`Delete the channel « ${ch.name} »? Rules stop telling it.`)) return;
    try { await deleteChannel(ch.id); onDone(); } catch (e) { setErr(String((e as Error).message || e)); }
  };
  return (
    <div className="mt-2 space-y-2.5 border-t border-line pt-2.5">
      {!ch && (
        <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="channel type">
          {kinds.map((k) => (
            <button key={k.kind} type="button" role="radio" aria-checked={kind === k.kind} onClick={() => setKind(k.kind)}
              className={`ui-focus inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] ${kind === k.kind ? "border-sea bg-sea/15 text-slate-100" : "border-line text-mut hover:text-slate-200"}`}>
              <span aria-hidden>{CH_ICON[k.kind] || "•"}</span>{k.label}
            </button>
          ))}
        </div>
      )}
      <div className="grid gap-2.5 sm:grid-cols-2">
        <Field label="Name" htmlFor="ce-name"><input id="ce-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={`Ops ${def?.label || ""}`} className={inputCls} /></Field>
        {(def?.fields || []).map((f) => {
          const set = ch?.config?.[`${f.key}_set`] === true;
          return (
            <Field key={f.key} label={f.label} htmlFor={`ce-${f.key}`} hint={f.secret ? (set ? "set — leave empty to keep it" : "kept secret, never shown again") : CH_HINT[`${kind}.${f.key}`]}>
              <input id={`ce-${f.key}`} type={f.secret ? "password" : "text"} value={cfg[f.key] || ""} autoComplete={f.secret ? "new-password" : "off"}
                placeholder={f.secret && set ? "•••••• (unchanged)" : ""} onChange={(e) => setCfg({ ...cfg, [f.key]: e.target.value })}
                className={`${inputCls} ${f.secret || /url|id|key/.test(f.key) ? "font-mono" : ""}`} />
            </Field>
          );
        })}
      </div>
      <label className="flex items-center gap-2 text-[12px] text-slate-300"><input type="checkbox" className="accent-sky-500" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> enabled</label>
      {err && <Banner tone="error">{err}</Banner>}
      <div className="flex flex-wrap gap-2">
        <button type="button" className={btn.primary} onClick={save} disabled={busy}>{ch ? "Save" : "Add the channel"}</button>
        <button type="button" className={btn.small} onClick={onDone}>Cancel</button>
        {ch && <button type="button" className={`${btn.danger} ml-auto`} onClick={remove}>Delete</button>}
      </div>
    </div>
  );
}

const CH_HINT: Record<string, string> = {
  "telegram.chat_id": "a group or a person — add the bot to the group, then its id (starts with -100…)",
  "teams.channel_id": "a Teams channel mapped to this project (Setup › Organization › Teams)",
  "email.to": "comma-separated addresses",
  "webhook.url": "receives a JSON POST; signed with X-Sokkan-Signature when a secret is set",
};

// ---------------------------------------------------------------- silences

function Silences({ rules }: { rules: Rule[] }) {
  const [list, setList] = useState<Silence[] | null>(null);
  const [err, setErr] = useState("");
  const load = () => listSilences().then(setList).catch((e) => setErr(String(e.message || e)));
  useEffect(() => { load(); }, []);
  if (err) return <Banner tone="error">{err}</Banner>;
  if (!list) return <div className="text-[12px] text-mut">Loading…</div>;
  const name = (id: number | null) => (id === null ? "every rule" : rules.find((r) => r.id === id)?.name || `rule #${id}`);
  const active = list.filter((s) => s.active);
  return (
    <div className="space-y-2">
      <p className="text-[12px] text-mut">A silence keeps evaluating but tells nobody — for a deploy, a maintenance, a known issue. Silence an alert from its row (1 h, 4 h, until tomorrow).</p>
      {active.length === 0 && <div className="rounded-xl border border-dashed border-line p-4 text-center text-[12.5px] text-mut">No silence in force.</div>}
      {active.map((s) => (
        <div key={s.id} className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-panel2/40 p-2.5 text-[12.5px]">
          <span aria-hidden>⏸</span>
          <span className="text-slate-100">{name(s.rule_id)}</span>
          {Object.keys(s.matchers || {}).length > 0 && <span className="font-mono text-[11px] text-mut">{Object.entries(s.matchers).map(([k, v]) => `${k}=${v}`).join(" · ")}</span>}
          <span className="text-[11px] text-mut">until {fmtTime(s.ends_at, true)} ({since(Date.now() / 1000, s.ends_at)} left)</span>
          {s.reason && <span className="text-[11px] text-slate-300">« {s.reason} »</span>}
          {s.created_by && <span className="text-[11px] text-mut">by {s.created_by}</span>}
          <button type="button" className={`${btn.small} ml-auto`} onClick={() => deleteSilence(s.id).then(load).catch((e) => setErr(String(e.message || e)))}>End now</button>
        </div>
      ))}
    </div>
  );
}
