"use client";
import { useEffect, useState } from "react";
import { useMe, useCan } from "@/lib/me";
import { useFeatures } from "@/lib/features";
import { llmStatus, llmSetByok } from "@/lib/api";
import Wordmark from "./Wordmark";

const TABS = ["Board", "Sessions", "Preview", "Mémoire/KB", "Coûts", "Infra", "Journal"] as const;
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
            title={enabled ? "" : "à venir (P2+)"}
          >
            {t}
          </button>
        );
      })}
      <Identity />
    </header>
  );
}

function LlmKey() {
  const isAdmin = useCan("admin");
  const [st, setSt] = useState<{ mode: string; configured: boolean } | null>(null);
  const [editing, setEditing] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { llmStatus().then(setSt).catch(() => {}); }, []);
  if (!isAdmin || !st) return null;
  const label = st.mode === "included" ? "Inférence incluse (opérée)"
    : st.configured ? "Clé LLM · configurée" : "Clé LLM · à configurer";
  const save = () => {
    if (!key.trim()) return;
    setBusy(true);
    llmSetByok(key.trim()).then((r) => { setSt(r); setEditing(false); setKey(""); })
      .catch(() => {}).finally(() => setBusy(false));
  };
  return (
    <div className="border-b border-line px-3 py-2">
      <div className="text-[11px] text-mut">Modèle</div>
      <div className={`text-[12px] ${st.configured ? "text-emerald-400" : "text-amber-300"}`}>{label}</div>
      {st.mode !== "included" && !editing && (
        <button onClick={() => setEditing(true)} className="mt-1 text-[11px] text-sea hover:underline">
          {st.configured ? "changer la clé" : "coller ma clé Anthropic"}
        </button>
      )}
      {editing && (
        <div className="mt-1.5">
          <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="sk-ant-…" type="password"
            className="w-full rounded border border-line bg-[#0b0f16] px-2 py-1 text-[11px] text-slate-100 outline-none" />
          <div className="mt-1 flex gap-1.5">
            <button disabled={busy} onClick={save}
              className="rounded bg-sea/80 px-2 py-0.5 text-[11px] text-white hover:bg-sea disabled:opacity-50">enregistrer</button>
            <button onClick={() => { setEditing(false); setKey(""); }}
              className="rounded border border-line px-2 py-0.5 text-[11px] text-mut">annuler</button>
          </div>
          <div className="mt-1 text-[10px] text-mut">Reste sur votre VM — jamais transmise à NINABOT.</div>
        </div>
      )}
    </div>
  );
}

function Identity() {
  const me = useMe();
  const [open, setOpen] = useState(false);
  const color: Record<string, string> = {
    owner: "text-brass", admin: "text-sea", dev: "text-emerald-400", viewer: "text-mut",
  };
  if (!me) return <span className="ml-auto text-[11px] text-mut">la barre, pas l’autopilote</span>;
  return (
    <div className="relative ml-auto flex items-center gap-2 text-[11px] text-mut">
      <span className="hidden sm:inline">la barre, pas l’autopilote</span>
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
            <LlmKey />
            <a
              href="/api/auth/logout"
              className="block px-3 py-2 text-[12.5px] text-slate-200 hover:bg-panel2"
            >
              Se déconnecter →
            </a>
          </div>
        </>
      )}
    </div>
  );
}
