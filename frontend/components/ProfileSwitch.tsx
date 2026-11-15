"use client";
// Setup › My account — the per-person setting « simple profile / technical profile » (feature
// `simple_profile`; nothing is rendered when it is off). The simple profile has the same
// switch in its own Settings.
import { useEffect, useState } from "react";
import { fetchPrefs, savePrefs, type Prefs } from "@/lib/simple/api";

export default function ProfileSwitch() {
  const [p, setP] = useState<Prefs | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => { fetchPrefs().then(setP).catch(() => {}); }, []);
  if (!p || !p.available) return null;
  return (
    <section className="rounded-xl border border-line bg-panel p-4">
      <div className="text-[13px] font-semibold text-slate-100">Interface: SOKKAN Cockpit or SOKKAN Atelier</div>
      <p className="mt-1 max-w-prose text-[12.5px] leading-relaxed text-mut">
        <span className="text-slate-200">SOKKAN Atelier</span> is a task-first home for people who are not
        developers: say what you want, attach documents, approve what the agent wants to save, validate the
        deliverable — in French or English, light theme. <span className="text-slate-200">SOKKAN Cockpit</span> is
        this screen. You can come back here at any time from the Settings of SOKKAN Atelier.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          disabled={p.locked && p.profile !== "simple"}
          onClick={async () => {
            setErr("");
            try { await savePrefs({ profile: "simple" }); window.location.assign("/"); }
            catch { setErr("The interface is set by an administrator on this instance."); }
          }}
          className="ui-focus rounded-lg border border-brass/60 bg-brass/10 px-3 py-1.5 text-[12.5px] font-medium text-brass hover:bg-brass/20 disabled:opacity-40">
          Switch to SOKKAN Atelier
        </button>
        {err && <span role="alert" className="text-[12px] text-red-300">{err}</span>}
      </div>
    </section>
  );
}
