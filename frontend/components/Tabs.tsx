"use client";
import { useEffect, useState } from "react";
import { useMe, useCan } from "@/lib/me";
import { useFeatures } from "@/lib/features";
import { llmStatus } from "@/lib/api";
import Wordmark from "./Wordmark";
import Profile from "./Profile";

const TABS = ["Board", "Sessions", "Preview", "Memory/KB", "Costs", "Infra", "Operate", "Journal"] as const;
export type Tab = (typeof TABS)[number];

export default function Tabs({
  active,
  onChange,
}: {
  active: Tab;
  onChange: (t: Tab) => void;
}) {
  const feats = useFeatures();
  const visible = TABS.filter(
    (t) => (t !== "Preview" || feats.preview) && (t !== "Infra" || feats.infra)
      && (t !== "Operate" || feats.observe)
  );
  return (
    <header className="relative z-30 flex h-[54px] shrink-0 items-center gap-1.5 border-b border-line bg-panel px-4">
      <Wordmark className="text-[42px]" />
      <span className="mr-8" />
      {visible.map((t) => {
        const enabled = true;
        return (
          <button
            key={t}
            disabled={!enabled}
            onClick={() => enabled && onChange(t)}
            className={`rounded-md px-4 py-1.5 text-[15px] font-medium ${
              active === t
                ? "bg-panel2 text-slate-100 ring-1 ring-line"
                : enabled
                ? "text-slate-300 hover:bg-panel2"
                : "cursor-not-allowed text-mut/50"
            }`}
            title={enabled ? "" : "coming soon (P2+)"}
          >
            {t}
          </button>
        );
      })}
      <Identity />
    </header>
  );
}

function ProfileMenuItem({ onOpen }: { onOpen: () => void }) {
  const [st, setSt] = useState<{ configured: boolean; mode: string } | null>(null);
  useEffect(() => { llmStatus().then(setSt).catch(() => {}); }, []);
  const warn = st && !st.configured;
  return (
    <button onClick={onOpen} className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12.5px] text-slate-200 hover:bg-panel2">
      Profile & organization
      {warn && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-amber-400" title="model not configured" />}
    </button>
  );
}

function Identity() {
  const me = useMe();
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState(false);
  const color: Record<string, string> = {
    owner: "text-brass", admin: "text-sea", dev: "text-emerald-400", viewer: "text-mut",
  };
  if (!me) return <span className="ml-auto text-[11px] text-mut">the helm, not the autopilot</span>;
  return (
    <div className="relative ml-auto flex items-center gap-2 text-[11px] text-mut">
      <span className="hidden sm:inline">the helm, not the autopilot</span>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 rounded-full border border-line bg-panel2 px-2 py-0.5 hover:bg-line"
      >
        {me.name} · <span className={color[me.role] || "text-mut"}>{me.role}</span>
        <span className="text-mut">▾</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-50 mt-1.5 w-52 overflow-hidden rounded-lg border border-line bg-panel shadow-xl">
            <div className="border-b border-line px-3 py-2">
              <div className="truncate text-[12px] text-slate-200">{me.name}</div>
              <div className="truncate text-[10.5px] text-mut">{me.email}</div>
            </div>
            <ProfileMenuItem onOpen={() => { setOpen(false); setSettings(true); }} />
          </div>
        </>
      )}
      {settings && <Profile onClose={() => setSettings(false)} />}
    </div>
  );
}
