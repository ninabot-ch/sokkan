"use client";
import { useCallback, useEffect, useState } from "react";
import {
  memoryLicence, memoryRollback, memorySwitch, memorySwitchDecide, memorySwitchState, memoryView,
} from "@/lib/api";
import type { MemProfile, MemoryView, SwitchJob, SwitchState } from "@/lib/api";

// Carte « Mémoire » de Magnitude : profil du moteur de mémoire (CortHeXis), changement
// surveillé par le banc de recall, retour arrière, licence du modèle recommandé.

const MODELS: { id: string; label: string; licence: string }[] = [
  { id: "", label: "keep the installed model", licence: "" },
  { id: "embeddinggemma-300m-q8", label: "EmbeddingGemma (Google) — best recall", licence: "Gemma Terms of Use" },
  { id: "multilingual-e5-base-q8", label: "multilingual-e5-base — open licence (MIT)", licence: "MIT" },
];

const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
const n3 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3));

function Bar({ p }: { p: number }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-line/60">
      <div className="h-full rounded-full bg-gradient-to-r from-sea to-brass transition-all duration-500"
        style={{ width: `${Math.min(100, Math.max(2, p * 100))}%` }} />
    </div>
  );
}

function ProfileTile({ p, current, recommended, selected, onSelect, admin }: {
  p: MemProfile; current: boolean; recommended: boolean; selected: boolean; onSelect: () => void; admin: boolean;
}) {
  const c = p.costs;
  return (
    <button disabled={!admin || current} onClick={onSelect}
      className={`flex flex-col rounded-xl border p-4 text-left transition-all duration-300 ${
        current ? "border-emerald-400/40 bg-emerald-400/5" : selected ? "border-sea bg-sea/10" : "border-line bg-panel2/30 hover:bg-panel2/60"
      } ${p.fits ? "" : "opacity-50"} disabled:cursor-default`}>
      <div className="flex items-baseline gap-2">
        <span className="text-[15px] font-semibold text-slate-100">{p.label}</span>
        {current && <span className="text-[10.5px] font-medium text-emerald-300">in use</span>}
        {recommended && !current && <span className="text-[10.5px] font-medium text-brass">recommended</span>}
      </div>
      <div className="mt-2 space-y-0.5 text-[12px] tabular-nums text-mut">
        <div>{c.vram_gb ? `${c.vram_gb} GB on the graphics card` : `${c.ram_gb} GB of memory`}</div>
        <div>answers in ~{c.query_ms_p50} ms{p.reranker ? (p.rerank_policy === "interactive" ? ", re-ranked" : ", re-ranked in background") : ""}</div>
        <div title="time to read 250 000 passages from scratch">full re-read ~{c.reindex_250k_h} h</div>
        <div>quality {(c.mrr_reranked ?? c.mrr).toFixed(2)}</div>
      </div>
      {!p.fits && <div className="mt-2 text-[11px] text-amber-300/80">no machine here can hold it</div>}
      {p.fits && p.fits_on && p.fits_on !== "this server" && <div className="mt-2 text-[11px] text-mut">served by {p.fits_on}</div>}
    </button>
  );
}

function JobPanel({ job, admin, busy, act, rollback }: {
  job: SwitchJob; admin: boolean; busy: boolean;
  act: (fn: () => Promise<unknown>, fail: string) => void; rollback: SwitchState["rollback"];
}) {
  const to = job.to_target?.profile;
  const cmp = job.comparison;
  if (job.status === "building" || job.status === "evaluating") {
    return (
      <div className="rounded-xl border border-line bg-panel2/60 p-4">
        <div className="text-[13.5px] font-medium text-slate-100">Changing to {to} — {Math.round(job.progress * 100)} %</div>
        <div className="mt-0.5 text-[12px] text-mut">{job.phase}</div>
        <div className="mt-3"><Bar p={job.progress} /></div>
        <div className="mt-2 text-[11.5px] text-mut">Your agents keep using the current memory meanwhile.</div>
      </div>
    );
  }
  if (job.status === "blocked") {
    const reg = cmp?.regressed;
    return (
      <div className="rounded-xl border border-amber-400/40 bg-amber-400/5 p-4 text-[12.5px] text-amber-100">
        <div className="text-[13.5px] font-medium">
          {reg ? `${to}: the memory would find fewer notes — nothing was changed` : `${to}: not enough questions to judge — nothing was changed`}
        </div>
        {cmp?.base && cmp?.new && (
          <div className="mt-1 tabular-nums">
            score {n3(cmp.base.mrr)} → {n3(cmp.new.mrr)} on {cmp.common} questions
            {cmp.worse ? ` · ${cmp.worse} answered worse` : ""}{cmp.gained ? ` · ${cmp.gained} better` : ""}
          </div>
        )}
        {!reg && <div className="mt-1 text-mut">Questions are collected from your sessions over time; you can also write some in CortHeXis → bench.</div>}
        {(cmp?.worse_examples || []).slice(0, 3).map((e) => (
          <div key={e.id} className="mt-1 text-[11.5px] text-mut">“{e.question}” — {e.expected.join(", ")}: #{e.rank_before ?? "–"} → {e.rank_after ? `#${e.rank_after}` : "not found"}</div>
        ))}
        {admin && (
          <div className="mt-3 flex gap-2">
            <button disabled={busy} onClick={() => act(() => memorySwitchDecide(job.id, "cancel"), "could not discard")}
              className="rounded-lg bg-sea/80 px-4 py-1.5 text-[12.5px] font-medium text-white hover:bg-sea disabled:opacity-40">
              Keep the current memory
            </button>
            <button disabled={busy} onClick={() => confirm(`Switch to ${to} anyway? You can undo it for 7 days.`) && act(() => memorySwitchDecide(job.id, "approve"), "could not switch")}
              className="rounded-lg border border-line px-4 py-1.5 text-[12.5px] text-slate-300 hover:bg-panel2 disabled:opacity-40">
              Switch anyway
            </button>
          </div>
        )}
      </div>
    );
  }
  if (job.status === "failed") {
    return (
      <div className="rounded-xl border border-red-400/30 bg-red-400/5 p-4 text-[12.5px] text-red-200">
        <div className="font-medium">The change to {to} did not go through — nothing was changed</div>
        <div className="mt-1 break-words text-red-200/80">{job.detail}</div>
      </div>
    );
  }
  if (job.status === "switched" && rollback && rollback.switch === job.id) {
    const d = cmp?.delta?.mrr;
    return (
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-emerald-400/25 bg-emerald-400/5 p-4 text-[12.5px] text-emerald-100">
        <div className="min-w-0 flex-1">
          <div className="font-medium">Now on {to} — can be undone until {when(rollback.until)}{job.decided_by && job.decided_by !== "bench" ? ` (approved by ${job.decided_by})` : ""}</div>
          {cmp?.base && cmp?.new && (
            <div className="mt-0.5 tabular-nums text-emerald-100/80">
              score {n3(cmp.base.mrr)} → {n3(cmp.new.mrr)}{d != null ? ` (${d >= 0 ? "+" : ""}${d.toFixed(3)})` : ""} on {cmp.common} questions
            </div>
          )}
        </div>
        {admin && (
          <button disabled={busy} onClick={() => confirm("Go back to the previous memory setup?") && act(memoryRollback, "rollback failed")}
            className="rounded-lg border border-line px-4 py-1.5 text-[12.5px] text-slate-300 hover:bg-panel2 disabled:opacity-40">
            Undo
          </button>
        )}
      </div>
    );
  }
  return null;
}

export default function MemoryCard({ admin }: { admin: boolean }) {
  const [mv, setMv] = useState<MemoryView | null>(null);
  const [sw, setSw] = useState<SwitchState | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [adv, setAdv] = useState(false);
  const [model, setModel] = useState("");
  const [urls, setUrls] = useState("");
  const [rebuild, setRebuild] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  const load = useCallback(() => {
    memoryView().then(setMv).catch(() => {});
    memorySwitchState().then(setSw).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);
  const running = sw?.job && (sw.job.status === "building" || sw.job.status === "evaluating");
  useEffect(() => {
    if (!running) return;
    const iv = setInterval(load, 1500);
    return () => clearInterval(iv);
  }, [running, load]);

  const act = async (fn: () => Promise<unknown>, fail: string) => {
    setBusy(true); setErr("");
    try { await fn(); load(); } catch (e) { setErr((e as Error).message || fail); } finally { setBusy(false); }
  };
  if (!mv) return null;

  const current = sw?.available ? sw.current?.profile : mv.current;
  const legacy = current === "remote" || current === "legacy";
  const host = (u: string) => u.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const recOn = mv.recommended_on && mv.recommended_on !== "this server" ? ` on ${mv.recommended_on}` : "";
  const selP = mv.profiles.find((p) => p.id === sel);
  const lic = mv.models.licence;
  const engine = mv.engine;
  const start = () => act(async () => {
    await memorySwitch({ profile: sel!, model: model || null, rebuild,
      urls: urls.split(/[\s,]+/).map((u) => u.trim()).filter(Boolean) });
    setSel(null); setAdv(false); setConfirmed(false);
  }, "could not start the change");

  return (
    <section className="space-y-4">
      <div className="flex items-baseline gap-2.5">
        <span className="text-[17px] font-semibold tracking-tight text-slate-100">Memory</span>
        <span className="text-[12.5px] text-mut">how your agents find information in your notes</span>
      </div>

      <div className="rounded-2xl border border-line bg-panel2/40 p-5">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[13px] text-slate-300">
          <span>In use: <b className="text-slate-100">{legacy ? "2.x model (before profiles)" : mv.profiles.find((p) => p.id === current)?.label ?? current ?? "—"}</b></span>
          {current === mv.recommended ? (
            <span className="text-mut">— the recommended one{recOn} ({mv.recommended_reason ?? mv.local.reason})</span>
          ) : (
            <span>Recommended: <b className="text-brass">{mv.profiles.find((p) => p.id === mv.recommended)?.label ?? mv.recommended}</b>
              <span className="text-mut">{recOn} ({mv.recommended_reason ?? mv.local.reason})</span></span>
          )}
        </div>
        {engine && !engine.error && (
          <div className="mt-1 text-[12px] leading-relaxed text-mut">
            Model: {engine.label ?? engine.model ?? "—"}{engine.licence ? ` · licence ${engine.licence === "gemma" ? "Gemma Terms" : engine.licence}` : ""}
            {engine.urls?.length ? ` · served by ${engine.urls.map(host).join(", then ")}` : ""}
            {engine.rerank_url ? ` · re-ranker ${host(engine.rerank_url)}` : ""}
            {sw?.active_generation ? ` · index #${sw.active_generation}` : ""}
          </div>
        )}
        {engine?.error && <div className="mt-1 text-[12px] text-red-300">{engine.error}</div>}

        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
          {mv.profiles.map((p) => (
            <ProfileTile key={p.id} p={p} current={p.id === current} recommended={p.id === mv.recommended}
              selected={p.id === sel} admin={admin && !!sw?.available && !running} onSelect={() => setSel(p.id === sel ? null : p.id)} />
          ))}
        </div>

        {sel && (
          <div className="mt-4 rounded-xl border border-sea/40 bg-sea/5 p-4 text-[12.5px] text-slate-300">
            <div className="text-[13.5px] font-medium text-slate-100">Change to {mv.profiles.find((p) => p.id === sel)?.label}?</div>
            <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-mut">
              <li>Prepared in the background; your agents keep the current memory meanwhile.</li>
              <li>Applied only if it finds your notes at least as well, measured on your own questions.</li>
              <li>You can undo it for {sw?.retention_days ?? 7} days.</li>
            </ul>
            <button onClick={() => setAdv((a) => !a)} className="mt-2 text-[12px] text-sea/80 hover:text-sea">{adv ? "hide" : "advanced"}</button>
            {adv && (
              <div className="mt-2 space-y-2">
                <label className="block text-[12px]">Model
                  <select value={model} onChange={(e) => setModel(e.target.value)}
                    className="ml-2 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-200">
                    {MODELS.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
                  </select>
                </label>
                <label className="block text-[12px]">Memory servers (optional, in order)
                  <input value={urls} onChange={(e) => setUrls(e.target.value)} placeholder="http://<machine>:8080"
                    className="mt-1 w-full rounded border border-line bg-[#0b0f16] px-2 py-1 font-mono text-[12px] text-slate-200" />
                </label>
                <label className="flex items-center gap-2 text-[12px]">
                  <input type="checkbox" checked={rebuild} onChange={(e) => setRebuild(e.target.checked)} />
                  re-read every note even if the model stays the same
                </label>
              </div>
            )}
            {/* re-reading every note loads the memory servers (on a GPU shared with production, too):
                never started without an explicit tick */}
            <label className="mt-3 flex items-start gap-2 rounded-lg border border-amber-400/30 bg-amber-400/5 p-2.5 text-[12px] text-amber-100">
              <input type="checkbox" className="mt-0.5" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
              <span>
                I understand that every note is re-read with the {selP?.label ?? sel} model
                {selP?.costs.reindex_250k_h != null ? ` (about ${selP.costs.reindex_250k_h} h per 250 000 passages)` : ""},
                on the memory servers{selP?.fits_on && selP.fits_on !== "this server" ? ` of ${selP.fits_on}` : ""}, while the current memory keeps serving.
              </span>
            </label>
            <div className="mt-3 flex gap-2">
              <button disabled={busy || !confirmed} onClick={start}
                className="rounded-lg bg-sea/80 px-4 py-1.5 text-[13px] font-medium text-white hover:bg-sea disabled:opacity-40">
                Prepare the change
              </button>
              <button onClick={() => { setSel(null); setConfirmed(false); }} className="text-[12.5px] text-mut hover:text-slate-300">cancel</button>
            </div>
          </div>
        )}

        {sw?.available === false && (
          <div className="mt-4 rounded-lg border border-line bg-ink/30 px-3 py-2.5 text-[12px] leading-relaxed text-mut">
            <span className="text-slate-300">The profile is set on the server for now.</span> This instance keeps its memory index in
            SQLite (2.x); switching from here, checked against your own questions and reversible, needs the 3.0 memory store
            (Postgres + pgvector). Meanwhile: <code className="font-mono text-slate-300">CORTHEXIS_MEMORY_PROFILE</code> or{" "}
            <code className="font-mono text-slate-300">./scripts/memory-setup.sh</code>.
          </div>
        )}
        {err && <div className="mt-3 rounded-lg border border-red-400/30 bg-red-400/5 px-3 py-2 text-[12.5px] text-red-300">{err}</div>}
        {sw?.job && <div className="mt-4"><JobPanel job={sw.job} admin={admin} busy={busy} act={act} rollback={sw.rollback} /></div>}
        {!running && sw?.rollback && sw.job?.id !== sw.rollback.switch && admin && (
          <div className="mt-3 text-[12px] text-mut">
            The previous setup ({sw.rollback.to_target?.profile}) can be restored until {when(sw.rollback.until)}.{" "}
            <button disabled={busy} onClick={() => act(memoryRollback, "rollback failed")} className="text-sea/80 hover:text-sea">Restore it</button>
          </div>
        )}
      </div>

      <div className="rounded-2xl border border-line bg-panel2/40 p-5 text-[12.5px] text-slate-300">
        <div className="text-[13.5px] font-medium text-slate-100">Recommended model: EmbeddingGemma (Google)</div>
        {lic.decision && lic.current ? (
          <div className="mt-1 text-mut">
            Gemma Terms of Use <b className={lic.decision === "accepted" ? "text-emerald-300" : "text-slate-300"}>{lic.decision}</b>
            {lic.by ? ` by ${lic.by}` : ""} on {when(lic.at)}{lic.via ? ` (${lic.via})` : ""}.
            {mv.models.installed["embeddinggemma-300m-q8"] ? " Model downloaded." : lic.decision === "accepted" ? " Download pending." : " The open (MIT) model is used."}
            {admin && (
              <button disabled={busy} onClick={() => act(async () => {
                const r = await memoryLicence(lic.decision === "accepted" ? "declined" : "accepted");
                setNote(r.downloading ? "Downloading the model (330 MB)…" : "");
              }, "could not record the decision")} className="ml-2 inline-flex min-h-6 items-center px-1 text-sea/80 hover:text-sea">
                {lic.decision === "accepted" ? "withdraw" : "accept instead"}
              </button>
            )}
          </div>
        ) : (
          <>
            <p className="mt-1 leading-relaxed text-mut">
              It lets your agents find the right information about one time in three more often than the basic model, and runs
              entirely on your machine: no data is sent to Google. It is free, commercial use included, but not open source: Google
              allows it under its <a className="text-sea/80 hover:text-sea" href={mv.models.terms_url} target="_blank" rel="noreferrer">Gemma Terms of Use</a>,
              which forbid some uses (<a className="text-sea/80 hover:text-sea" href={mv.models.policy_url} target="_blank" rel="noreferrer">use policy</a>).
              If you decline, a model under the MIT licence is used: everything works, recall is a little lower.
            </p>
            {admin && (
              <div className="mt-3 flex gap-2">
                <button disabled={busy} onClick={() => act(async () => {
                  const r = await memoryLicence("accepted");
                  setNote(r.downloading ? "Accepted — downloading the model (330 MB)…" : "Accepted.");
                }, "could not record the decision")}
                  className="rounded-lg bg-sea/80 px-4 py-1.5 text-[12.5px] font-medium text-white hover:bg-sea disabled:opacity-40">
                  I accept the Gemma terms
                </button>
                <button disabled={busy} onClick={() => act(async () => { await memoryLicence("declined"); setNote("Declined — the MIT model stays."); }, "could not record the decision")}
                  className="rounded-lg border border-line px-4 py-1.5 text-[12.5px] text-slate-300 hover:bg-panel2 disabled:opacity-40">
                  Use the MIT model
                </button>
              </div>
            )}
          </>
        )}
        {note && <div className="mt-2 text-[12px] text-emerald-300">{note}</div>}
        <div className="mt-2 text-[11px] text-mut">The decision is recorded with your name in the journal. Switching model afterwards goes through “Change” above (advanced → model).</div>
      </div>
    </section>
  );
}
