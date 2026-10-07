"use client";
// 3.2 `shared_review` — share a session or a preview with a person / a team of its
// project, read or read-write, optionally for a limited time; a write share of a session
// can carry the delegated HITL approval (« ask X to validate »). Popout over the view.
import { useEffect, useRef, useState } from "react";
import {
  shareApprover, shareCreate, sharePeople, shareRevoke, sharesOf, when,
  type Share, type ShareKind, type SharePerson,
} from "@/lib/uifeatures";

const DURATIONS: [string, number | null][] = [
  ["no end date", null], ["1 hour", 3600], ["1 day", 86400], ["7 days", 7 * 86400], ["30 days", 30 * 86400],
];

export function AccessChip({ access }: { access: "read" | "write" }) {
  return (
    <span className={`ui-chip ${access === "write" ? "ui-c-write" : "ui-c-read"}`}>
      <span aria-hidden>{access === "write" ? "✎" : "👁"}</span>{access === "write" ? "can reply" : "read only"}
    </span>
  );
}

export default function ShareDialog({ kind, target, title, path, env, onClose }: {
  kind: ShareKind; target: string; title?: string; path?: string; env?: string; onClose: () => void;
}) {
  const [people, setPeople] = useState<SharePerson[] | null>(null);
  const [shares, setShares] = useState<Share[]>([]);
  const [who, setWho] = useState("");
  const [access, setAccess] = useState<"read" | "write">("read");
  const [ttl, setTtl] = useState<number | null>(null);
  const [approver, setApprover] = useState(false);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  const box = useRef<HTMLDivElement>(null);

  const reload = () => sharesOf(kind, target).then(setShares).catch(() => setShares([]));
  useEffect(() => {
    sharePeople(kind, target).then(setPeople).catch((e) => { setPeople([]); setErr(String(e.message || e)); });
    reload();
    box.current?.querySelector<HTMLElement>("#share-who")?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, target]);

  const picked = people?.find((p) => `${p.kind}:${p.id}` === who);
  const writeOk = !!picked?.write_ok;
  useEffect(() => { if (!writeOk) { setAccess("read"); setApprover(false); } }, [writeOk]);
  useEffect(() => { if (access === "read") setApprover(false); }, [access]);

  const submit = async () => {
    if (!picked) return;
    setBusy(true); setErr(""); setDone("");
    try {
      await shareCreate({ kind, target, principal_kind: picked.kind, principal: picked.id, access,
        expires_in_s: ttl, approver: approver && kind === "session", note, title, path, env });
      setDone(`Shared with ${picked.id}${approver ? " — asked to validate" : ""}.`);
      setWho(""); setNote(""); setApprover(false);
      reload();
    } catch (e) { setErr(String((e as Error).message || e)); } finally { setBusy(false); }
  };

  const active = shares.filter((s) => !s.revoked_at && (!s.expires_at || s.expires_at * 1000 > Date.now()));
  return (
    <div className="fixed inset-0 z-[70] flex items-start justify-center bg-black/60 p-4 pt-16" onClick={onClose}>
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby="share-title"
        className="w-full max-w-lg overflow-hidden rounded-2xl border border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-line px-4 py-2.5">
          <span aria-hidden className="text-[15px]">⇪</span>
          <div className="min-w-0">
            <div id="share-title" className="text-[13.5px] font-medium text-slate-100">
              Share this {kind === "session" ? "session" : "preview"} for review</div>
            <div className="truncate text-[11px] text-mut">{title || target}</div>
          </div>
          <button onClick={onClose} aria-label="close" className="ui-focus ml-auto rounded px-1.5 text-mut hover:text-slate-200">✕</button>
        </div>
        <div className="space-y-3 p-4 text-[12.5px]">
          <div>
            <label htmlFor="share-who" className="mb-1 block text-[11px] text-mut">With (people and teams of this project only)</label>
            <select id="share-who" value={who} onChange={(e) => setWho(e.target.value)}
              className="ui-focus w-full rounded border border-line bg-panel2 px-2 py-1.5 text-slate-100">
              <option value="">{people === null ? "loading…" : people.length ? "pick a person or a team…" : "nobody else in this project"}</option>
              {people?.map((p) => (
                <option key={`${p.kind}:${p.id}`} value={`${p.kind}:${p.id}`}>
                  {p.kind === "team" ? "team " : ""}{p.id} — {p.role}</option>
              ))}
            </select>
          </div>
          <fieldset>
            <legend className="mb-1 text-[11px] text-mut">Access</legend>
            <div className="flex gap-2">
              {(["read", "write"] as const).map((a) => {
                const dis = a === "write" && !writeOk;
                return (
                  <label key={a} className={`flex flex-1 cursor-pointer items-start gap-2 rounded-lg border p-2 ${access === a ? "border-sea bg-sea/10" : "border-line"} ${dis ? "cursor-not-allowed opacity-50" : ""}`}>
                    <input type="radio" name="share-access" value={a} checked={access === a} disabled={dis}
                      onChange={() => setAccess(a)} className="ui-focus mt-0.5 accent-sky-500" />
                    <span>
                      <span className="block text-slate-100">{a === "read" ? "Read" : "Read & write"}</span>
                      <span className="block text-[10.5px] text-mut">{a === "read" ? "follows the work, cannot act"
                        : dis && picked ? `${picked.id} is ${picked.role} here: a viewer never gets write` : "can reply in the session"}</span>
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>
          {kind === "session" && (
            <label className={`flex items-start gap-2 rounded-lg border border-line p-2 ${access !== "write" ? "opacity-50" : ""}`}>
              <input type="checkbox" checked={approver} disabled={access !== "write"} onChange={(e) => setApprover(e.target.checked)}
                className="ui-focus mt-0.5 accent-amber-400" />
              <span>
                <span className="block text-slate-100">Ask {picked?.id ?? "them"} to validate</span>
                <span className="block text-[10.5px] text-mut">The session&apos;s tool approvals (HITL) are delegated: they see them
                  in their rail and allow or deny. You keep the hand too. Notified now.</span>
              </span>
            </label>
          )}
          <div className="flex gap-2">
            <div className="flex-1">
              <label htmlFor="share-ttl" className="mb-1 block text-[11px] text-mut">Duration</label>
              <select id="share-ttl" value={ttl ?? ""} onChange={(e) => setTtl(e.target.value ? Number(e.target.value) : null)}
                className="ui-focus w-full rounded border border-line bg-panel2 px-2 py-1.5 text-slate-100">
                {DURATIONS.map(([l, v]) => <option key={l} value={v ?? ""}>{l}</option>)}
              </select>
            </div>
            <div className="flex-[2]">
              <label htmlFor="share-note" className="mb-1 block text-[11px] text-mut">Note (optional)</label>
              <input id="share-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="what should they look at?"
                className="ui-focus w-full rounded border border-line bg-[#0b0f16] px-2 py-1.5 text-slate-100 outline-none" />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={submit} disabled={!picked || busy}
              className="ui-focus rounded bg-sea/80 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-sea disabled:opacity-40">
              {busy ? "sharing…" : "share"}</button>
            <span className="text-[10.5px] text-mut">Logged in the journal · revocable at any time</span>
          </div>
          <div aria-live="polite">
            {done && <div className="text-[11.5px] text-emerald-300">✓ {done}</div>}
            {err && <div role="alert" className="text-[11.5px] text-red-400">{err}</div>}
          </div>

          <div className="border-t border-line pt-3">
            <div className="mb-1.5 text-[11px] text-mut">Shared with ({active.length})</div>
            {!active.length && <div className="text-[11.5px] text-mut/80">not shared yet</div>}
            <ul className="space-y-1.5">
              {active.map((s) => (
                <li key={s.id} className="flex flex-wrap items-center gap-1.5 rounded-lg border border-line bg-panel2/40 px-2 py-1.5">
                  <span className="text-slate-200">{s.principal_kind === "team" ? "team " : ""}{s.principal}</span>
                  <AccessChip access={s.access} />
                  {s.approver && <span className="ui-chip ui-c-approval"><span aria-hidden>✋</span>validates</span>}
                  <span className="text-[10.5px] text-mut">by {s.created_by} · {s.expires_at ? `until ${when(s.expires_at)}` : "no end date"}</span>
                  <span className="ml-auto flex gap-1">
                    {kind === "session" && s.access === "write" && (
                      <button onClick={() => shareApprover(s.id, !s.approver).then(reload).catch((e) => setErr(String(e.message || e)))}
                        className="ui-focus rounded border border-line px-1.5 text-[10.5px] text-mut hover:text-slate-200">
                        {s.approver ? "stop delegating" : `ask to validate`}</button>
                    )}
                    <button onClick={() => shareRevoke(s.id).then(reload).catch((e) => setErr(String(e.message || e)))}
                      className="ui-focus rounded border border-red-500/40 px-1.5 text-[10.5px] text-red-300 hover:bg-red-500/10">revoke</button>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
