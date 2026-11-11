"use client";
// The review of the ingestion batches INSIDE the technical cockpit (Setup › Ingestion): the
// simple profile's screen with its tokens on its own element (`.sk.sk-embed`), a given language
// and the night theme — <html> is not touched, the rest of the cockpit keeps its styles.
import { EmbeddedUIProvider, type Lang } from "@/lib/simple/i18n";
import IngestReview from "./IngestReview";

export default function IngestEmbedded({ lang }: { lang: Lang }) {
  return (
    <div className="sk sk-embed" data-sk-theme="dark" lang={lang}>
      <EmbeddedUIProvider lang={lang} theme="dark"><IngestReview /></EmbeddedUIProvider>
    </div>
  );
}
