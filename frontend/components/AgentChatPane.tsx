"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AgentSocket, fetchAgentCommands,
  type AgentEvent, type AgentQuestion, type PermMode,
} from "@/lib/agent";
import type { Message } from "@/lib/types";
import ChatMessage from "./ChatMessage";
import { useCan } from "@/lib/me";

interface PermReq { id: string; tool: string; title: string; input: Record<string, unknown> }
interface QReq { id: string; questions: AgentQuestion[] }

// modes de permission dans l'ordre du cycle (clic sur le badge ou Maj+Tab)
const PERM_MODES: { id: PermMode; label: string; cls: string; title: string }[] = [
  { id: "default",           label: "🔒 confirmations", cls: "border-line text-mut",
    title: "every mutating tool asks for confirmation" },
  { id: "acceptEdits",       label: "✏️ auto-edits",    cls: "border-sea/50 text-sea",
    title: "file edits auto-accepted · Bash still confirms" },
  { id: "bypassPermissions", label: "⚡ automode",       cls: "border-amber-500/60 text-amber-300 bg-amber-500/10",
    title: "everything auto-accepted — no confirmations (YOLO)" },
  { id: "plan",              label: "📋 plan",          cls: "border-violet-400/50 text-violet-300",
    title: "read-only — Claude produces a plan before acting" },
];

// pane de grille pour une session SDK possédée par SOKKAN.
// Le sid vient du store (spawn) ; le resume est géré côté serveur
// (claude_session_id persisté en sqlite) — un refresh rejoue le ring buffer.
export default function AgentChatPane({
  sid, title, tag, onClose,
}: {
  sid: string;
  title?: string;
  tag?: string;
  onClose?: (sid: string) => void;
}) {
  const canWrite = useCan("dev");
  const [open, setOpen] = useState(false);
  const [working, setWorking] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [perms, setPerms] = useState<PermReq[]>([]);
  const [questions, setQuestions] = useState<QReq[]>([]);
  const [text, setText] = useState("");
  const [model, setModel] = useState<string>("");
  const [pmode, setPmode] = useState<PermMode>("default");
  const sock = useRef<AgentSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  // connexion WS (resume serveur — pas de csid côté client)
  useEffect(() => {
    if (!sid) return;
    const s = new AgentSocket(sid, onEvent, setOpen, () => null);
    sock.current = s;
    return () => { s.close(); sock.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  const onEvent = (e: AgentEvent) => {
    switch (e.type) {
      case "session":
        // claude_session_id persisté côté serveur — rien à faire ici
        break;
      case "model":
        setModel(e.model);
        break;
      case "perm_mode":
        setPmode(e.mode);
        break;
      case "status":
        setWorking(e.state === "working");
        break;
      case "text":
        push({ role: "assistant", kind: "text", text: e.text });
        break;
      case "thinking":
        push({ role: "assistant", kind: "thinking", text: e.text });
        break;
      case "tool_use":
        push({ role: "assistant", kind: "tool", tool: e.tool, title: e.title,
               input: e.input, id: e.id, result: null });
        break;
      case "tool_result":
        setMessages((ms) => ms.map((m) =>
          m.kind === "tool" && m.id && m.id === e.tool_use_id
            ? { ...m, result: { text: e.text, is_error: e.is_error, truncated: e.truncated } }
            : m));
        break;
      case "permission":
        setPerms((p) => [...p, { id: e.id, tool: e.tool, title: e.title, input: e.input }]);
        break;
      case "question":
        setQuestions((q) => [...q, { id: e.id, questions: e.questions }]);
        break;
      case "permission_resolved":
        setPerms((p) => p.filter((x) => x.id !== e.id));
        break;
      case "question_resolved":
        setQuestions((q) => q.filter((x) => x.id !== e.id));
        break;
      case "error":
        push({ role: "system", kind: "note", text: `⚠ ${e.message}` });
        break;
      case "result":
        // fin de tour : rien à afficher (le texte est déjà streamé)
        break;
    }
  };

  const push = (m: Message) => {
    stick.current = true;
    setMessages((ms) => [...ms, m]);
  };

  useEffect(() => {
    if (stick.current && scrollRef.current)
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages.length, perms.length, questions.length]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const doSend = () => {
    const t = text.trim();
    if (!t || !sock.current) return;
    push({ role: "user", kind: "text", text: t });
    sock.current.sendUser(t);
    setText("");
  };

  const answerPerm = (id: string, decision: "allow" | "deny") => {
    sock.current?.answerPermission(id, decision);
    setPerms((p) => p.filter((x) => x.id !== id));
  };

  const answerQuestion = (id: string, answers: Record<string, string | string[]>) => {
    sock.current?.answerQuestion(id, answers);
    setQuestions((q) => q.filter((x) => x.id !== id));
  };

  // cycle de mode (optimiste ; le serveur reconfirme via l'event perm_mode)
  const cycleMode = () => {
    if (!canWrite || !sock.current) return;
    const i = PERM_MODES.findIndex((m) => m.id === pmode);
    const next = PERM_MODES[(i + 1) % PERM_MODES.length];
    setPmode(next.id);
    sock.current.setMode(next.id);
  };

  // ---- palette slash -------------------------------------------------------
  const [cmds, setCmds] = useState<{ name: string; desc: string }[]>([]);
  const [palIdx, setPalIdx] = useState(0);
  useEffect(() => { fetchAgentCommands().then(setCmds).catch(() => {}); }, []);
  const palette = useMemo(() => {
    const m = /^\/(\S*)$/.exec(text);
    if (!m) return [];
    const pre = m[1].toLowerCase();
    return cmds.filter((c) => c.name.slice(1).toLowerCase().startsWith(pre)).slice(0, 8);
  }, [text, cmds]);
  useEffect(() => { setPalIdx(0); }, [text]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Tab" && e.shiftKey) { e.preventDefault(); cycleMode(); return; }
    if (palette.length) {
      if (e.key === "ArrowDown") { e.preventDefault(); setPalIdx((i) => (i + 1) % palette.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setPalIdx((i) => (i - 1 + palette.length) % palette.length); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault(); setText(palette[palIdx].name + " "); return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
  };

  const curMode = PERM_MODES.find((m) => m.id === pmode) ?? PERM_MODES[0];

  return (
    <div className="flex min-h-0 flex-col rounded-xl border border-line bg-panel">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${
          working ? "animate-pulse bg-emerald-400" : open ? "bg-emerald-500" : "bg-slate-600"
        }`} title={working ? "Claude is working…" : open ? "connected" : "disconnected"} />
        {tag && <span className="shrink-0 rounded bg-brass/15 px-1.5 text-[10px] text-brass">{tag}</span>}
        {model && <span className="shrink-0 rounded bg-sea/15 px-1.5 text-[10px] text-sea" title="session model">{model}</span>}
        <div className="min-w-0">
          <div className="truncate text-[13px] font-medium text-slate-100">{title || "Session"}</div>
          <div className="truncate text-[10.5px] text-mut">
            {open ? "connected" : "connecting…"}{working && <span className="text-brass"> · Claude is working…</span>}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={cycleMode} disabled={!canWrite}
            title={`${curMode.title} · click or Shift+Tab to change mode`}
            className={`shrink-0 rounded-md border px-2 py-0.5 text-[11px] hover:bg-panel2 disabled:opacity-40 ${curMode.cls}`}>
            {curMode.label}
          </button>
          {working && (
            <button onClick={() => sock.current?.interrupt()}
              className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-100 hover:bg-amber-500/20">
              interrupt
            </button>
          )}
          {onClose && (
            <button onClick={() => onClose(sid)} title="close pane"
              className="rounded px-1.5 py-0.5 text-mut hover:bg-panel2 hover:text-slate-200">✕</button>
          )}
        </div>
      </header>

      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {messages.length === 0 && (
          <div className="mt-6 text-center text-[12px] text-mut">
            session ready — type a message (type '/' for commands)
          </div>
        )}
        {messages.map((m, i) => <ChatMessage key={i} m={m} />)}

        {/* demandes de permission */}
        {perms.map((p) => (
          <div key={p.id} className="my-2 rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2">
            <div className="text-[12px] text-amber-100">
              Allow <span className="font-semibold">{p.tool}</span>?
            </div>
            <pre className="my-1.5 max-h-32 overflow-auto rounded bg-[#0b0f16] p-2 text-[11px] text-slate-300">
              {p.title}
            </pre>
            <div className="flex gap-2">
              <button disabled={!canWrite} onClick={() => answerPerm(p.id, "allow")}
                className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-1 text-[12px] text-emerald-100 hover:bg-emerald-500/20 disabled:opacity-40">
                Allow
              </button>
              <button disabled={!canWrite} onClick={() => answerPerm(p.id, "deny")}
                className="rounded-md border border-line px-3 py-1 text-[12px] text-mut hover:text-slate-200 disabled:opacity-40">
                Deny
              </button>
            </div>
          </div>
        ))}

        {/* questions à choix (AskUserQuestion) */}
        {questions.map((q) => <QuestionCard key={q.id} q={q} disabled={!canWrite}
          onAnswer={(a) => answerQuestion(q.id, a)} />)}
      </div>

      {/* composer + palette slash */}
      <div className="relative border-t border-line p-2">
        {palette.length > 0 && (
          <div className="absolute bottom-full left-2 right-2 mb-1 overflow-hidden rounded-lg border border-line bg-panel2 shadow-xl">
            {palette.map((c, i) => (
              <button key={c.name} onClick={() => setText(c.name + " ")}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] ${
                  i === palIdx ? "bg-sea/15 text-slate-100" : "text-slate-300 hover:bg-panel"
                }`}>
                <span className="font-mono text-brass">{c.name}</span>
                <span className="truncate text-mut">{c.desc}</span>
              </button>
            ))}
          </div>
        )}
        {canWrite ? (
          <div className="flex items-end gap-2">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={onKeyDown}
              rows={2}
              placeholder="message (Enter to send, Shift+Enter for newline, / for commands)"
              className="min-h-[36px] flex-1 resize-y rounded-lg border border-line bg-[#0b0f16] px-2.5 py-1.5 text-[12.5px] text-slate-100 outline-none focus:border-sea/50"
            />
            <button onClick={doSend} disabled={!text.trim() || !open}
              className="shrink-0 rounded-lg bg-sea/80 px-3 py-2 text-[12.5px] font-medium text-white disabled:opacity-40 hover:bg-sea">
              send
            </button>
          </div>
        ) : (
          <div className="text-center text-[11px] text-mut">read-only (viewer role)</div>
        )}
      </div>
    </div>
  );
}

// --- carte question à choix (radio si single, cases si multiSelect) ----------
function QuestionCard({ q, disabled, onAnswer }: {
  q: QReq; disabled: boolean; onAnswer: (a: Record<string, string | string[]>) => void;
}) {
  const [sel, setSel] = useState<Record<string, string[]>>({});

  const toggle = (question: string, label: string, multi: boolean) => {
    setSel((s) => {
      const cur = s[question] || [];
      if (multi) {
        return { ...s, [question]: cur.includes(label) ? cur.filter((x) => x !== label) : [...cur, label] };
      }
      return { ...s, [question]: [label] };
    });
  };

  const submit = () => {
    const answers: Record<string, string | string[]> = {};
    for (const item of q.questions) {
      const cur = sel[item.question] || [];
      answers[item.question] = item.multiSelect ? cur : (cur[0] || "");
    }
    onAnswer(answers);
  };

  const ready = q.questions.every((item) => (sel[item.question] || []).length > 0);

  return (
    <div className="my-2 rounded-lg border border-sea/40 bg-sea/5 px-3 py-2">
      {q.questions.map((item, qi) => (
        <div key={qi} className="mb-2 last:mb-0">
          {item.header && <div className="mb-1 text-[10.5px] uppercase tracking-wide text-brass">{item.header}</div>}
          <div className="mb-1.5 text-[12.5px] text-slate-100">{item.question}</div>
          <div className="flex flex-wrap gap-1.5">
            {item.options.map((opt) => {
              const on = (sel[item.question] || []).includes(opt.label);
              return (
                <button key={opt.label} disabled={disabled}
                  onClick={() => toggle(item.question, opt.label, !!item.multiSelect)}
                  title={opt.description}
                  className={`rounded-md border px-2.5 py-1 text-left text-[12px] disabled:opacity-40 ${
                    on ? "border-sea bg-sea/20 text-white" : "border-line bg-panel2 text-slate-300 hover:text-slate-100"
                  }`}>
                  {item.multiSelect && <span className="mr-1">{on ? "☑" : "☐"}</span>}
                  {opt.label}
                </button>
              );
            })}
          </div>
        </div>
      ))}
      <button disabled={disabled || !ready} onClick={submit}
        className="mt-1 rounded-md bg-sea/80 px-3 py-1 text-[12px] font-medium text-white disabled:opacity-40 hover:bg-sea">
        submit
      </button>
    </div>
  );
}
