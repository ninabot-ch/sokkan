"use client";
// 3.5 — Operate › Alerts. The SOKKAN alerting brick: rules evaluated by the cockpit itself over your
// Prometheus, Loki, Elasticsearch / OpenSearch and its own figures — ElastAlert's rule types, set up
// in a guided page with a live backtest instead of a YAML file. « Event-driven ops, human-gated »:
// an alert can open an incident, start a diagnosis session or propose an agent run, nothing runs
// without a person's go-ahead.
//
// Deep links: /?plane=operate&tab=alerts[&rule=<id>|&alert=<id>|&view=new|settings]
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { runbooksList } from "@/lib/api";
import { useMe } from "@/lib/me";
import { rank } from "@/lib/me";
import {
  alertingStatus, listAgentsLite, listAlerts, listChannels, listRules, listSources, listTemplates,
} from "@/lib/alerting";
import {
  ago,
  valueUnit,
  emptyRule, fmtValue, fromTemplate, ruleState, since, sortAlerts, sortRules, toRuleIn,
  type Alert, type AlertCounts, type AlertingStatus, type AlertSource, type Channel, type ChannelKindDef, type Rule,
  type RuleCounts, type RuleIn, type Template,
} from "@/lib/alertingModel";
import { AlertRow } from "./AlertRow";
import RuleDetail from "./RuleDetail";
import Settings from "./Settings";
import Wizard from "./Wizard";
import { Banner, Seg, SeverityChip, Sparkline, StateChip, btn, inputCls } from "./bits";

type View =
  | { v: "overview" }
  | { v: "gallery" }
  | { v: "wizard"; start: RuleIn; ruleId?: number; template?: string | null }
  | { v: "rule"; id: number }
  | { v: "settings"; tab?: "sources" | "channels" | "silences" };

const setUrl = (p: Record<string, string | null>) => {
  if (typeof window === "undefined") return;
  const u = new URL(window.location.href);
  for (const k of ["rule", "alert", "view"]) u.searchParams.delete(k);
  for (const [k, v] of Object.entries(p)) if (v) u.searchParams.set(k, v);
  // a rule / alert / view in the URL is a deep link: it carries its plane and tab, else a reload or a
  // shared link (« ?project=platform&rule=1 », seen in the 09.10 journey) lands on the Board
  if (Object.values(p).some(Boolean)) { u.searchParams.set("plane", "operate"); u.searchParams.set("tab", "alerts"); }
  window.history.replaceState(null, "", u.toString());
};

export default function Alerts({ onOpenIncident }: { onOpenIncident?: (id: number) => void }) {
  const me = useMe();
  const canWrite = !me || rank(me.role) >= rank("dev");
  const canManage = !me || rank(me.role) >= rank("admin") || me.project_role === "maintainer" || me.project_role === "admin";

  const [view, setView] = useState<View>({ v: "overview" });
  const [focusAlert, setFocusAlert] = useState<number | null>(null);
  const [status, setStatus] = useState<AlertingStatus | null>(null);
  const [rules, setRules] = useState<Rule[] | null>(null);
  const [rCounts, setRCounts] = useState<RuleCounts | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [aCounts, setACounts] = useState<AlertCounts | null>(null);
  const [sources, setSources] = useState<AlertSource[]>([]);
  const [chan, setChan] = useState<{ channels: Channel[]; kinds: ChannelKindDef[] }>({ channels: [], kinds: [] });
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [agents, setAgents] = useState<{ id: number; name: string }[]>([]);
  const [runbooks, setRunbooks] = useState<{ name: string; description?: string }[]>([]);
  const [err, setErr] = useState("");

  const loadLive = useCallback(() => {
    listRules().then((x) => { setRules(x.rules); setRCounts(x.counts); setErr(""); }).catch((e) => setErr(String(e.message || e)));
    listAlerts("active").then((x) => { setAlerts(x.alerts); setACounts(x.counts); }).catch(() => {});
    alertingStatus().then(setStatus).catch(() => {});
  }, []);
  const loadChannels = useCallback(() => { listChannels().then(setChan).catch(() => {}); }, []);
  useEffect(() => {
    loadLive(); loadChannels();
    listSources().then(setSources).catch(() => {});
    listTemplates().then(setTemplates).catch(() => setTemplates([]));
    listAgentsLite().then(setAgents);
    runbooksList().then(setRunbooks).catch(() => {});
    const iv = setInterval(loadLive, 15000);
    return () => clearInterval(iv);
  }, [loadLive, loadChannels]);

  // deep link, once
  const linked = useRef(false);
  useEffect(() => {
    if (linked.current) return;
    linked.current = true;
    const q = new URLSearchParams(window.location.search);
    const rule = q.get("rule"); const alert = q.get("alert"); const v = q.get("view");
    if (rule && /^\d+$/.test(rule)) setView({ v: "rule", id: +rule });
    else if (alert && /^\d+$/.test(alert)) setFocusAlert(+alert);
    else if (v === "new") setView({ v: "gallery" });
    else if (v === "settings") setView({ v: "settings" });
  }, []);
  useEffect(() => {
    if (focusAlert && alerts.some((a) => a.id === focusAlert)) document.getElementById(`alert-${focusAlert}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focusAlert, alerts]);

  const go = (nv: View) => {
    setView(nv);
    setUrl(nv.v === "rule" ? { rule: String(nv.id) } : nv.v === "gallery" || (nv.v === "wizard" && !nv.ruleId) ? { view: "new" }
      : nv.v === "settings" ? { view: "settings" } : {});
    if (typeof window !== "undefined") document.querySelector("main")?.scrollTo?.({ top: 0 });
  };
  const newBlank = () => {
    const s = sources.find((x) => x.kind === "prometheus" && x.status?.ok !== false) ?? sources[0] ?? null;
    go({ v: "wizard", start: emptyRule(s) });
  };

  let body: React.ReactNode;
  if (view.v === "wizard") {
    body = <Wizard key={view.ruleId ?? "new"} start={view.start} ruleId={view.ruleId} sources={sources} channels={chan.channels} kinds={chan.kinds}
      agents={agents} runbooks={runbooks} canManage={canManage} fromTemplate={view.template}
      onChannelsChanged={loadChannels} onCancel={() => go(view.ruleId ? { v: "rule", id: view.ruleId } : { v: "overview" })}
      onSaved={(r) => { loadLive(); go({ v: "rule", id: r.id }); }} />;
  } else if (view.v === "rule") {
    body = <RuleDetail id={view.id} sources={sources} channels={chan.channels} alerts={alerts} canWrite={canWrite}
      onEdit={(r) => go({ v: "wizard", start: toRuleIn(r), ruleId: r.id })} onBack={() => go({ v: "overview" })} onChanged={loadLive} onOpenIncident={onOpenIncident} />;
  } else if (view.v === "settings") {
    body = <Settings rules={rules || []} canManage={canManage} initial={view.tab} onBack={() => { listSources().then(setSources).catch(() => {}); loadChannels(); go({ v: "overview" }); }} />;
  } else if (view.v === "gallery") {
    body = <Gallery templates={templates} sources={sources} onBack={() => go({ v: "overview" })} onBlank={newBlank}
      onPick={(t) => go({ v: "wizard", start: fromTemplate(t, sources), template: t.name })} />;
  } else {
    body = <Overview rules={rules} rCounts={rCounts} alerts={alerts} aCounts={aCounts} status={status} templates={templates} sources={sources}
      channels={chan.channels} canWrite={canWrite} canManage={canManage} err={err} focusAlert={focusAlert}
      onNew={() => go({ v: "gallery" })} onBlank={newBlank} onSettings={(tab) => go({ v: "settings", tab })}
      onOpenRule={(id) => go({ v: "rule", id })} onPick={(t) => go({ v: "wizard", start: fromTemplate(t, sources), template: t.name })}
      onChanged={loadLive} onOpenIncident={onOpenIncident} />;
  }
  return <div className="min-h-0 flex-1 overflow-y-auto p-3">{body}</div>;
}

// ---------------------------------------------------------------- overview

function Overview(p: {
  rules: Rule[] | null; rCounts: RuleCounts | null; alerts: Alert[]; aCounts: AlertCounts | null; status: AlertingStatus | null;
  templates: Template[] | null; sources: AlertSource[]; channels: Channel[]; canWrite: boolean; canManage: boolean; err: string; focusAlert: number | null;
  onNew: () => void; onBlank: () => void; onSettings: (tab?: "sources" | "channels" | "silences") => void; onOpenRule: (id: number) => void;
  onPick: (t: Template) => void; onChanged: () => void; onOpenIncident?: (id: number) => void;
}) {
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<"all" | "attention" | "off">("all");
  const active = sortAlerts(p.alerts);
  const firing = p.aCounts?.firing ?? active.filter((a) => a.state === "firing").length;
  const pending = p.aCounts?.pending ?? active.filter((a) => a.state === "pending").length;
  const rules = useMemo(() => {
    const s = q.trim().toLowerCase();
    return sortRules(p.rules || []).filter((r) => (!s || r.name.toLowerCase().includes(s) || (r.sentence || "").toLowerCase().includes(s))
      && (filter === "all" || (filter === "off" ? !r.enabled : ["firing", "pending", "error"].includes(ruleState(r)))));
  }, [p.rules, q, filter]);
  const ev = p.status?.evaluator;
  const brokenSources = p.sources.filter((s) => s.status?.ok === false);

  if (p.err && !p.rules) return <div className="mx-auto max-w-5xl"><Banner tone="error">{p.err}</Banner></div>;
  if (!p.rules) return <div className="p-4 text-[12px] text-mut">Loading…</div>;

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      {/* header */}
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-[15px] font-semibold text-slate-100">Alerts</h2>
        {ev?.last_tick && <span className="text-[11px] text-mut">· checked {ago(ev.last_tick)}{ev.errors ? ` · ${ev.errors} source error${ev.errors > 1 ? "s" : ""}` : ""}</span>}
        <span className="ml-auto flex gap-1.5">
          <button type="button" className={btn.ghost} onClick={() => p.onSettings()}>⚙ Channels & sources</button>
          {p.canWrite && <button type="button" className={btn.primary} onClick={p.onNew}>+ New alert</button>}
        </span>
      </div>

      {/* stat tiles — not before the first rule (four zeros above « No alert rule yet » say nothing) */}
      {p.rules.length > 0 && <div className="grid grid-cols-2 gap-2 sm:grid-cols-4" role="list" aria-label="summary">
        <Tile label="Firing" n={firing} icon="▲" tone={firing ? "text-red-300" : "text-slate-400"} />
        <Tile label="Pending" n={pending} icon="◔" tone={pending ? "text-amber-200" : "text-slate-400"} hint="condition true, waiting for « only if it lasts »" />
        <Tile label="Silenced" n={(p.aCounts?.silenced ?? 0) + (p.rCounts?.silenced ?? 0)} icon="‖" tone="text-slate-300" />
        <Tile label="Rules watching" n={(p.rCounts?.ok ?? 0) + (p.rCounts?.firing ?? 0) + (p.rCounts?.pending ?? 0)} icon="✓" tone="text-emerald-300"
          hint={p.rCounts?.disabled ? `${p.rCounts.disabled} off` : undefined} />
      </div>}

      {brokenSources.length > 0 && (
        <Banner tone="warn">
          {brokenSources.map((s) => s.name).join(", ")} {brokenSources.length > 1 ? "do" : "does"} not answer — rules on {brokenSources.length > 1 ? "them" : "it"} show « source error » and cannot fire.{" "}
          <button type="button" className="underline" onClick={() => p.onSettings("sources")}>Check the sources</button>
        </Banner>
      )}
      {p.rules.length > 0 && p.channels.length === 0 && (
        <Banner tone="info">No notification channel yet: alerts only show here{p.canManage ? <> — <button type="button" className="underline" onClick={() => p.onSettings("channels")}>add Telegram, Teams, Slack, e-mail or a webhook</button></> : " — ask a maintainer to add one"}.</Banner>
      )}

      {/* active alerts */}
      {p.rules.length > 0 && (
        <section aria-label="active alerts" className="space-y-1.5">
          <h3 className="text-[12px] font-semibold text-slate-200">Now</h3>
          {active.length === 0 ? (
            <div className="flex items-center gap-2 rounded-xl border border-emerald-500/25 bg-emerald-500/5 px-3 py-2.5 text-[12.5px] text-emerald-200">
              <span aria-hidden>✓</span> All quiet — nothing firing.
            </div>
          ) : active.map((a) => <AlertRow key={a.id} a={a} canWrite={p.canWrite} onChanged={p.onChanged} onOpenRule={p.onOpenRule} onOpenIncident={p.onOpenIncident} focus={a.id === p.focusAlert} />)}
        </section>
      )}

      {/* rules */}
      {p.rules.length === 0 ? (
        <EmptyState templates={p.templates} canWrite={p.canWrite} onPick={p.onPick} onBlank={p.onBlank} onAll={p.onNew} />
      ) : (
        <section aria-label="rules" className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[12px] font-semibold text-slate-200">Rules <span className="font-normal text-mut">({p.rules.length})</span></h3>
            <Seg label="filter rules" size="sm" value={filter} onChange={setFilter} opts={[{ id: "all", label: "All" }, { id: "attention", label: "Need attention" }, { id: "off", label: "Off" }]} />
            <input aria-label="search rules" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search…" className={`${inputCls} ml-auto w-44 py-1 text-[12px]`} />
          </div>
          <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line bg-panel/40">
            {rules.map((r) => {
              const st = ruleState(r);
              const unit = valueUnit(r.query);
              return (
                <li key={r.id}>
                  <button type="button" onClick={() => p.onOpenRule(r.id)}
                    className="ui-focus grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5 px-3 py-2 text-left hover:bg-panel2/60 sm:grid-cols-[6.5rem_minmax(0,1fr)_auto_5.5rem]">
                    <span className="row-span-2 sm:row-span-1"><StateChip s={st} n={r.state?.firing} /></span>
                    <span className="min-w-0">
                      <span className="flex items-center gap-1.5"><span className="truncate text-[13px] text-slate-100">{r.name}</span><SeverityChip s={r.severity} compact /></span>
                      <span className="block truncate text-[11px] text-mut">{st === "error" && r.state?.error ? `source error: ${r.state.error}` : r.sentence}</span>
                    </span>
                    <span className="hidden sm:block"><Sparkline pts={r.spark} state={st} /></span>
                    <span className="text-right text-[11.5px] tabular-nums text-slate-300">
                      {r.state?.last_value !== null && r.state?.last_value !== undefined ? fmtValue(r.state.last_value, unit) : <span className="text-mut">—</span>}
                    </span>
                  </button>
                </li>
              );
            })}
            {rules.length === 0 && <li className="px-3 py-3 text-[12px] text-mut">No rule matches.</li>}
          </ul>
        </section>
      )}
    </div>
  );
}

function Tile({ label, n, icon, tone, hint }: { label: string; n: number; icon: string; tone: string; hint?: string }) {
  return (
    <div role="listitem" className="rounded-xl border border-line bg-panel/50 px-3 py-2" title={hint}>
      <div className={`flex items-baseline gap-1.5 ${tone}`}><span aria-hidden className="text-[12px]">{icon}</span><span className="text-[22px] font-semibold tabular-nums leading-none">{n}</span></div>
      <div className="mt-0.5 text-[11px] text-mut">{label}{hint && label === "Rules watching" ? ` · ${hint}` : ""}</div>
    </div>
  );
}

// ---------------------------------------------------------------- empty state + template gallery

const CAT_ICON: Record<string, string> = { Web: "🌐", Hosts: "🖥", Logs: "📜", Certificates: "🔒", SOKKAN: "⎈", Agents: "✦" };

function TemplateCard({ t, onPick, disabled }: { t: Template; onPick: (t: Template) => void; disabled?: boolean }) {
  return (
    <button type="button" disabled={disabled} onClick={() => onPick(t)}
      className={`ui-focus flex h-full flex-col gap-1 rounded-xl border p-3 text-left ${t.available ? "border-line bg-panel2/40 hover:border-sea/50" : "border-dashed border-line bg-transparent hover:border-line"}`}>
      <span className="flex items-center gap-2">
        <span aria-hidden className="text-[15px]">{CAT_ICON[t.category] || "◇"}</span>
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-slate-100">{t.name}</span>
        {t.available ? <span className="ui-chip ui-c-ok"><span aria-hidden>✓</span>ready</span> : <span className="ui-chip ui-c-off">needs data</span>}
      </span>
      <span className="text-[11.5px] leading-snug text-mut">{t.description}</span>
      {!t.available && t.missing && <span className="text-[10.5px] text-amber-200/90">{t.missing}</span>}
    </button>
  );
}

function EmptyState({ templates, canWrite, onPick, onBlank, onAll }: {
  templates: Template[] | null; canWrite: boolean; onPick: (t: Template) => void; onBlank: () => void; onAll: () => void;
}) {
  const ready = (templates || []).filter((t) => t.available);
  return (
    <section aria-label="get started" className="rounded-2xl border border-line bg-panel/40 p-5">
      <h3 className="text-[15px] font-semibold text-slate-100">No alert rule yet</h3>
      <p className="mt-1 max-w-2xl text-[12.5px] text-mut">
        A rule watches your metrics or your logs and tells the right people when something goes wrong — then it can open an incident or start a diagnosis
        session. {ready.length > 0 ? <>Start from a template: <b className="text-slate-200">{ready.length} {ready.length === 1 ? "is" : "are"} ready for your stack</b>, the data is already there.</> : "Start from a template, or from scratch."}
      </p>
      {canWrite ? (
        <>
          {templates === null ? <div className="mt-3 text-[12px] text-mut">Loading templates…</div> : (
            <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {(ready.length ? ready : templates).slice(0, 6).map((t) => <TemplateCard key={t.id} t={t} onPick={onPick} />)}
            </div>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            {(templates || []).length > 6 && <button type="button" className={btn.ghost} onClick={onAll}>See all {templates!.length} templates</button>}
            <button type="button" className={btn.ghost} onClick={onBlank}>Start from scratch</button>
          </div>
        </>
      ) : <p className="mt-2 text-[12px] text-mut">You can read the alerts of this project; a developer or a maintainer creates the rules.</p>}
    </section>
  );
}

function Gallery({ templates, sources, onPick, onBlank, onBack }: {
  templates: Template[] | null; sources: AlertSource[]; onPick: (t: Template) => void; onBlank: () => void; onBack: () => void;
}) {
  const cats = useMemo(() => {
    const m = new Map<string, Template[]>();
    for (const t of templates || []) m.set(t.category, [...(m.get(t.category) || []), t]);
    for (const [k, v] of m) m.set(k, [...v].sort((a, b) => Number(b.available) - Number(a.available) || a.name.localeCompare(b.name)));
    return [...m.entries()];
  }, [templates]);
  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={onBack} className={btn.small}>← Alerts</button>
        <h2 className="text-[15px] font-semibold text-slate-100">New alert</h2>
        <span className="text-[12px] text-mut">pick a template — everything stays editable — or start from scratch</span>
      </div>
      <button type="button" onClick={onBlank}
        className="ui-focus flex w-full items-center gap-3 rounded-xl border border-dashed border-sea/50 bg-sea/5 p-3 text-left hover:bg-sea/10">
        <span aria-hidden className="text-[20px] text-sky-300">＋</span>
        <span><span className="block text-[13px] font-medium text-slate-100">Start from scratch</span>
          <span className="block text-[11.5px] text-mut">pick a source ({sources.map((s) => s.name).join(", ") || "none yet"}), describe what to watch, see the backtest</span></span>
      </button>
      {templates === null ? <div className="text-[12px] text-mut">Loading templates…</div> : cats.map(([cat, ts]) => (
        <section key={cat} aria-label={cat}>
          <h3 className="mb-1.5 flex items-center gap-1.5 text-[12px] font-semibold text-slate-200"><span aria-hidden>{CAT_ICON[cat] || "◇"}</span>{cat}</h3>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{ts.map((t) => <TemplateCard key={t.id} t={t} onPick={onPick} />)}</div>
        </section>
      ))}
    </div>
  );
}
