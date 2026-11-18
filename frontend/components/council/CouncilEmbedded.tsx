"use client";
// The council room INSIDE the technical cockpit (Build › Council): the simple profile's screen with
// its tokens on its own element, a given language and the night theme (same pattern as Setup › Ingestion).
import { EmbeddedUIProvider, type Lang } from "@/lib/simple/i18n";
import CouncilRoom from "./CouncilRoom";

export default function CouncilEmbedded({ lang }: { lang: Lang }) {
  return (
    <div className="sk sk-embed" data-sk-theme="dark" lang={lang}>
      <EmbeddedUIProvider lang={lang} theme="dark"><CouncilRoom /></EmbeddedUIProvider>
    </div>
  );
}
