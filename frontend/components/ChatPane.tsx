"use client";
import { useEffect, useRef, useState } from "react";
import { fetchBindings, fetchLive, fetchSession, sendInput, sendKey } from "@/lib/api";
import type { Binding, LiveState, SessionDetail } from "@/lib/types";
import ChatMessage from "./ChatMessage";
import { useCan } from "@/lib/me";

const STATE_LABEL: Record<string, string> = {
  booting: "démarrage de la session…",
  working: "Claude travaille…",
  awaiting: "Claude attend une réponse",
  idle: "prête",
  dead: "fenêtre fermée",
};

export default function ChatPane({
  id,
  onClose,
}: {
  id: string;
  onClose: (id: string) => void;
}) {
  const [data, setData] = useState<SessionDetail | null>(null);
  const [live, setLive] = useState<LiveState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [bound, setBound] = useState<string | null>(null); // tmux target "sess:win" lié à CE pane
  const [dead, setDead] = useState(false); // session connue mais fenêtre tmux fermée
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [sendErr, setSendErr] = useState<string | null>(null);
  const [view, setView] = useState<"chat" | "term">("chat");
  const [showMirror, setShowMirror] = useState(false);
  const [pending, setPending] = useState<string[]>([]); // prompts envoyés, pas encore dans le transcript
  const canWrite = useCan("dev");
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const d = await fetchSession(id);
        if (alive) { setData(d); setErr(null); }
      } catch (e) {
        if (alive) setErr(String(e));
      }
    };
    load();
    const iv = setInterval(load, 3000);
    return () => { alive = false; clearInterval(iv); };
  }, [id]);

  // signe de vie temps-réel depuis le pane tmux (plus rapide que le transcript)
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const l = await fetchLive(id);
        if (alive) setLive(l);
      } catch { /* keep */ }
    };
    tick();
    const iv = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(iv); };
  }, [id]);

  // binding fenêtre tmux ↔ cette session (work-layout après relaunch, ou carte spawnée)
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const bs = await fetchBindings();
        if (!alive) return;
        const b = bs.find((x: Binding) => x.session_id === id);
        setBound(b && b.alive ? b.target : null);
        setDead(!!b && !b.alive);
      } catch { /* keep */ }
    };
    load();
    const iv = setInterval(load, 15000);
    return () => { alive = false; clearInterval(iv); };
  }, [id]);

  useEffect(() => {
    if (stick.current && scrollRef.current)
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [data?.n_messages, pending.length]);

  // réconcilie : un prompt en file disparaît dès qu'il apparaît dans le transcript
  useEffect(() => {
    if (!data) return;
    setPending((p) =>
      p.filter((t) => !data.messages.some(
        (m) => m.role === "user" && m.kind === "text" && (m.text || "").trim() === t.trim()
      ))
    );
  }, [data]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const doSend = async () => {
    if (!text.trim() || !bound) return;
    const msg = text;
    setSending(true); setSendErr(null);
    try {
      await sendInput(bound, msg);
      setText("");
      setPending((p) => [...p, msg]);
      stick.current = true;
    } catch (e) {
      setSendErr(String(e));
    } finally {
      setSending(false);
    }
  };

  const press = async (key: string) => {
    try { await sendKey(id, key); } catch (e) { setSendErr(String(e)); }
  };

  const st = live?.state;
  const working = st === "working";
  const awaiting = st === "awaiting";

  return (
    <div className="flex min-h-0 flex-col rounded-xl border border-line bg-panel">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span
          className={`h-2.5 w-2.5 shrink-0 rounded-full ${
            working ? "animate-pulse bg-emerald-400"
              : awaiting ? "animate-pulse bg-amber-400"
              : data?.active ? "bg-emerald-500" : "bg-slate-600"
          }`}
          title={st ? STATE_LABEL[st] : data?.active ? "active" : "idle"}
        />
        <div className="min-w-0">
          <div className="truncate text-[13px] font-medium text-slate-100">
            {data?.title || id.slice(0, 8)}
          </div>
          <div className="truncate text-[10.5px] text-mut">
            {data?.git_branch} · {data?.n_messages ?? "…"} msg
            {st && st !== "idle" && <span className="text-brass"> · {STATE_LABEL[st]}</span>}
            {bound && <span className="text-brass"> · {bound}</span>}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <div className="flex overflow-hidden rounded-md border border-line text-[11px]">
            {(["chat", "term"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-2 py-0.5 ${view === v ? "bg-panel2 text-slate-100" : "text-mut hover:text-slate-200"}`}
              >{v === "chat" ? "Chat" : "Terminal"}</button>
            ))}
          </div>
          <button
            onClick={() => onClose(id)}
            className="rounded px-1.5 text-mut hover:bg-panel2 hover:text-slate-200"
            title="fermer"
          >✕</button>
        </div>
      </header>

      {view === "chat" ? (
        <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {err && <div className="text-[12px] text-red-400">{err}</div>}
          {!data && !err && <div className="text-[12px] text-mut">chargement…</div>}
          {data && data.messages.length === 0 && pending.length === 0 && !err && (
            <div className="mt-6 text-center text-[12px] text-mut">
              {st === "booting" || data.starting
                ? "démarrage de la session…"
                : "session prête — écris un message ci-dessous"}
            </div>
          )}
          {data?.messages.map((m, i) => <ChatMessage key={i} m={m} />)}
          {pending.map((t, i) => (
            <div key={`p${i}`} className="my-2 flex justify-end">
              <div className="max-w-[88%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-sea/10 px-3 py-2 text-[13px] text-slate-300 ring-1 ring-sea/20">
                {t}
                <span className="ml-1.5 align-middle text-[10px] text-mut">⏳ en file</span>
              </div>
            </div>
          ))}
        </div>
      ) : bound ? (
        <iframe
          title={`terminal ${bound}`}
          src={`/term/?arg=${encodeURIComponent(bound)}`}
          className="min-h-0 flex-1 border-0 bg-[#0b0f16]"
        />
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center px-4 text-center text-[12px] text-mut">
          {dead ? "session terminée — fenêtre tmux fermée" : "terminal indisponible — session non liée"}
        </div>
      )}

      {/* bandeau « signe de vie » — mirror le terminal quand claude bosse / propose un choix */}
      {view === "chat" && (working || awaiting) && (
        <div className={`border-t px-3 py-2 ${awaiting ? "border-amber-500/40 bg-amber-500/5" : "border-line bg-panel2/40"}`}>
          <div className="flex items-center gap-2 text-[11.5px]">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${awaiting ? "bg-amber-400" : "animate-pulse bg-emerald-400"}`} />
            <span className={`min-w-0 flex-1 truncate ${awaiting ? "text-amber-200" : "text-slate-300"}`}>
              {live?.activity || (awaiting ? "Claude attend une réponse" : "Claude travaille…")}
            </span>
            <button
              onClick={() => setShowMirror((v) => !v)}
              className="shrink-0 rounded px-1.5 text-[10.5px] text-mut hover:text-slate-200"
            >{showMirror ? "masquer" : "terminal ▾"}</button>
          </div>

          {/* boutons de choix : claude propose un menu (permission / sélection) */}
          {awaiting && live?.choices?.length ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {live.choices.map((c) => (
                <button
                  key={c.key}
                  disabled={!canWrite}
                  onClick={() => press(c.key)}
                  className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-left text-[11.5px] text-amber-100 hover:bg-amber-500/20 disabled:opacity-40"
                  title={c.label}
                >
                  <span className="font-semibold">{c.key}.</span>{" "}
                  <span className="align-middle">{c.label.length > 64 ? c.label.slice(0, 64) + "…" : c.label}</span>
                </button>
              ))}
              <button
                disabled={!canWrite}
                onClick={() => press("Escape")}
                className="rounded-md border border-line px-2 py-1 text-[11.5px] text-mut hover:text-slate-200 disabled:opacity-40"
              >Échap</button>
            </div>
          ) : null}

          {/* miroir brut du terminal (les étapes réflexion-action en direct) */}
          {showMirror && live?.tail && (
            <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-[#0b0f16] px-2 py-1.5 text-[10.5px] leading-snug text-slate-400 ring-1 ring-line">
              {live.tail}
            </pre>
          )}
        </div>
      )}

      {/* composer — envoie à la fenêtre liée à CETTE session (pas de choix manuel) */}
      <div className="border-t border-line p-2">
        {bound && !canWrite ? (
          <div className="text-center text-[11px] text-mut">lecture seule (rôle viewer)</div>
        ) : bound ? (
          <>
            <div className="flex items-end gap-2">
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
                }}
                rows={2}
                placeholder={`message → ${bound} (Entrée envoyer, Maj+Entrée newline)`}
                className="min-h-[36px] flex-1 resize-y rounded-lg border border-line bg-[#0b0f16] px-2.5 py-1.5 text-[12.5px] text-slate-100 outline-none focus:border-sea/50"
              />
              <button
                onClick={doSend}
                disabled={sending || !text.trim()}
                className="shrink-0 rounded-lg bg-sea/80 px-3 py-2 text-[12.5px] font-medium text-white disabled:opacity-40 hover:bg-sea"
              >{sending ? "…" : "envoyer"}</button>
            </div>
            {sendErr && <div className="mt-1 text-[11px] text-red-400">{sendErr}</div>}
          </>
        ) : (
          <div className="text-center text-[11px] text-mut">
            {dead
              ? "session terminée (fenêtre tmux fermée) — supprime-la depuis le rail"
              : "envoi indisponible — session non liée à une fenêtre tmux"}
          </div>
        )}
      </div>
    </div>
  );
}
