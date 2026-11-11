"use client";
// Simple profile — light i18n: two dictionaries (dict.ts), the person's language (their
// setting, else the browser's), a `t(key, vars)` hook. The technical profile does not use it.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { en, fr, type Key } from "./dict";

export type Lang = "fr" | "en";
export type LangPref = "auto" | Lang;
export type Theme = "light" | "dark";
export type ThemePref = Theme | "auto";

const DICTS: Record<Lang, Record<Key, string>> = { fr, en };

export function browserLang(): Lang {
  if (typeof navigator === "undefined") return "en";
  const list = navigator.languages?.length ? navigator.languages : [navigator.language || "en"];
  for (const l of list) {
    if (/^fr\b/i.test(l)) return "fr";
    if (/^en\b/i.test(l)) return "en";
  }
  return "en";
}

export const resolveLang = (pref: LangPref): Lang => (pref === "fr" || pref === "en" ? pref : browserLang());

export function translate(lang: Lang, key: Key, vars?: Record<string, string | number>): string {
  const s = DICTS[lang][key] ?? DICTS.en[key] ?? key;
  return vars ? s.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m)) : s;
}

export type T = (key: Key, vars?: Record<string, string | number>) => string;

interface Ctx { lang: Lang; t: T; theme: Theme; langPref: LangPref; themePref: ThemePref;
  setLangPref: (l: LangPref) => void; setThemePref: (t: ThemePref) => void }

const C = createContext<Ctx | null>(null);

function cookie(name: string, value: string | null) {
  try {
    document.cookie = value === null
      ? `${name}=; path=/; max-age=0; samesite=lax`
      : `${name}=${value}; path=/; max-age=31536000; samesite=lax`;
  } catch { /* cookies blocked: the page still works, only the first paint differs */ }
}

/** Language + theme of the simple profile. While mounted it owns <html lang> and
 *  <html data-sk-theme> (the tokens of app/simple.css exist only then); unmounting — the person
 *  went back to the technical profile — gives the page back as it was. */
export function SimpleUIProvider({ initialLang, initialTheme, onChange, children }: {
  initialLang: LangPref; initialTheme: ThemePref;
  onChange: (p: { lang?: LangPref; theme?: ThemePref }) => void; children: React.ReactNode;
}) {
  const [langPref, setLang] = useState<LangPref>(initialLang);
  const [themePref, setTheme] = useState<ThemePref>(initialTheme);
  const [systemDark, setSystemDark] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = () => setSystemDark(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  const lang = resolveLang(langPref);
  const theme: Theme = themePref === "auto" ? (systemDark ? "dark" : "light") : themePref;

  useEffect(() => {
    const el = document.documentElement;
    const before = el.lang;
    el.lang = lang;
    cookie("sokkan_lang", lang);
    return () => { el.lang = before || "en"; };
  }, [lang]);
  useEffect(() => {
    const el = document.documentElement;
    el.dataset.skTheme = theme;
    cookie("sokkan_sk_theme", theme);
    const meta = document.querySelector('meta[name="theme-color"]');
    const prev = meta?.getAttribute("content") ?? null;
    meta?.setAttribute("content", getComputedStyle(el).getPropertyValue("--sk-bg").trim() || prev || "");
    return () => { delete el.dataset.skTheme; if (prev !== null) meta?.setAttribute("content", prev); };
  }, [theme]);
  // leaving the simple profile: the cookies that pre-paint it go too
  useEffect(() => () => { cookie("sokkan_lang", null); cookie("sokkan_sk_theme", null); }, []);

  const t = useCallback<T>((key, vars) => translate(lang, key, vars), [lang]);
  const value = useMemo<Ctx>(() => ({
    lang, t, theme, langPref, themePref,
    setLangPref: (l) => { setLang(l); onChange({ lang: l }); },
    setThemePref: (x) => { setTheme(x); onChange({ theme: x }); },
  }), [lang, t, theme, langPref, themePref, onChange]);
  return <C.Provider value={value}>{children}</C.Provider>;
}

/** The same context for ONE screen of the simple profile shown inside the technical cockpit
 *  (Setup › Ingestion): language and theme are given, <html> is not touched, nothing is saved. */
export function EmbeddedUIProvider({ lang, theme, children }: { lang: Lang; theme: Theme; children: React.ReactNode }) {
  const t = useCallback<T>((key, vars) => translate(lang, key, vars), [lang]);
  const value = useMemo<Ctx>(() => ({
    lang, t, theme, langPref: lang, themePref: theme, setLangPref: () => {}, setThemePref: () => {},
  }), [lang, t, theme]);
  return <C.Provider value={value}>{children}</C.Provider>;
}

export function useUI(): Ctx {
  const c = useContext(C);
  if (!c) throw new Error("useUI outside SimpleUIProvider");
  return c;
}
export const useT = (): T => useUI().t;

// ---- small formatters, worded through the dictionary ----------------------------------------
export function when(t: T, ts: number | null | undefined, lang: Lang): string {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return t("when.now");
  if (s < 3600) return t("when.min", { n: Math.round(s / 60) });
  if (s < 86400) return t("when.hour", { n: Math.round(s / 3600) });
  if (s < 2 * 86400) return t("when.yesterday");
  if (s < 14 * 86400) return t("when.days", { n: Math.round(s / 86400) });
  return new Date(ts * 1000).toLocaleDateString(lang === "fr" ? "fr-CH" : "en-GB", { day: "numeric", month: "long", year: "numeric" });
}

/** « modifié il y a 14 mois » — the age of a source, and whether it is old enough to flag. */
export function age(t: T, ts: number | null | undefined): { text: string; old: boolean } {
  if (!ts) return { text: "", old: false };
  const days = Math.max(0, Math.floor((Date.now() / 1000 - ts) / 86400));
  const old = days > 365;
  if (days < 1) return { text: t("age.today"), old };
  if (days < 2) return { text: t("age.day"), old };
  if (days < 45) return { text: t("age.days", { n: days }), old };
  const months = Math.round(days / 30.44);
  if (months < 2) return { text: t("age.month"), old };
  if (months < 24) return { text: t("age.months", { n: months }), old };
  const years = Math.floor(days / 365.25);
  return { text: years < 2 ? t("age.year") : t("age.years", { n: years }), old };
}

export function size(t: T, bytes: number): string {
  if (bytes < 1024 * 1024) return t("docs.size.kb", { n: Math.max(1, Math.round(bytes / 1024)) });
  return t("docs.size.mb", { n: (bytes / 1024 / 1024).toFixed(1).replace(".", ",") });
}

export function fullDate(ts: number, lang: Lang): string {
  return new Date(ts * 1000).toLocaleString(lang === "fr" ? "fr-CH" : "en-GB",
    { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
