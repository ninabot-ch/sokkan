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
}

const DEFAULTS: Features = { infra: true, infra_topo: true, fleet: false, observe: false, preview: true, tmux: true, assistant: false };
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
