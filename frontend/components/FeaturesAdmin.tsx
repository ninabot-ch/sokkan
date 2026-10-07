"use client";
import { useState } from "react";
import { useFeatures, type FeatureItem } from "@/lib/features";

/** 3.2 — Profile → Features: every feature of the registry (backend/features.py), its
 *  effective state, what it needs and WHY it is off. Read-only: a feature is switched in
 *  the environment (SOKKAN_FEATURE_<ID>=1|0, SOKKAN_EDITION) then the API restarted —
 *  docs/enterprise/FEATURES.md. */
const GROUPS: [string, (f: FeatureItem) => boolean][] = [
  ["Asked for but off", (f) => f.problem],
  ["On", (f) => f.enabled && !f.problem],
  ["Off", (f) => !f.enabled && !f.problem && f.kind !== "planned"],
  ["Planned", (f) => f.kind === "planned" && !f.problem],
];

const dflt = (v: boolean | "auto") => (v === "auto" ? "auto" : v ? "on" : "off");

export default function FeaturesAdmin() {
  const reg = useFeatures().registry;
  const [open, setOpen] = useState<string | null>(null);
  if (!reg) return <div className="text-[12px] text-mut">This API does not serve the feature registry.</div>;
  const byId = Object.fromEntries(reg.items.map((f) => [f.id, f]));
  return (
    <div className="space-y-3 text-[12.5px]">
      <div className="rounded-lg border border-line bg-panel2/40 p-3 text-[11.5px] text-mut">
        Edition <span className="font-medium text-slate-100">{reg.edition}</span>. One app, one registry: each feature
        is switched with <code className="text-slate-300">SOKKAN_FEATURE_&lt;ID&gt;=1|0</code> in the environment
        (the edition only changes defaults), then the API restarted. A feature whose dependency is off stays off —
        its reason is below. Reference: <code className="text-slate-300">docs/enterprise/FEATURES.md</code>.
      </div>
      {reg.problems.length > 0 && (
        <div role="alert" className="rounded border border-red-400/40 bg-red-500/10 px-2 py-1.5 text-[11.5px] text-red-300">
          {reg.problems.length} feature{reg.problems.length > 1 ? "s" : ""} asked for in the environment
          {reg.problems.length > 1 ? " are" : " is"} OFF: {reg.problems.join(", ")}.
        </div>
      )}
      {GROUPS.map(([label, pick]) => {
        const items = reg.items.filter(pick);
        if (!items.length) return null;
        return (
          <div key={label}>
            <div className="mb-1 text-[11px] uppercase tracking-wide text-mut">{label} · {items.length}</div>
            <div className="divide-y divide-line rounded-lg border border-line">
              {items.map((f) => (
                <div key={f.id} className="px-3 py-2">
                  <button className="flex w-full items-center gap-2 text-left" onClick={() => setOpen(open === f.id ? null : f.id)}
                    aria-expanded={open === f.id}>
                    <span aria-label={f.enabled ? "on" : "off"}
                      className={`h-2 w-2 shrink-0 rounded-full ${f.problem ? "bg-red-400" : f.enabled ? "bg-emerald-400" : "bg-slate-600"}`} />
                    <span className="text-slate-100">{f.title}</span>
                    <code className="text-[10.5px] text-mut">{f.id}</code>
                    {f.status !== "stable" && (
                      <span className="rounded border border-line px-1 text-[10px] text-mut">{f.status}{f.target ? ` ${f.target}` : ""}</span>
                    )}
                    <span className="ml-auto text-[11px] text-mut">{f.enabled ? "on" : "off"}</span>
                  </button>
                  {(f.problem || !f.enabled) && f.kind !== "planned" && (
                    <div className={`mt-1 pl-4 text-[11.5px] ${f.problem ? "text-red-300" : "text-mut"}`}>{f.reason}</div>
                  )}
                  {f.note && <div className="mt-1 pl-4 text-[11.5px] text-amber-300">{f.note}</div>}
                  {open === f.id && (
                    <div className="mt-1.5 space-y-0.5 pl-4 text-[11.5px] text-mut">
                      <div className="text-slate-300">{f.description}</div>
                      <div>Decided by: {f.reason} <span className="text-[10.5px]">({f.source})</span></div>
                      {f.requires.length > 0 && (
                        <div>Requires: {f.requires.map((r) => `${r} (${byId[r]?.enabled ? "on" : "off"})`).join(", ")}</div>
                      )}
                      {f.required_by.length > 0 && <div>Required by: {f.required_by.join(", ")}</div>}
                      {f.conflicts.length > 0 && <div>Conflicts with: {f.conflicts.join(", ")}</div>}
                      {f.kind === "toggle" && (
                        <div>Defaults: community {dflt(f.defaults.community)}, enterprise {dflt(f.defaults.enterprise)} ·
                          switch <code>{f.env.join(" / ")}</code></div>
                      )}
                      {f.kind === "integration" && <div>On when configured: <code>{f.config.join(", ")}</code></div>}
                      {f.legacy_var && <div className="text-amber-300">Set by the legacy variable {f.legacy_var}.</div>}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
