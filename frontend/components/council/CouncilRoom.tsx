"use client";
// Launch council — « Salle du Conseil » (feature `council`, experimental). One page, three
// views: the list of the project's councils, the opening form (brief, documents, ceiling,
// Claude choice) and the room: nine fixed tiles (initials + icon, never a generated face) with a
// « speaking » indicator that is also written, the discussion, the CEO's input, the budget meter
// and the agreement card. State is readable at a glance; `prefers-reduced-motion` stops the pulse.
import { useCallback, useEffect, useRef, useState } from "react";
import { useT, useUI } from "@/lib/simple/i18n";
import type { Key } from "@/lib/simple/dict";
import Icon from "@/components/simple/icons";
import {
  actions, fmtChf, fmtTokens, groupRounds, isLive, isOver, mergeTurns, meter, tileStatus,
  type Council, type RoleInfo, type Turn,
} from "@/lib/council";
import * as api from "@/lib/councilApi";

export default function CouncilRoom() {
  const t = useT();
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide co-page">
      <div>
        <h1 className="sk-h1">{t("council.title")} <span className="sk-pill sk-pill-idle">{t("council.exp")}</span></h1>
        <p className="sk-lede">{t("council.lede")}</p>
      </div>
      {open ? <Room id={open} onBack={() => setOpen(null)} />
        : creating ? <NewCouncil onCancel={() => setCreating(false)} onOpened={(id) => { setCreating(false); setOpen(id); }} />
        : <List onNew={() => setCreating(true)} onOpen={setOpen} />}
    </div></div>
  );
}

function List({ onNew, onOpen }: { onNew: () => void; onOpen: (id: string) => void }) {
  const t = useT();
  const [rows, setRows] = useState<Council[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => { api.listCouncils().then((r) => setRows(r.councils)).catch(() => setFailed(true)); }, []);
  return (
    <>
      <div><button className="sk-btn sk-btn-primary" onClick={onNew}><Icon name="plus" />{t("council.new")}</button></div>
      {failed ? <div className="sk-empty" role="alert"><Icon name="alert" /><p>{t("council.load.failed")}</p></div>
        : rows === null ? <div className="sk-list" aria-busy="true" aria-label={t("loading")}><div className="sk-skel" /></div>
        : rows.length === 0 ? <div className="sk-empty"><Icon name="crew" /><p>{t("council.list.empty")}</p></div>
        : (
          <div><h2 className="sk-h2">{t("council.list.title")}</h2>
            <ul className="sk-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {rows.map((c) => (
                <li key={c.id}><button className="sk-card sk-card-btn" onClick={() => onOpen(c.id)}>
                  <span className="sk-card-title">{c.title}</span>
                  <span className="co-state" data-state={c.state}>{t(`council.state.${c.state}` as Key)}</span>
                </button></li>
              ))}
            </ul></div>
        )}
    </>
  );
}

function NewCouncil({ onCancel, onOpened }: { onCancel: () => void; onOpened: (id: string) => void }) {
  const t = useT();
  const { lang } = useUI();
  const [cfg, setCfg] = useState<api.CouncilConfig | null>(null);
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [docs, setDocs] = useState<{ name: string; text: string }[]>([]);
  const [dn, setDn] = useState(""); const [dt, setDt] = useState("");
  const [claude, setClaude] = useState(false);
  const [pii, setPii] = useState<Record<string, number> | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.fetchConfig(lang).then(setCfg).catch(() => setErr(t("council.load.failed"))); }, [lang, t]);
  const submit = async (ack: boolean, useClaude: boolean) => {
    setBusy(true); setErr("");
    try {
      const c = await api.openCouncil({ title, brief, lang, claude: useClaude, pii_ack: ack, documents: docs });
      onOpened(c.id);
    } catch (e) {
      if (e instanceof api.CouncilApiError && e.code === "claude_pii") setPii(e.found ?? {});
      else setErr(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  return (
    <form className="co-form" onSubmit={(e) => { e.preventDefault(); void submit(false, claude); }}>
      <label className="co-field"><span>{t("council.field.title")}</span>
        <input className="sk-input" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="co-field"><span>{t("council.field.brief")}</span>
        <textarea className="sk-input" rows={6} required minLength={10} value={brief} placeholder={t("council.field.brief.ph")}
          onChange={(e) => setBrief(e.target.value)} /></label>
      <fieldset className="co-field"><legend>{t("council.field.docs")}</legend>
        {docs.length === 0 ? <p className="sk-muted">{t("council.docs.none")}</p>
          : <ul>{docs.map((d, i) => <li key={i}>{d.name}</li>)}</ul>}
        <input className="sk-input" aria-label={t("council.docs.name")} placeholder={t("council.docs.name")} value={dn} onChange={(e) => setDn(e.target.value)} />
        <textarea className="sk-input" rows={3} aria-label={t("council.docs.text")} placeholder={t("council.docs.text")} value={dt} onChange={(e) => setDt(e.target.value)} />
        <button type="button" className="sk-btn" disabled={!dt.trim()} onClick={() => { setDocs([...docs, { name: dn.trim() || "document", text: dt }]); setDn(""); setDt(""); }}>
          <Icon name="clip" />{t("council.docs.attach")}</button>
      </fieldset>
      {cfg && (
        <p className="sk-muted">{t("council.budget")} : {t("council.budget.tokens", { used: "0", max: fmtTokens(cfg.defaults.max_tokens) })} · {fmtChf(cfg.defaults.max_cost_chf)}</p>
      )}
      {cfg?.claude.allowed ? (
        <label className="co-check"><input type="checkbox" checked={claude} onChange={(e) => setClaude(e.target.checked)} />
          <span>{t("council.claude")}<small>{t("council.claude.why")}</small></span></label>
      ) : cfg ? <p className="sk-muted" data-testid="claude-off">{cfg.claude.reason}</p> : null}
      {pii && (
        <div className="sk-card" role="alert">
          <p>{t("council.pii.found", { found: Object.entries(pii).map(([k, n]) => `${k} ×${n}`).join(", ") })}</p>
          <button type="button" className="sk-btn sk-btn-primary" onClick={() => void submit(true, true)}>{t("council.pii.anonymize")}</button>{" "}
          <button type="button" className="sk-btn" onClick={() => { setClaude(false); setPii(null); void submit(false, false); }}>{t("council.pii.local")}</button>
        </div>
      )}
      {err && <p role="alert" className="co-err">{err}</p>}
      <div><button className="sk-btn sk-btn-primary" disabled={busy || brief.trim().length < 10}>{t("council.open")}</button>{" "}
        <button type="button" className="sk-btn sk-btn-quiet" onClick={onCancel}>{t("cancel")}</button></div>
    </form>
  );
}

function Room({ id, onBack }: { id: string; onBack: () => void }) {
  const t = useT();
  const { lang } = useUI();
  const [c, setC] = useState<Council | null>(null);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [refusing, setRefusing] = useState(false);
  const [reason, setReason] = useState("");
  const last = useRef(0);
  const poll = useCallback(async () => {
    try {
      const r = await api.fetchCouncil(id, last.current, lang);
      setC(r.council); setRoles(r.roles);
      if (r.journal.length) { setTurns((h) => mergeTurns(h, r.journal)); last.current = Math.max(last.current, ...r.journal.map((x) => x.id)); }
    } catch { setErr(t("council.load.failed")); }
  }, [id, lang, t]);
  useEffect(() => { void poll(); }, [poll]);
  useEffect(() => {
    if (c && !isOver(c.state)) { const h = window.setInterval(poll, isLive(c.state) ? 1500 : 5000); return () => window.clearInterval(h); }
  }, [c, poll]);
  if (!c) return err ? <p role="alert">{err}</p> : <div className="sk-list" aria-busy="true"><div className="sk-skel" /></div>;
  const a = actions(c.state);
  const act = async (f: () => Promise<unknown>) => { setErr(""); try { await f(); await poll(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } };
  const name = (rid: string) => roles.find((r) => r.id === rid)?.label ?? (rid === "ceo" ? t("council.ceo") : t("council.system"));
  const m = meter(c.budget);
  return (
    <>
      <div className="co-head">
        <button className="sk-btn sk-btn-quiet" onClick={onBack}><Icon name="back" />{t("council.back")}</button>
        <h2 className="sk-h2">{c.title}</h2>
        <span className="co-state" data-state={c.state}>{t(`council.state.${c.state}` as Key)}</span>
      </div>
      <p className="sk-muted co-brief">{c.brief}</p>

      <section aria-label={t("council.tiles")}>
        <ul className="co-grid">
          {roles.map((r) => {
            const s = tileStatus(r.id, c, turns);
            return (
              <li key={r.id} className="co-tile" data-status={s} style={{ "--co-hue": r.avatar.hue } as React.CSSProperties}
                aria-label={t("council.avatar", { name: r.label, status: t(`council.tile.${s}` as Key) })}>
                <span className="co-avatar" aria-hidden>{r.avatar.initials}</span>
                <span className="co-name">{r.short}</span>
                <span className="co-status">{s === "speaking" && <span className="co-dot" aria-hidden />}{t(`council.tile.${s}` as Key)}</span>
              </li>
            );
          })}
        </ul>
      </section>

      <div className="co-meter" role="group" aria-label={t("council.budget.label", { pct: m.pct })}>
        <div className="co-bar" data-level={m.level}><span style={{ width: `${m.pct}%` }} /></div>
        <span>{t("council.budget.label", { pct: m.pct })} · {t("council.budget.tokens", { used: fmtTokens(c.budget.tokens), max: fmtTokens(c.budget.max_tokens) })}
          {" · "}{fmtChf(c.budget.cost_chf)} / {fmtChf(c.budget.max_cost_chf)}</span>
      </div>
      {c.state === "stopped_budget" && <p role="alert" className="co-err">{c.stop_message || t("council.budget.stopped")}</p>}
      {c.state === "failed" && <p role="alert" className="co-err">{t("council.failed", { why: c.stop_message })}</p>}

      <section aria-label={t("council.thread")} aria-live="polite" className="co-thread">
        {groupRounds(turns).map((g) => (
          <div key={g.round}>
            <h3 className="co-round">{t(`council.round.${Math.min(g.round, 3)}` as Key)}</h3>
            {g.turns.map((x) => (
              <article key={x.id} className="co-turn" data-kind={x.kind}>
                <header><strong>{name(x.role)}</strong>{x.addressed_to && x.kind === "reply" ? <span> {t("council.to", { to: x.addressed_to })}</span> : null}
                  {x.model && <small> · {x.model} · {x.tokens_in + x.tokens_out} tok · {x.ms} ms</small>}</header>
                <p>{x.kind === "synthesis" ? t("council.round.3") : x.text}</p>
              </article>
            ))}
          </div>
        ))}
      </section>

      {a.say && (
        <form className="co-say" onSubmit={(e) => { e.preventDefault(); const text = msg; setMsg(""); void act(() => api.say(id, text)); }}>
          <input className="sk-input" value={msg} maxLength={4000} placeholder={t("council.say.ph")} aria-label={t("council.say.ph")} onChange={(e) => setMsg(e.target.value)} />
          <button className="sk-btn" disabled={!msg.trim()}><Icon name="send" />{t("council.say.send")}</button>
        </form>
      )}
      <div className="co-actions">
        {a.start && <button className="sk-btn sk-btn-primary" onClick={() => void act(() => api.startCouncil(id))}>{t("council.start")}</button>}
        {a.stop && c.state !== "brief" && c.state !== "card" && <button className="sk-btn" onClick={() => void act(() => api.stopCouncil(id))}><Icon name="stop" />{t("council.stop")}</button>}
        <details className="co-export"><summary className="sk-btn sk-btn-quiet">{t("council.export")}</summary>
          <a href={api.exportUrl(id, "json")}>{t("council.export.json")}</a> · <a href={api.exportUrl(id, "md")}>{t("council.export.md")}</a></details>
      </div>
      {err && <p role="alert" className="co-err">{err}</p>}

      {c.card && (
        <section className="sk-card sk-card-approval co-card" aria-label={t("council.card.title")}>
          <h3 className="sk-card-title">{t("council.card.title")} · {t("council.card.v", { n: c.card.version })}</h3>
          <p className="sk-muted">{c.card.where}</p>
          <dl className="co-facts">{c.card.facts.map((f) => <div key={f.label}><dt>{f.label}</dt><dd>{f.value}</dd></div>)}</dl>
          <pre className="sk-pre">{c.card.preview}</pre>
          <p>{c.card.after}</p>
          {a.decide && (refusing ? (
            <div><label className="co-field"><span>{t("council.card.reason")}</span>
              <textarea className="sk-input" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
              <button className="sk-btn sk-btn-primary" disabled={reason.trim().length < 3}
                onClick={() => void act(async () => { await api.refuseCard(id, reason); setRefusing(false); setReason(""); })}>{t("council.card.reason.send")}</button></div>
          ) : (
            <div className="co-actions">
              <button className="sk-btn sk-btn-primary" onClick={() => void act(() => api.acceptCard(id))}><Icon name="check" />{t("council.card.accept")}</button>
              <button className="sk-btn" onClick={() => setRefusing(true)}>{t("council.card.refuse")}</button>
            </div>
          ))}
        </section>
      )}
      {c.accepted && (
        <section className="sk-card" role="status">
          <p>{t("council.accepted", { k: c.accepted.cards.length, a: c.accepted.agents.length, h: c.positions.length })}</p>
          {c.positions.length > 0 && <><h3 className="sk-h2">{t("council.positions")}</h3>
            <ul>{c.positions.map((p, i) => <li key={i}>{p.profil} — {p.pourcentage_poste} %</li>)}</ul></>}
        </section>
      )}
    </>
  );
}
