"use client";
import { createContext, useContext, useEffect, useState } from "react";

export interface Features {
  infra: boolean;
  infra_topo: boolean;
  fleet: boolean;
  observe: boolean;
  preview: boolean;
  tmux: boolean;
  assistant: boolean;
  missions_link: boolean;
  magnitude: boolean;
  demo: boolean;
  agents: boolean;
  /** 3.1.1 : Crew visible in read-only to a viewer (SOKKAN_CREW_VIEWER_READONLY=1) */
  agents_viewer_readonly?: boolean;
  /** 3.1.1 : simulated agent runs of the public demo (SOKKAN_DEMO_CREW=1) */
  demo_crew?: boolean;
  /** 3.3 : Helm (feature `helm`) — hierarchy in the card dialog, Helm tab for managers */
  helm?: boolean;
  /** 3.2 : projects (feature `multi_project`) */
  multi_project?: boolean;
  /** 3.2 : the feature registry (backend/features.py) — effective state and why */
  registry?: FeatureRegistry;
}

export interface FeatureItem {
  id: string;
  title: string;
  description: string;
  status: "stable" | "beta" | "experimental" | "planned";
  kind: "toggle" | "integration" | "invariant" | "planned";
  enabled: boolean;
  source: string;
  reason: string;
  /** asked for explicitly but not honoured (missing dependency, conflict, planned) */
  problem: boolean;
  note: string;
  legacy_var: string;
  requires: string[];
  conflicts: string[];
  required_by: string[];
  env: string[];
  config: string[];
  defaults: { community: boolean | "auto"; enterprise: boolean | "auto" };
  target: string;
  doc: string;
}

export interface FeatureRegistry {
  edition: "community" | "enterprise";
  items: FeatureItem[];
  problems: string[];
}

const DEFAULTS: Features = { infra: true, infra_topo: true, fleet: false, observe: false, preview: true, tmux: true, assistant: false, missions_link: true, magnitude: true, demo: false, agents: true };
const Ctx = createContext<Features>(DEFAULTS);

export function FeaturesProvider({ children }: { children: React.ReactNode }) {
  const [f, setF] = useState<Features>(DEFAULTS);
  useEffect(() => {
    fetch("/api/features", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : DEFAULTS))
      .then(setF)
      .catch(() => {});
  }, []);
  return <Ctx.Provider value={f}>{children}</Ctx.Provider>;
}

export const useFeatures = () => useContext(Ctx);
