"use client";
// Simple profile — Nina, the way in for someone who does not know where to start: « your guide
// to SOKKAN », not « your DevOps engineer ». Same endpoints as the technical panel
// (/api/assistant/history, /api/assistant/chat/stream); the wording, the examples and the look
// are the simple profile's.
import { useEffect, useRef, useState } from "react";
import { useT } from "@/lib/simple/i18n";
import Icon from "./icons";
import Markdown from "./Markdown";

interface Msg { role: string; content: string }

export default function Nina({ onClose }: { onClose: () => void }) {
  const t = useT();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    fetch("/api/assistant/history", { cache: "no-store" }).then((r) => (r.ok ? r.json() : []))
      .then((h: Msg[]) => setMsgs((m) => [...(Array.isArray(h) ? h : []), ...m])).catch(() => {});
  }, []);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [msgs, busy]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const ask = async (text: string) => {
    const q = text.trim();
    if (!q || busy) return;
    setInput(""); setErr(false); setBusy(true);
    setMsgs((m) => [...m, { role: "user", content: q }, { role: "assistant", content: "" }]);
    try {
      const r = await fetch("/api/assistant/chat/stream", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ message: q }) });
      if (!r.ok || !r.body) throw new Error(String(r.status));
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "", acc = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const p of parts) {
          const ev = /^event: (\w+)/m.exec(p)?.[1];
          const data = /^data: (.*)$/m.exec(p)?.[1];
          if (!ev || !data) continue;
          const payload = JSON.parse(data);
          if (ev === "delta") acc += payload.text || "";
          else if (ev === "done") acc = payload.text || acc;
          else if (ev === "error") throw new Error(payload.detail || "error");
          setMsgs((m) => [...m.slice(0, -1), { role: "assistant", content: acc }]);
        }
      }
    } catch {
      setErr(true);
      setMsgs((m) => (m[m.length - 1]?.content ? m : m.slice(0, -1)));
    } finally { setBusy(false); }
  };

  return (
    <aside className="sk-panel" aria-label={t("nina.title")} style={{ position: "absolute", inset: "0 0 0 auto", zIndex: 30, boxShadow: "var(--sk-shadow-lift)" }}>
      <div className="sk-panel-head">
        <span className="sk-avatar" aria-hidden>N</span>
        <div className="sk-grow"><div className="sk-h3">{t("nina.title")}</div><div className="sk-small sk-muted">{t("nina.tagline")}</div></div>
        <button className="sk-btn sk-btn-quiet sk-btn-icon" onClick={onClose} aria-label={t("close")}><Icon name="close" /></button>
      </div>
      <div className="sk-panel-body" tabIndex={0}>
        <div className="sk-nina-list" aria-live="polite">
          {msgs.length === 0 && (
            <>
              <p>{t("nina.hello")}</p>
              {(["nina.ex1", "nina.ex2", "nina.ex3"] as const).map((k) => (
                <button key={k} className="sk-suggest" onClick={() => ask(t(k))}>{t(k)}</button>
              ))}
            </>
          )}
          {msgs.map((m, i) => m.role === "user"
            ? <div key={i} className="sk-nina-msg is-me">{m.content}</div>
            : <div key={i} className="sk-nina-msg is-nina">{m.content ? <Markdown text={m.content} /> : <span className="sk-working"><span className="sk-spin" aria-hidden />{t("nina.thinking")}</span>}</div>)}
          {err && <div className="sk-error" role="alert"><Icon name="alert" small /><span>{t("nina.error")}</span></div>}
          <div ref={end} />
        </div>
      </div>
      <form className="sk-panel-foot" onSubmit={(e) => { e.preventDefault(); ask(input); }}>
        <label className="sk-visually-hidden" htmlFor="sk-nina-q">{t("nina.placeholder")}</label>
        <input id="sk-nina-q" className="sk-input sk-grow" value={input} onChange={(e) => setInput(e.target.value)} placeholder={t("nina.placeholder")} autoFocus />
        <button className="sk-btn sk-btn-primary sk-btn-icon" disabled={busy || !input.trim()} aria-label={t("nina.send")}><Icon name="send" /></button>
      </form>
    </aside>
  );
}
