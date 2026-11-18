"use client";
// 4.0.1 — the approval of a send / an event / a deposit asked by the agent of THIS work, in the
// thread itself: it was only in Settings › Connectors, so the person did not see that the mail
// was waiting for them. Same card, same decision (the API, never the model); Settings keeps the
// list of every card of the person.
import { useCallback, useEffect, useState } from "react";
import { fetchActions, type ConnectorAction } from "@/lib/simple/connectors";
import { useT } from "@/lib/simple/i18n";
import { Card } from "./Connectors";
import Icon from "./icons";

export default function ConnectorCards({ sid, tick, onWaiting }: { sid: string; tick: number; onWaiting?: (n: number) => void }) {
  const t = useT();
  const [cards, setCards] = useState<ConnectorAction[]>([]);
  const [off, setOff] = useState(false);
  const [msg, setMsg] = useState("");
  const load = useCallback(async () => {
    try {
      const mine = (await fetchActions("pending")).actions.filter((a) => a.session_id === sid);
      setCards(mine);
      onWaiting?.(mine.length);
    } catch { setOff(true); }        // the feature is off (404) or the person has no connector
  }, [sid, onWaiting]);
  useEffect(() => { if (!off) load(); }, [load, tick, off]);
  useEffect(() => {
    if (off || cards.length === 0) return;
    const id = window.setInterval(load, 5000);   // decided elsewhere (Settings, Teams): it goes
    return () => window.clearInterval(id);
  }, [off, cards.length, load]);
  if (off || (cards.length === 0 && !msg)) return null;
  return (
    <div className="sk-conn-thread">
      {cards.map((a) => (
        <section key={a.id} className="sk-approval" aria-label={t("conn.thread.title")}>
          <div className="sk-approval-eyebrow"><Icon name="shield" small />{t("ap.eyebrow")}</div>
          <p className="sk-approval-note">{t("conn.thread.note")}</p>
          <Card a={a} onDone={(m) => { setMsg(m); load(); }} onError={() => setMsg(t("conn.failed"))} />
        </section>
      ))}
      {msg && <p className="sk-small sk-muted" role="status">{msg}</p>}
    </div>
  );
}
