"use client";
import { createContext, useContext, useEffect, useState } from "react";

export interface Features {
  infra: boolean;
  preview: boolean;
  tmux: boolean;
}

const DEFAULTS: Features = { infra: true, preview: true, tmux: true };
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
