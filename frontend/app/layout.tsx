import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

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

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={baloo.variable}>
      <body>{children}</body>
    </html>
  );
}
