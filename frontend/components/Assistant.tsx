"use client";
import { agentPropose, type Agent } from "@/lib/api";
import HelmProposal from "./HelmProposal";
import type { ProjectProposal } from "@/lib/helm";
import { useEffect, useRef, useState } from "react";
import { useFeatures } from "@/lib/features";
import MiniMarkdown from "./MiniMarkdown";
import LevelBadge from "./LevelBadge";

// 3.4: the panel's own words follow the browser's language (the cockpit is English, Nina
// answers in the person's language) — a French greeting in an English UI confused a manager
const FR = typeof navigator !== "undefined" && /^fr\b/i.test(navigator.language || "");
const T = FR ? {
  tagline: "Votre ingénieure DevOps — projets, produit, mémoire, coûts.",
  hello: "Bonjour 👋 Je connais SOKKAN par cœur. Par exemple :",
  ex: ["« Comment importer mon projet ? »", "« Comment semer la mémoire de ce projet ? »", "« Worker ou plan supérieur, comment choisir ? »"],
  exHelm: "« Crée un projet avec moi » — je le découpe en cartes",
  thinking: "Nina réfléchit…", placeholder: "Votre question…", unknown: "erreur inconnue",
} : {
  tagline: "Your DevOps engineer — projects, product, memory, costs.",
  hello: "Hello 👋 I know SOKKAN inside out. For example:",
  ex: ["“How do I import my project?”", "“How do I seed this project's memory?”", "“Why is this agent waiting?”"],
  exHelm: "“Create a project with me” — I break it into cards",
  thinking: "Nina is thinking…", placeholder: "Your question…", unknown: "unknown error",
};

type Msg = { role: string; content: string; ts?: number; level?: string };

// Nina — l'agente d'assistance embarquée (S1) : bouton flottant + panneau.
// Feature-gated serveur (SOKKAN_FEATURE_ASSISTANT) — le flag front ne fait
// que masquer le bouton.
// 3.2 : the panel belongs to the tab where it was opened — switching tab tucks it away
// (conversation kept, it comes back on that tab) unless it is explicitly pinned.
export default function Assistant({ tab }: { tab: string }) {
  const features = useFeatures();
  const [openOn, setOpenOn] = useState<string | null>(null);
  const [pinned, setPinned] = useState(false);
  const open = openOn !== null && (pinned || openOn === tab);
  const tucked = openOn !== null && !open;
  const toggle = () => {
    if (open) { setOpenOn(null); setPinned(false); } else setOpenOn(tab);
  };
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const loaded = useRef(false);
  const [historyReady, setHistoryReady] = useState(false);

  useEffect(() => {
    if (!open || loaded.current) return;
    loaded.current = true;
    fetch("/api/assistant/history", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : []))
      // keep what was typed meanwhile — the history used to overwrite a message sent at once
      .then((h: Msg[]) => setMsgs((m) => [...(Array.isArray(h) ? h : []), ...m]))
      .catch(() => {})
      .finally(() => setHistoryReady(true));
  }, [open]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs, busy]);

  // 3.4: « Create a project with Nina » (Helm) opens the panel here and asks for it
  const [queued, setQueued] = useState<string | null>(null);
  useEffect(() => {
    const h = (e: Event) => {
      const m = (e as CustomEvent<{ message?: string }>).detail?.message;
      setOpenOn(tab);
      if (m) setQueued(m);
    };
    window.addEventListener("sokkan:nina", h);
    return () => window.removeEventListener("sokkan:nina", h);
  }, [tab]);
  useEffect(() => {
    if (queued && open && historyReady && !busy) { const m = queued; setQueued(null); void send(m); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queued, open, historyReady, busy]);

  if (!features.assistant) return null;
  const wide = msgs.some((m) => m.role === "assistant" && m.content.includes("```sokkan-project"));

  async function send(forced?: string) {
    const message = (forced ?? input).trim();
    if (!message || busy) return;
    if (forced === undefined) setInput("");
    setErr(null);
    setMsgs((m) => [...m, { role: "user", content: message }]);
    setBusy(true);
    try {
      // Flux SSE : le débit du modèle ne change pas, mais on lit pendant que ça
      // s'écrit. Sur silicium maison une réponse détaillée met 30 s à sortir —
      // l'attendre en entier derrière un spinner la rendait inutilisable.
      const r = await fetch("/api/assistant/chat/stream", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message }),
      });
      if (!r.ok || !r.body) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data?.detail || `erreur ${r.status}`);
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let started = false;
      let level: string | undefined;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // une trame SSE se termine par une ligne vide ; on ne traite que les
        // trames complètes et on garde le reste en tampon
        const frames = buf.split("\n\n");
        buf = frames.pop() ?? "";
        for (const frame of frames) {
          const ev = /^event:\s*(\w+)/m.exec(frame)?.[1];
          const raw = /^data:\s*(.*)$/m.exec(frame)?.[1];
          if (!ev || !raw) continue;
          let payload: { text?: string; detail?: string };
          try {
            payload = JSON.parse(raw);
          } catch {
            continue;
          }
          if (ev === "error") throw new Error(payload.detail || T.unknown);
          if (ev === "level" && payload.text) level = payload.text;
          // `first` is captured NOW: React may run the updater after `started` flipped —
          // reading `started` inside it replaced the person's own message (08.10)
          if (ev === "delta" && payload.text) {
            const chunk = payload.text;
            const first = !started;
            const lv = level;
            setMsgs((m) => {
              if (first) return [...m, { role: "assistant", content: chunk, level: lv }];
              const last = m[m.length - 1];
              return [...m.slice(0, -1), { ...last, content: last.content + chunk }];
            });
            // le spinner disparaît au 1er fragment : c'est là que l'attente cesse
            if (!started) setBusy(false);
            started = true;
          }
          if (ev === "done" && payload.text) {
            const full = payload.text;
            const replace = started;
            const lv = level;
            setMsgs((m) =>
              replace
                ? [...m.slice(0, -1), { role: "assistant", content: full, level: lv }]
                : [...m, { role: "assistant", content: full, level: lv }],
            );
            started = true;
          }
        }
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : T.unknown);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {/* bouton flottant */}
      <button
        onClick={toggle}
        title={tucked ? `Nina — conversation open on ${openOn}: reopen it here` : "Nina — assistance"}
        aria-label={open ? "Close Nina" : tucked ? `Reopen Nina (conversation open on ${openOn})` : "Open Nina"}
        aria-expanded={open}
        className="fixed bottom-5 right-5 z-40 flex h-12 w-12 items-center justify-center rounded-full border border-line bg-panel text-xl shadow-lg transition hover:border-amber-500/60"
      >
        {open ? "✕" : "🧭"}
        {tucked && (
          <span aria-hidden className="absolute -right-0.5 -top-0.5 h-3 w-3 rounded-full border-2 border-panel bg-amber-400" />
        )}
      </button>

      {/* panneau */}
      {open && (
        <div className={`fixed bottom-20 right-5 z-40 flex flex-col rounded-2xl border border-line bg-[#0d0f14] shadow-2xl ${wide ? "h-[min(720px,82vh)] w-[min(600px,94vw)]" : "h-[min(560px,75vh)] w-[min(400px,92vw)]"}`}>
          <div className="flex items-start gap-2 border-b border-line px-4 py-3">
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-slate-100">Nina</div>
              <div className="text-[11px] text-mut">
                {T.tagline}
              </div>
            </div>
            <button
              onClick={() => { setPinned((v) => !v); setOpenOn(tab); }}
              aria-pressed={pinned}
              title={pinned ? "Pinned: stays open on every tab — click to unpin" : "Pin: keep Nina open when switching tabs"}
              className={`ui-focus shrink-0 rounded px-2 py-0.5 text-[11px] ring-1 ${pinned ? "bg-amber-500/15 text-amber-200 ring-amber-500/50" : "text-mut ring-line hover:text-slate-200"}`}
            >
              <span aria-hidden>📌</span> {pinned ? "Pinned" : "Pin"}
            </button>
          </div>
          <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
            {msgs.length === 0 && !busy && (
              <div className="text-[13px] leading-relaxed text-mut">
                {T.hello}
                <ul className="mt-2 list-disc pl-4">
                  {features.helm && <li>{T.exHelm}</li>}
                  {T.ex.map((x) => <li key={x}>{x}</li>)}
                </ul>
              </div>
            )}
            {msgs.map((m, i) => (
              <div
                key={i}
                className={
                  m.role === "user"
                    ? "ml-6 rounded-xl bg-blue-500/10 px-3 py-2 text-[13px] leading-relaxed text-slate-100"
                    : "mr-6 rounded-xl border border-line bg-panel px-3 py-2 text-[13px] leading-relaxed text-slate-200"
                }
              >
                {m.role === "assistant" && m.level && m.level !== "project" && (
                  <div className="mb-1 flex items-center gap-1.5 text-[10.5px] text-mut">
                    <LevelBadge level={m.level} /> built from notes at this level — share it accordingly
                  </div>
                )}
                {m.role === "assistant" ? <AssistantText text={m.content} /> : m.content}
              </div>
            ))}
            {busy && <div className="mr-6 animate-pulse text-[13px] text-mut">{T.thinking}</div>}
            {err && <div className="text-[12px] text-red-400">{err}</div>}
            <div ref={endRef} />
          </div>
          <div className="flex gap-2 border-t border-line p-3">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
              placeholder={T.placeholder}
              className="flex-1 rounded-lg border border-line bg-[#07080a] px-3 py-2 text-[13px] text-slate-100 outline-none focus:border-blue-500/50"
            />
            <button
              onClick={() => void send()}
              disabled={busy || !input.trim()}
              className="rounded-lg border border-line bg-panel px-3 py-2 text-[13px] text-slate-100 disabled:opacity-40"
            >
              ➤
            </button>
          </div>
        </div>
      )}
    </>
  );
}


// Nina peut terminer une réponse par un bloc ```sokkan-agent {json}``` (cf. KB
// « Agents ») : on l'affiche comme une carte proposée, que l'humain crée d'un clic.
// La carte naît « pending » : elle ne tourne qu'après approbation dans Crew.
const AGENT_BLOCK = /```sokkan-agent\s*([\s\S]*?)```/;

// 3.3 Helm : ```sokkan-project {json}``` = la carte projet + la décomposition proposées
// par Nina après l'interview ; l'humain les modifie puis valide (HelmProposal).
const PROJECT_BLOCK = /```sokkan-project\s*([\s\S]*?)```/;

function AssistantText({ text }: { text: string }) {
  const pm = text.match(PROJECT_BLOCK);
  if (pm) {
    let p: ProjectProposal | null = null;
    try { p = JSON.parse(pm[1]); } catch { p = null; }
    const after = text.slice((pm.index || 0) + pm[0].length).trim();
    return (
      <>
        <MiniMarkdown text={text.slice(0, pm.index).trimEnd()} />
        {p ? <HelmProposal proposal={p} /> : <pre className="mt-2 whitespace-pre-wrap text-[11px] text-mut">{pm[0]}</pre>}
        {after && <MiniMarkdown className="mt-2" text={after} />}
      </>
    );
  }
  // a project block still being written (or cut): show it as code, not as raw prose
  const open = text.indexOf("```sokkan-project");
  if (open >= 0) {
    return (
      <>
        <MiniMarkdown text={text.slice(0, open).trimEnd()} />
        <pre className="mt-2 max-h-40 overflow-hidden whitespace-pre-wrap text-[11px] text-mut">{text.slice(open)}</pre>
      </>
    );
  }
  const m = text.match(AGENT_BLOCK);
  if (!m) return <MiniMarkdown text={text} />;
  let spec: Record<string, unknown> | null = null;
  try { spec = JSON.parse(m[1]); } catch { spec = null; }
  const before = text.slice(0, m.index).trimEnd();
  const after = text.slice((m.index || 0) + m[0].length).trim();
  return (
    <>
      <MiniMarkdown text={before} />
      {spec ? <AgentProposal spec={spec} /> : <pre className="mt-2 whitespace-pre-wrap text-[11px] text-mut">{m[0]}</pre>}
      {after && <MiniMarkdown className="mt-2" text={after} />}
    </>
  );
}

function AgentProposal({ spec }: { spec: Record<string, unknown> }) {
  const [state, setState] = useState<{ id?: number; err?: string; busy?: boolean }>({});
  const create = async () => {
    setState({ busy: true });
    try {
      const a = await agentPropose(spec as Partial<Agent>);
      setState({ id: a.id });
    } catch (e) { setState({ err: String((e as Error).message) }); }
  };
  const row = (k: string, v: unknown) => (v === undefined || v === "" || (Array.isArray(v) && !v.length) ? null : (
    <div key={k} className="flex gap-2"><span className="w-24 shrink-0 text-mut">{k}</span><span className="min-w-0 break-words text-slate-200">{Array.isArray(v) ? v.join(", ") : String(v)}</span></div>
  ));
  return (
    <div className="crew-c-idle crew-card mt-2 whitespace-normal rounded-lg border border-line bg-panel2/70 p-2.5 text-[11.5px]">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-[13px] font-semibold text-slate-100">{String(spec.name || "new agent")}</span>
        <span className="crew-fg text-[10px] font-semibold uppercase">● proposed agent</span>
      </div>
      <div className="space-y-0.5">
        {row("purpose", spec.purpose)}{row("deliverable", spec.deliverable)}{row("trigger", spec.trigger === "cron" ? `cron ${spec.schedule}` : spec.trigger)}
        {row("model", spec.model)}{row("tools", spec.tools)}{row("auto", spec.auto_approve)}{row("secrets", spec.secrets)}
        {row("budget", spec.budget_usd !== undefined ? `$${spec.budget_usd} / run` : undefined)}{row("outputs", spec.outputs)}
      </div>
      <div className="mt-2 flex items-center gap-2">
        {state.id ? (
          <a href={`/?plane=build&tab=crew&agent=${state.id}`} className="text-sea hover:underline">✓ Card created — review & approve it in Crew →</a>
        ) : (
          <button onClick={create} disabled={state.busy} className="rounded-md bg-brass/90 px-2.5 py-1 font-semibold text-ink hover:bg-brass disabled:opacity-50">Create the card</button>
        )}
        {state.err && <span className="text-red-400">{state.err}</span>}
      </div>
    </div>
  );
}
