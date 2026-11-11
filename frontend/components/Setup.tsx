"use client";
// 3.2.2 — the Setup plane: Organization · Engines · Magnitude · Secrets · My account ·
// Notifications. The pages are the former « Profile & organization » sections (SetupPages).
import Magnitude from "./Magnitude";
import StudyPage from "./StudyPage";
import dynamic from "next/dynamic";
import { useLang } from "@/lib/studyText";
import { AccountPage, EnginesPage, Notifications, OrganizationPage, Secrets } from "./SetupPages";
import type { SubTab } from "@/lib/planes";

// the review screen comes from the simple profile's bundle: loaded only when the tab is opened
const IngestEmbedded = dynamic(() => import("./simple/IngestEmbedded"), { ssr: false });

const TITLES: Partial<Record<SubTab, string>> = {
  organization: "Organization", engines: "Engines", secrets: "Secrets", account: "My account",
  notifications: "Notifications",
};

export default function Setup({ tab, section }: { tab: SubTab; section?: string }) {
  if (tab === "magnitude") return <Magnitude />;
  if (tab === "study") return (      // feature `study_mode`: its own title and language switch
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-5xl p-4 md:p-6"><StudyPage section={section} /></div>
    </div>
  );
  if (tab === "ingest") return <IngestPage />;   // feature `doc_ingest`
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className={`mx-auto w-full p-4 md:p-6 ${tab === "organization" ? "max-w-5xl" : "max-w-3xl"}`}>
        <h2 className="mb-3 text-[15px] font-semibold text-slate-100">{TITLES[tab]}</h2>
        {tab === "organization" ? <OrganizationPage section={section} />
          : tab === "engines" ? <EnginesPage />
          : tab === "secrets" ? <Secrets />
          : tab === "notifications" ? <Notifications />
          : <AccountPage />}
      </div>
    </div>
  );
}

/** Setup › Ingestion — the review of the ingestion batches is ONE screen, written for the
 *  simple profile (components/simple/IngestReview.tsx): here it is embedded with the same
 *  tokens, in the cockpit's night theme, with the language switch Setup › Study already uses. */
function IngestPage() {
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
      <IngestEmbedded lang={lang} />
    </div>
  );
}
