"use client";
import { createContext, useContext, useEffect, useState } from "react";
import { fetchMe } from "./api";
import type { Me } from "./types";
import Wordmark from "@/components/Wordmark";

const ROLES = ["viewer", "dev", "admin", "owner"];
export const rank = (r: string) => ROLES.indexOf(r);

const Ctx = createContext<Me | null>(null);

export function MeProvider({ children }: { children: React.ReactNode }) {
  // undefined = chargement · null = non authentifié (login requis) · Me = connecté
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  useEffect(() => {
    fetchMe().then(setMe).catch(() => setMe(null));
  }, []);

  if (me === undefined)
    return <div className="flex h-screen items-center justify-center text-[13px] text-mut">…</div>;
  if (me === null) return <LoginScreen />;
  return <Ctx.Provider value={me}>{children}</Ctx.Provider>;
}

interface AuthInfo { mode: string; login_required: boolean }

function LoginScreen() {
  const [info, setInfo] = useState<AuthInfo | null>(null);
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    fetch("/api/auth/info").then((r) => r.json()).then(setInfo).catch(() => {});
  }, []);

  const loginLocal = async () => {
    setErr("");
    const r = await fetch("/api/auth/local", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (r.ok) location.reload();
    else setErr("invalid token");
  };

  return (
    <div className="flex h-screen flex-col items-center justify-center gap-9 bg-ink px-6">
      <div className="flex flex-col items-center gap-4">
        <Wordmark className="text-[150px] lg:text-[210px]" />
        <div className="font-baloo txt-gold text-[40px] font-semibold leading-none lg:text-[52px]">
          The Helm
        </div>
      </div>

      {info?.mode === "local" ? (
        <div className="flex w-72 flex-col gap-2.5">
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && loginLocal()}
            placeholder="access token (SOKKAN_LOCAL_TOKEN)"
            autoFocus
            className="rounded-xl border border-line bg-panel2 px-4 py-3 text-[14px] text-slate-100 outline-none focus:border-sea/50"
          />
          <button
            onClick={loginLocal}
            disabled={!token.trim()}
            className="rounded-xl bg-gradient-to-r from-[#6E49EA] via-[#4870E2] to-[#1C9ED9] px-10 py-3 text-[15px] font-semibold text-white shadow-lg shadow-[#4870E2]/25 ring-1 ring-white/10 transition hover:brightness-110 active:scale-[0.98] disabled:opacity-40"
          >
            Sign in
          </button>
          {err && <div className="text-center text-[12px] text-red-300">{err}</div>}
        </div>
      ) : (
        <a
          href="/api/auth/login"
          className="rounded-xl bg-gradient-to-r from-[#6E49EA] via-[#4870E2] to-[#1C9ED9] px-10 py-3 text-[15px] font-semibold text-white shadow-lg shadow-[#4870E2]/25 ring-1 ring-white/10 transition hover:brightness-110 active:scale-[0.98]"
        >
          Sign in (SSO)
        </a>
      )}
    </div>
  );
}

export const useMe = () => useContext(Ctx);
export function useCan(min: "dev" | "admin" | "owner") {
  const me = useMe();
  return !me || rank(me.role) >= rank(min);
}
