// 3.6.7 — the public demo of SOKKAN Atelier (feature `demo_atelier`). Every visitor of the
// demo is the SAME identity: what a visitor chooses (language, theme, Atelier or Cockpit) is
// kept in THEIR browser, never written to the server — one visitor switching to English would
// otherwise have switched every next visitor too. Each read is guarded: storage may be blocked.
import type { Prefs } from "./api";

const UI = "sokkan_demo_ui";
const LANG = "sokkan_demo_lang";
const THEME = "sokkan_demo_theme";

function read(key: string): string {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}

function write(key: string, value: string): void {
  try { localStorage.setItem(key, value); } catch { /* private mode: the choice lasts this page */ }
}

/** The interface the visitor asked for: `?ui=atelier|cockpit` (the link of the site's button),
 *  else their last choice in this browser, else null (the instance's default). */
export function demoInterface(): "simple" | "technical" | null {
  if (typeof window === "undefined") return null;
  const q = new URLSearchParams(window.location.search).get("ui");
  const asked = q === "atelier" ? "simple" : q === "cockpit" ? "technical" : "";
  if (asked) write(UI, asked);
  const v = asked || read(UI);
  return v === "simple" || v === "technical" ? v : null;
}

/** Leave for the other interface (a full load: the two are separate bundles). */
export function switchDemoInterface(to: "simple" | "technical"): void {
  write(UI, to);
  const q = new URLSearchParams(window.location.search);
  q.delete("w"); q.delete("v");
  q.set("ui", to === "simple" ? "atelier" : "cockpit");
  window.location.assign(`/?${q.toString()}`);
}

/** The server's preferences with the visitor's own choices on top: their language and theme
 *  (the device's by default — the demo identity's stored theme is nobody's). */
export function demoPrefs(p: Prefs, profile: "simple" | "technical" | null): Prefs {
  const lang = read(LANG);
  const theme = read(THEME);
  return {
    ...p,
    profile: profile || p.profile,
    lang: lang === "fr" || lang === "en" || lang === "auto" ? lang : "auto",
    theme: theme === "light" || theme === "dark" || theme === "auto" ? theme : "auto",
    locked: false,
  };
}

export function saveDemoPrefs(p: { lang?: string; theme?: string }): void {
  if (p.lang) write(LANG, p.lang);
  if (p.theme) write(THEME, p.theme);
}
