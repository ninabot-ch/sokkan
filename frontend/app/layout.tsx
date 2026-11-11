import type { Metadata } from "next";
import localFont from "next/font/local";
import { cookies } from "next/headers";
import "./globals.css";
import "./simple.css";

// Baloo 2 (SIL OFL 1.1, fonts/Baloo2-OFL.txt), latin subset, variable weight: shipped in
// the repository rather than fetched from Google Fonts at build time, so that a
// self-hosted `docker compose up --build` does not depend on fonts.googleapis.com
// (a flaky answer failed an update build; an offline machine could not build at all).
const baloo = localFont({
  src: [{ path: "./fonts/baloo2-latin.woff2", weight: "400 800", style: "normal" }],
  variable: "--font-baloo",
  display: "swap",
});

export const metadata: Metadata = {
  title: "SOKKAN",
  description: "La barre, pas l'autopilote.",
  manifest: "/site.webmanifest",
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/favicon/favicon-32.png", type: "image/png", sizes: "32x32" },
    ],
    apple: "/favicon/apple-touch-icon.png",
  },
};

export const viewport = { themeColor: "#0B0C0F" };

// The technical cockpit is in English and dark. The simple profile (feature `simple_profile`)
// has its own language and theme: its shell leaves two cookies so that the very first paint of
// the next visit already carries the right <html lang> and the right tokens (no dark flash, a
// screen reader announces the right language). No cookie = the cockpit as it always was.
export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const jar = await cookies();
  const lang = jar.get("sokkan_lang")?.value;
  const theme = jar.get("sokkan_sk_theme")?.value;
  return (
    <html lang={lang === "fr" || lang === "en" ? lang : "en"} className={baloo.variable}
      data-sk-theme={theme === "light" || theme === "dark" ? theme : undefined}>
      <body>{children}</body>
    </html>
  );
}
