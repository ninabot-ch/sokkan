"use client";
// 4.0.1 — the organisation at the top of the Atelier: its logo and its name (Settings ›
// Organisation). The logo sent in the first launch was shown nowhere. Nothing when neither is set.
import { useEffect, useState } from "react";
import { fetchOrgProfile } from "@/lib/simple/org";
import { logoUrl } from "@/lib/simple/orgLogo";

export default function OrgBadge() {
  const [org, setOrg] = useState<{ name: string; logo: boolean; v: number } | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => fetchOrgProfile()
      .then((p) => { if (alive) setOrg({ name: p.name || "", logo: !!p.logo, v: Number(p.logo_v || p.updated_at || 0) }); })
      .catch(() => { if (alive) setOrg(null); });
    load();
    // Settings › Organisation says when it changed (logo or name), the badge follows
    window.addEventListener("sk-org-changed", load);
    return () => { alive = false; window.removeEventListener("sk-org-changed", load); };
  }, []);
  if (!org || (!org.name && !org.logo)) return null;
  return (
    <div className="sk-org-badge">
      {org.logo && <img src={logoUrl(org.v)} alt="" className="sk-org-badge-logo" />}
      {org.name && <span className="sk-org-badge-name">{org.name}</span>}
    </div>
  );
}
