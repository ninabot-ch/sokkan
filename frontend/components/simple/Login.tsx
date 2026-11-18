"use client";
// The sign-in screen for people who are not engineers (`plain` of GET /api/auth/info: named
// accounts with a personal access code, or the simple profile by default). The browser's
// language, the light theme of the simple profile, no variable name. The technical sign-in
// screen (lib/me.tsx) is untouched and does not load this file.
import { useEffect, useMemo, useState } from "react";
import { browserLang, translate, type Lang, type T, type Theme } from "@/lib/simple/i18n";

function cookieOf(name: string): string {
  try {
    const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
    return m ? decodeURIComponent(m[1]) : "";
  } catch { return ""; }
}

export default function Login({ mode }: { mode: string }) {
  // a language or a theme the person chose on an earlier visit wins; else the browser's
  // language and the light theme
  const lang = useMemo<Lang>(() => { const c = cookieOf("sokkan_lang"); return c === "fr" || c === "en" ? c : browserLang(); }, []);
  const theme = useMemo<Theme>(() => (cookieOf("sokkan_sk_theme") === "dark" ? "dark" : "light"), []);
  const t = useMemo<T>(() => (key, vars) => translate(lang, key, vars), [lang]);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // 4.0 — an invitation link (/?invite=…): one click signs the person in and shows their own
  // code once, to keep for the next visits (backend/onboarding.py, POST /api/auth/invite)
  const invite = useMemo(() => {
    if (typeof window === "undefined") return "";
    const v = new URLSearchParams(window.location.search).get("invite") || "";
    return /^[A-Za-z0-9_-]{16,80}$/.test(v) ? v : "";
  }, []);
  const [inv, setInv] = useState<"ask" | "busy" | { code: string; name: string } | "gone">(invite ? "ask" : "gone");
  const acceptInvite = async () => {
    setInv("busy"); setErr("");
    try {
      const r = await fetch("/api/auth/invite", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ token: invite }) });
      if (r.ok) { const d = await r.json(); setInv({ code: d.code, name: d.name }); return; }
      setErr(t(r.status === 410 || r.status === 404 ? "login.invite.gone" : r.status === 429 ? "login.throttled" : "login.failed"));
    } catch { setErr(t("login.failed")); }
    setInv("gone");
  };
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const el = document.documentElement;
    const before = { lang: el.lang, theme: el.dataset.skTheme };
    el.lang = lang;
    el.dataset.skTheme = theme;
    document.title = `${t("login.title")} — SOKKAN`;
    return () => { el.lang = before.lang || "en"; if (before.theme) el.dataset.skTheme = before.theme; else delete el.dataset.skTheme; };
  }, [lang, theme, t]);

  const next = typeof window === "undefined" ? "/" : window.location.pathname + window.location.search;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!code.trim() || busy) return;
    setBusy(true); setErr("");
    try {
      const r = await fetch("/api/auth/local", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ token: code.trim() }) });
      if (r.ok) { window.location.reload(); return; }
      setErr(t(r.status === 429 ? "login.throttled" : r.status === 401 ? "login.invalid" : "login.failed"));
    } catch { setErr(t("login.failed")); }
    setBusy(false);
  };

  return (
    <div className="sk sk-login">
      <main className="sk-login-card">
        <span className="sk-wordmark sk-login-brand" role="img" aria-label="SOKKAN">
          <span className="g" aria-hidden>S</span>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/sokkan-icon.svg" alt="" />
          <span className="g" aria-hidden>K</span><span className="k" aria-hidden>K</span><span className="g" aria-hidden>AN</span>
        </span>
        <h1 className="sk-login-title">{t(invite && inv !== "gone" ? "login.invite.title" : "login.title")}</h1>
        {mode === "local" && invite && inv !== "gone" ? (
          typeof inv === "object" ? (
            <div className="sk-login-form">
              <p>{t("login.invite.code", { name: inv.name })}</p>
              <code className="sk-ob-secret" tabIndex={0}>{inv.code}</code>
              <button className="sk-btn" type="button" onClick={async () => {
                try { await navigator.clipboard.writeText(inv.code); setCopied(true); } catch { /* selectable */ }
              }}>{t(copied ? "login.invite.copied" : "login.invite.copy")}</button>
              <p className="sk-small sk-muted">{t("login.invite.keep")}</p>
              <button className="sk-btn sk-btn-primary sk-login-submit" type="button"
                onClick={() => window.location.assign("/")}>{t("login.invite.enter")}</button>
            </div>
          ) : (
            <div className="sk-login-form">
              <p>{t("login.invite.text")}</p>
              <button className="sk-btn sk-btn-primary sk-login-submit" type="button" onClick={acceptInvite} disabled={inv === "busy"}>
                {inv === "busy" ? t("login.busy") : t("login.invite.accept")}</button>
            </div>
          )
        ) : mode === "local" ? (
          <form className="sk-login-form" onSubmit={submit}>
            <label className="sk-field">
              <span>{t("login.code")}</span>
              <input className="sk-input" type="password" name="access-code" autoComplete="current-password" autoFocus
                value={code} onChange={(e) => setCode(e.target.value)} aria-describedby="sk-login-hint" aria-invalid={!!err} />
            </label>
            <p className="sk-small sk-muted" id="sk-login-hint">{t("login.hint")}</p>
            {err && <p className="sk-login-error" role="alert">{err}</p>}
            <button className="sk-btn sk-btn-primary sk-login-submit" type="submit" disabled={!code.trim() || busy}>
              {busy ? t("login.busy") : t("login.submit")}
            </button>
          </form>
        ) : (
          <a className="sk-btn sk-btn-primary sk-login-submit" href={`/api/auth/login?next=${encodeURIComponent(next)}`}>{t("login.submit")}</a>
        )}
      </main>
    </div>
  );
}
