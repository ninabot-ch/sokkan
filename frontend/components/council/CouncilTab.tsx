"use client";
// Build › Council in the technical cockpit: the room, embedded, with the language switch the other
// embedded screens (Setup › Ingestion, Study) use.
import dynamic from "next/dynamic";
import { useLang } from "@/lib/studyText";

const CouncilEmbedded = dynamic(() => import("./CouncilEmbedded"), { ssr: false });

export default function CouncilTab() {
  const [lang, setLang] = useLang();
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-6xl justify-end px-4 pt-3 md:px-6">
        <div role="group" aria-label="Language" className="inline-flex overflow-hidden rounded-lg border border-line text-[12.5px]">
          {(["fr", "en"] as const).map((l) => (
            <button key={l} aria-pressed={lang === l} onClick={() => setLang(l)}
              className={`ui-focus px-2.5 py-1 ${lang === l ? "bg-panel2 font-semibold text-slate-100" : "text-mut hover:text-slate-200"}`}>
              {l === "fr" ? "Français" : "English"}
            </button>
          ))}
        </div>
      </div>
      <CouncilEmbedded lang={lang} />
    </div>
  );
}
