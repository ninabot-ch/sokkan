"use client";
// 3.2.2 — the Setup plane: Organization · Engines · Magnitude · Secrets · My account ·
// Notifications. The pages are the former « Profile & organization » sections (SetupPages).
import Magnitude from "./Magnitude";
import { AccountPage, EnginesPage, Notifications, OrganizationPage, Secrets } from "./SetupPages";
import type { SubTab } from "@/lib/planes";

const TITLES: Partial<Record<SubTab, string>> = {
  organization: "Organization", engines: "Engines", secrets: "Secrets", account: "My account",
  notifications: "Notifications",
};

export default function Setup({ tab, section }: { tab: SubTab; section?: string }) {
  if (tab === "magnitude") return <Magnitude />;
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
