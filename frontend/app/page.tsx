"use client";
import { useState } from "react";
import Tabs, { type Tab } from "@/components/Tabs";
import SessionRail from "@/components/SessionRail";
import ChatPane from "@/components/ChatPane";
import AgentChatPane from "@/components/AgentChatPane";
import Board from "@/components/Board";
import Preview from "@/components/Preview";
import MemoryKB from "@/components/MemoryKB";
import Infra from "@/components/Infra";
import Journal from "@/components/Journal";
import Costs from "@/components/Costs";
import { MeProvider } from "@/lib/me";
import { FeaturesProvider } from "@/lib/features";
import { fetchSessions } from "@/lib/api";

const DENSITIES = [1, 2, 3, 4];

interface OpenPane {
  id: string;
  kind: "sdk" | "tmux";
  title?: string;
  tag?: string;
}

export default function Home() {
  const [tab, setTab] = useState<Tab>("Sessions");
  const [open, setOpen] = useState<OpenPane[]>([]);
  const [cols, setCols] = useState(2);

  const close = (id: string) => setOpen((cur) => cur.filter((x) => x.id !== id));

  // ouvre un pane ; si le kind n'est pas connu (ex. « ouvrir » depuis une vieille
  // carte du board), on le résout via /api/sessions
  const openSession = async (s: { session_id: string; kind?: "sdk" | "tmux"; title?: string; tag?: string }) => {
    let { kind, title, tag } = s;
    if (!kind) {
      try {
        const all = await fetchSessions();
        const found = all.find((x) => x.session_id === s.session_id);
        kind = (found?.kind as "sdk" | "tmux") ?? "tmux";
        title = title ?? found?.title;
        tag = tag ?? found?.tag;
      } catch { kind = "tmux"; }
    }
    setOpen((cur) => (cur.some((x) => x.id === s.session_id)
      ? cur
      : [...cur, { id: s.session_id, kind: kind!, title, tag }]));
    setTab("Sessions");
  };

  const toggle = (s: { session_id: string; kind?: "sdk" | "tmux"; title?: string; tag?: string }) => {
    if (open.some((x) => x.id === s.session_id)) close(s.session_id);
    else openSession(s);
  };

  return (
    <FeaturesProvider>
    <MeProvider>
    <div className="flex h-screen flex-col">
      <Tabs active={tab} onChange={setTab} />
      {tab === "Board" ? (
        <Board onOpenSession={(sid) => openSession({ session_id: sid })} />
      ) : tab === "Preview" ? (
        <Preview />
      ) : tab === "Mémoire/KB" ? (
        <MemoryKB />
      ) : tab === "Infra" ? (
        <Infra />
      ) : tab === "Journal" ? (
        <Journal />
      ) : tab === "Coûts" ? (
        <Costs />
      ) : (
        <div className="flex min-h-0 flex-1">
          <SessionRail
            open={open.map((x) => x.id)}
            onOpen={toggle}
            onDelete={close}
          />
          <main className="flex min-h-0 flex-1 flex-col">
            <div className="flex items-center gap-2 border-b border-line bg-panel/60 px-3 py-1.5 text-[11px] text-mut">
              <span>{open.length} fenêtre(s)</span>
              <span className="ml-auto">densité</span>
              {DENSITIES.map((d) => (
                <button
                  key={d}
                  onClick={() => setCols(d)}
                  className={`rounded px-1.5 ${cols === d ? "bg-panel2 text-slate-200 ring-1 ring-line" : "hover:bg-panel2"}`}
                >{d}</button>
              ))}
            </div>
            {open.length === 0 ? (
              <div className="flex flex-1 items-center justify-center text-[13px] text-mut">
                ← choisis une session dans le rail (ou spawn une tâche dans le Board)
              </div>
            ) : (
              <div
                className="grid min-h-0 flex-1 gap-2 overflow-auto p-2"
                style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
              >
                {open.map((p) =>
                  p.kind === "sdk" ? (
                    <AgentChatPane key={p.id} sid={p.id} title={p.title} tag={p.tag} onClose={close} />
                  ) : (
                    <ChatPane key={p.id} id={p.id} onClose={(id) => close(id)} />
                  )
                )}
              </div>
            )}
          </main>
        </div>
      )}
    </div>
    </MeProvider>
    </FeaturesProvider>
  );
}
