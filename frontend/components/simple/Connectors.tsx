"use client";
// 4.0 — Settings › Connectors: the person connects their mailbox, calendar and shared documents
// (and revokes them), and decides the approval cards — nothing is sent, created or deposited
// before a click here. The instance administrator turns providers on and opens shared
// mailboxes / folders to projects. Credentials are typed once and never shown back.
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/simple/api";
import { connectLogin, decideAction, fetchActions, fetchConnectors, revokeConnector, saveConnectorsSettings, startGraph,
  type ConnectorAction, type ConnectorsOverview, type Provider } from "@/lib/simple/connectors";
import { useT } from "@/lib/simple/i18n";
import type { Key } from "@/lib/simple/dict";
import Icon from "./icons";

const LOGIN_PROVIDERS: Provider[] = ["imap", "caldav", "webdav"];

export default function Connectors() {
  const t = useT();
  const [ov, setOv] = useState<ConnectorsOverview | null>(null);
  const [cards, setCards] = useState<ConnectorAction[]>([]);
  const [msg, setMsg] = useState("");
  const load = useCallback(async () => {
    try { setOv(await fetchConnectors()); setCards((await fetchActions("pending")).actions); } catch { setOv(null); }
  }, []);
  useEffect(() => { load(); }, [load]);
  if (!ov) return null;
  const say = (e: unknown) => setMsg(e instanceof ApiError ? e.message : t("conn.failed"));
  const enabled = new Set(Object.values(ov.me.families).flat().map((f) => f.provider));
  const connected = new Set(ov.accounts.map((a) => a.provider));
  return (
    <fieldset className="sk-claude-set">
      <legend className="sk-legend">{t("conn.title")}</legend>
      <p className="sk-small sk-muted">{t("conn.hint")}</p>
      {!ov.me.content_allowed && <div className="sk-notice"><Icon name="info" /><span>{ov.me.reason}</span></div>}
      {cards.length > 0 && (
        <div className="sk-list" aria-label={t("conn.cards")}>
          <div className="sk-small"><strong>{t("conn.cards")}</strong></div>
          {cards.map((a) => <Card key={a.id} a={a} onDone={(m) => { setMsg(m); load(); }} onError={say} />)}
        </div>
      )}
      {(["graph", ...LOGIN_PROVIDERS, "demo"] as Provider[]).filter((p) => enabled.has(p)).map((p) => (
        <Account key={p} provider={p} connected={connected.has(p)}
          login={ov.accounts.find((a) => a.provider === p)?.login || ""} onChange={load} onError={say} />
      ))}
      {ov.is_admin && ov.settings && <Admin ov={ov} onSaved={load} onError={say} />}
      {msg && <div className="sk-small" role="status">{msg}</div>}
    </fieldset>
  );
}

function Card({ a, onDone, onError }: { a: ConnectorAction; onDone: (m: string) => void; onError: (e: unknown) => void }) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const p = a.preview as Record<string, string | string[]>;
  const decide = async (ok: boolean) => {
    setBusy(true);
    try { await decideAction(a.id, ok); onDone(t(ok ? "conn.card.done" : "conn.card.rejected")); }
    catch (e) { onError(e); onDone(""); } finally { setBusy(false); }
  };
  return (
    <div className="sk-card-approval">
      <div><strong>{a.title}</strong></div>
      {a.kind === "mail.send" && <div className="sk-small">{t("conn.to")} {(p.to as string[] || []).join(", ")}{(p.cc as string[] || []).length ? ` · cc ${(p.cc as string[]).join(", ")}` : ""}</div>}
      {a.kind === "calendar.create" && <div className="sk-small">{String(p.start)} → {String(p.end)} · {(p.attendees as string[] || []).join(", ")}</div>}
      {a.kind === "files.write" && <div className="sk-small">{String(p.path)} · {String(p.size)} o</div>}
      <pre className="sk-small sk-pre">{String(p.body ?? p.excerpt ?? "").slice(0, 1500)}</pre>
      <div className="sk-row">
        <button className="sk-btn" disabled={busy} onClick={() => decide(true)}><Icon name="check" />{t(`conn.approve.${a.kind}` as Key)}</button>
        <button className="sk-btn sk-btn-quiet" disabled={busy} onClick={() => decide(false)}>{t("conn.reject")}</button>
      </div>
    </div>
  );
}

function Account({ provider, connected, login, onChange, onError }: {
  provider: Provider; connected: boolean; login: string; onChange: () => void; onError: (e: unknown) => void;
}) {
  const t = useT();
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (f: () => Promise<unknown>) => {
    setBusy(true);
    try { await f(); setPw(""); onChange(); } catch (e) { onError(e); } finally { setBusy(false); }
  };
  return (
    <div className="sk-list">
      <div className="sk-small"><strong>{t(`conn.p.${provider}` as Key)}</strong>
        {connected ? ` · ${t("conn.connected")}${login ? ` (${login})` : ""}` : ""}</div>
      {connected ? (
        <div><button className="sk-btn sk-btn-quiet" disabled={busy} onClick={() => run(() => revokeConnector(provider))}>{t("conn.revoke")}</button></div>
      ) : provider === "graph" ? (
        <div><button className="sk-btn" disabled={busy} onClick={() => run(async () => { window.location.href = (await startGraph()).url; })}>{t("conn.consent")}</button></div>
      ) : provider === "demo" ? (
        <div><button className="sk-btn" disabled={busy} onClick={() => run(() => connectLogin("demo", "", ""))}>{t("conn.demo")}</button></div>
      ) : (
        <>
          <input type="text" autoComplete="username" placeholder={t("conn.login")} aria-label={t("conn.login")} value={user} onChange={(e) => setUser(e.target.value)} />
          <input type="password" autoComplete="off" placeholder={t("conn.password")} aria-label={t("conn.password")} value={pw} onChange={(e) => setPw(e.target.value)} />
          <div><button className="sk-btn" disabled={busy || !pw} onClick={() => run(() => connectLogin(provider, user.trim(), pw))}>{t("conn.connect")}</button></div>
        </>
      )}
    </div>
  );
}

function Admin({ ov, onSaved, onError }: { ov: ConnectorsOverview; onSaved: () => void; onError: (e: unknown) => void }) {
  const t = useT();
  const s = ov.settings!;
  const [secret, setSecret] = useState("");
  const toggle = async (p: string, on: boolean) => {
    if (!on && !window.confirm(t("conn.admin.off"))) return;
    try { await saveConnectorsSettings({ providers: { [p]: { enabled: on } } }); onSaved(); } catch (e) { onError(e); }
  };
  return (
    <div className="sk-list">
      <div className="sk-small"><strong>{t("conn.admin")}</strong></div>
      {Object.keys(s.providers).map((p) => (
        <label key={p} className="sk-small"><input type="checkbox" checked={!!s.providers[p].enabled} onChange={(e) => toggle(p, e.target.checked)} /> {t(`conn.p.${p}` as Key)}</label>
      ))}
      <label className="sk-small"><input type="checkbox" checked={s.allow_send}
        onChange={async (e) => { try { await saveConnectorsSettings({ allow_send: e.target.checked }); onSaved(); } catch (x) { onError(x); } }} /> {t("conn.admin.send")}</label>
      {!!s.providers.graph?.enabled && (
        <>
          <input type="text" placeholder="Tenant ID" aria-label="Tenant ID" defaultValue={String(s.providers.graph.tenant || "")}
            onBlur={async (e) => { try { await saveConnectorsSettings({ providers: { graph: { tenant: e.target.value.trim() } } }); } catch (x) { onError(x); } }} />
          <input type="text" placeholder="Client ID" aria-label="Client ID" defaultValue={String(s.providers.graph.client_id || "")}
            onBlur={async (e) => { try { await saveConnectorsSettings({ providers: { graph: { client_id: e.target.value.trim() } } }); } catch (x) { onError(x); } }} />
          <input type="password" autoComplete="off" placeholder={s.graph_client_secret_set ? t("conn.admin.secret.set") : t("conn.admin.secret")}
            aria-label={t("conn.admin.secret")} value={secret} onChange={(e) => setSecret(e.target.value)} />
          <div><button className="sk-btn" disabled={!secret} onClick={async () => { try { await saveConnectorsSettings({ graph_client_secret: secret }); setSecret(""); onSaved(); } catch (x) { onError(x); } }}>{t("conn.admin.save")}</button></div>
        </>
      )}
      <p className="sk-small sk-muted">{t("conn.admin.more")}</p>
    </div>
  );
}
