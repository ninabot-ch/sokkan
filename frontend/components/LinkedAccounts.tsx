"use client";
import { useEffect, useState } from "react";
import {
  forgeLinkUrl, forgeLinks, forgeRefresh, forgeStatus, forgeUnlink,
  type ForgeLink, type ForgeProjectAccess, type ForgeStatus,
} from "@/lib/forge";

const btn = "rounded bg-sea/80 px-2.5 py-1 text-[11.5px] font-medium text-white hover:bg-sea disabled:opacity-40";
const ghost = "rounded border border-line px-2 py-0.5 text-[11px] text-mut hover:text-slate-200";
const when = (t: number | null) => (t ? new Date(t * 1000).toLocaleString() : "—");
const STATE: Record<ForgeLink["state"], string> = {
  active: "text-emerald-300", expired: "text-amber-300", revoked: "text-red-300",
};

/** 3.2 lot 5 — Profile → Linked accounts: link / unlink GitLab, linked identity, scopes,
 *  token expiry (refreshed server-side), and the access GitLab gives in each project. */
export default function LinkedAccounts() {
  const [st, setSt] = useState<ForgeStatus | null>(null);
  const [links, setLinks] = useState<ForgeLink[]>([]);
  const [projects, setProjects] = useState<ForgeProjectAccess[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const flash = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("forge") : null;
  const load = () => forgeLinks().then((d) => { setLinks(d.links); setProjects(d.projects); }).catch(() => {});
  useEffect(() => { forgeStatus().then((s) => { setSt(s); if (s.enabled) load(); }).catch((e) => setErr(String(e.message || e))); }, []);
  const run = (p: Promise<unknown>) => { setBusy(true); setErr(""); p.then(load).catch((e) => setErr(String(e.message || e))).finally(() => setBusy(false)); };
  if (!st) return <div className="text-[12px] text-mut">{err || "…"}</div>;
  if (!st.enabled) return <div className="text-[12px] text-mut">No forge integration on this instance (feature <code>gitlab</code> is off).</div>;
  return (
    <div className="space-y-3 text-[12.5px]">
      <div className="text-[10.5px] text-mut">
        Projects whose access comes from GitLab read <b>your</b> level with your own account, and your sessions push
        with <b>your</b> token — never a shared account. The token stays encrypted on the server; sessions get it only
        through git, for the project&apos;s GitLab.
      </div>
      {flash === "linked" && <div className="rounded border border-emerald-400/40 bg-emerald-500/10 px-2 py-1 text-[11.5px] text-emerald-300">GitLab account linked.</div>}
      {flash === "error" && <div className="rounded border border-red-400/40 bg-red-500/10 px-2 py-1 text-[11.5px] text-red-300">GitLab did not link the account (consent refused or expired).</div>}
      {err && <div className="rounded border border-red-400/40 bg-red-500/10 px-2 py-1 text-[11.5px] text-red-300">{err}</div>}
      {st.providers.map((p) => {
        const l = links.find((x) => x.provider === p.provider);
        return (
          <div key={p.provider} className="rounded-lg border border-line bg-panel2/40 p-3">
            <div className="flex items-center gap-2">
              <span className="text-[13.5px] font-medium text-slate-100">GitLab</span>
              <code className="text-[11px] text-mut">{p.base_url}</code>
              {l && <span className={`ml-1 text-[11px] ${STATE[l.state]}`}>● {l.state}</span>}
              <span className="ml-auto flex gap-1.5">
                {(!l || l.state !== "active") && (
                  <a href={forgeLinkUrl(p.provider)} aria-disabled={!p.configured}
                    className={`${btn} ${p.configured ? "" : "pointer-events-none opacity-40"}`}>{l ? "Link again" : "Link GitLab"}</a>
                )}
                {l && <button disabled={busy} className={ghost} onClick={() => run(forgeUnlink(p.provider))}>Unlink</button>}
              </span>
            </div>
            {!p.configured && <div className="mt-1 text-[11px] text-amber-300">The administrator has not registered the GitLab application yet.</div>}
            {l ? (
              <dl className="mt-2 grid grid-cols-[110px_1fr] gap-y-0.5 text-[11.5px]">
                <dt className="text-mut">Account</dt><dd className="text-slate-200">@{l.forge_username} <span className="text-mut">(id {l.forge_user_id})</span></dd>
                <dt className="text-mut">Scopes</dt><dd className="font-mono text-[11px] text-slate-300">{l.scopes}</dd>
                <dt className="text-mut">Linked</dt><dd className="text-slate-300">{when(l.linked_at)}</dd>
                <dt className="text-mut">Token expires</dt><dd className="text-slate-300">{when(l.expires_at)} <span className="text-mut">(renewed automatically)</span></dd>
                {l.revoked_at && <><dt className="text-mut">Revoked</dt><dd className="text-red-300">{when(l.revoked_at)} — link it again</dd></>}
              </dl>
            ) : (
              <div className="mt-1.5 text-[11px] text-mut">Asks GitLab for: <span className="font-mono">{p.scopes.join(" ")}</span> (no <span className="font-mono">api</span> scope).</div>
            )}
          </div>
        );
      })}
      {projects.length > 0 && (
        <div className="rounded-lg border border-line bg-panel2/40 p-3">
          <div className="flex items-center">
            <span className="text-[11px] text-mut">Your access from GitLab (lowest level over each project&apos;s repositories)</span>
            <button disabled={busy} className={`${ghost} ml-auto`} onClick={() => run(forgeRefresh())}>Refresh my access</button>
          </div>
          <ul className="mt-1.5 space-y-1">
            {projects.map((p) => (
              <li key={p.slug} className="flex items-center gap-2 text-[12px]">
                <span className="text-slate-200">{p.name}</span>
                <span className={p.role ? "text-emerald-300" : "text-mut"}>{p.role ?? "no access"}</span>
                <span className="ml-auto text-[10.5px] text-mut">{p.repos.map((r) => r.repo_path).join(", ")} · checked {when(p.computed_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
