"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  benchAdd, benchDelete, benchGenerate, benchHarvest, benchOverview, benchQuestions, benchRun,
  benchSetStatus,
} from "@/lib/api";
import type { BenchMetrics, BenchOverview, BenchQuestion, BenchRun } from "@/lib/api";
import { useCan } from "@/lib/me";

// CortHeXis → Bench : « est-ce que la mémoire retrouve la bonne note, chez moi ? »

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)} %`);
const num = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));
const day = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";

const SOURCE: Record<string, { label: string; hint: string; cls: string }> = {
  transcript: { label: "from a session", hint: "a real question, followed by the note the agent opened", cls: "bg-sea/15 text-sea" },
  client: { label: "written", hint: "written by a person, with the note that answers it", cls: "bg-emerald-400/15 text-emerald-300" },
  generated: { label: "generated", hint: "written by the AI from the note itself — counts less, it flatters the engine", cls: "bg-panel2 text-mut" },
};

function Stat({ label, value, sub, hint }: { label: string; value: string; sub?: string; hint: string }) {
  return (
    <div className="rounded-xl border border-line bg-panel2/40 px-4 py-3" title={hint}>
      <div className="text-[11px] uppercase tracking-wide text-mut">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-mut">{sub}</div>}
    </div>
  );
}

/** MRR des derniers passages (du plus ancien au plus récent), une ligne simple. */
function Trend({ runs }: { runs: BenchRun[] }) {
  const pts = runs.filter((r) => r.status === "done" && r.metrics.overall?.mrr != null).slice(0, 20).reverse();
  if (pts.length < 2) return null;
  const W = 220, H = 44;
  const xs = (i: number) => (i / (pts.length - 1)) * (W - 8) + 4;
  const ys = (v: number) => H - 4 - v * (H - 8);
  const d = pts.map((r, i) => `${i ? "L" : "M"}${xs(i).toFixed(1)},${ys(r.metrics.overall!.mrr!).toFixed(1)}`).join(" ");
  return (
    <svg width={W} height={H} className="shrink-0" aria-label="score of the last runs">
      <path d={d} fill="none" stroke="#3b82f6" strokeWidth={1.6} />
      {pts.map((r, i) => (
        <circle key={r.id} cx={xs(i)} cy={ys(r.metrics.overall!.mrr!)} r={r.regression ? 3.2 : 2}
          fill={r.regression ? "#f87171" : "#D4A017"}>
          <title>{`${day(r.started_at)} — score ${num(r.metrics.overall!.mrr, 3)}${r.regression ? " (drop)" : ""}`}</title>
        </circle>
      ))}
    </svg>
  );
}

function Line({ m }: { m?: BenchMetrics }) {
  if (!m || !m.n) return <span className="text-mut">—</span>;
  return (
    <span className="tabular-nums">
      {pct(m.h1)} first · {pct(m.h5)} top 5 · score {num(m.mrr, 3)} <span className="text-mut">({m.n})</span>
    </span>
  );
}

export default function MemoryBench({ noteNames, onPickNote }: { noteNames: string[]; onPickNote?: (n: string) => void }) {
  const canEdit = useCan("dev");
  const [ov, setOv] = useState<BenchOverview | null>(null);
  const [qs, setQs] = useState<BenchQuestion[]>([]);
  const [filter, setFilter] = useState<string>("all");
  const [q, setQ] = useState("");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    benchOverview().then((o) => {
      setOv(o);
      if (o.available) benchQuestions().then(setQs).catch(() => {});
    }).catch(() => setOv({ available: false, reason: "The bench could not be loaded." }));
  }, []);
  useEffect(() => { load(); }, [load]);
  // un passage en cours : on suit jusqu'au résultat
  const running = (ov?.busy || []).length > 0 || (ov?.runs || []).some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const iv = setInterval(load, 1500);
    return () => clearInterval(iv);
  }, [running, load]);

  const act = async (fn: () => Promise<unknown>, okText: string) => {
    setBusy(true); setMsg(null);
    try { await fn(); setMsg({ ok: true, text: okText }); load(); }
    catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
    finally { setBusy(false); }
  };
  const add = () => act(async () => {
    await benchAdd(q.trim(), note.split(",").map((s) => s.trim()).filter(Boolean));
    setQ(""); setNote("");
  }, "Question added — it counts from the next run.");

  const shown = useMemo(() => qs.filter((x) => filter === "all" || x.source === filter || (filter === "missed" && x.measured && (x.rank == null || x.rank > 5))), [qs, filter]);

  if (!ov) return <div className="p-6 text-[13px] text-mut">…</div>;
  if (!ov.available) {
    return (
      <div className="mx-auto mt-10 max-w-lg rounded-2xl border border-line bg-panel2/40 p-6 text-[13px] leading-relaxed text-mut">
        <div className="mb-1 text-[15px] font-semibold text-slate-100">Recall bench</div>
        {ov.reason}
      </div>
    );
  }
  const last = ov.last;
  const o = last?.metrics.overall;
  const counts = ov.questions!;
  const findings = ov.findings || [];

  return (
    <div className="mx-auto w-full max-w-4xl space-y-5 pb-10">
      <div className="flex flex-wrap items-end gap-4">
        <div className="min-w-0 flex-1">
          <div className="text-[17px] font-semibold text-slate-100">Recall bench</div>
          <div className="text-[12.5px] text-mut">
            Does the memory bring up the right note for the questions your team really asks? Measured on your own notes,
            every night{last ? `, last run ${day(last.finished_at || last.started_at)}` : ""}.
          </div>
        </div>
        <Trend runs={ov.runs || []} />
      </div>

      {findings.map((f) => (
        <div key={f.check} className={`rounded-xl border px-4 py-3 text-[12.5px] ${f.severity === "info" ? "border-line bg-panel2/40 text-mut" : f.severity === "critical" ? "border-red-400/40 bg-red-400/5 text-red-200" : "border-amber-400/40 bg-amber-400/5 text-amber-100"}`}>
          <div className="font-medium">{f.title}</div>
          <div className="mt-0.5">{f.detail}</div>
          <div className="mt-1 text-mut">{f.remedy}</div>
          {f.notes.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {f.notes.map((n) => (
                <button key={n} onClick={() => onPickNote?.(n)} className="rounded bg-brass/15 px-1.5 py-0.5 text-[10.5px] text-brass hover:bg-brass/25">{n}</button>
              ))}
            </div>
          )}
        </div>
      ))}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Right note first" value={pct(o?.h1)} hint="share of the questions whose expected note comes out first (hit@1)" />
        <Stat label="In the first five" value={pct(o?.h5)} hint="share of the questions whose expected note is among the first five results (hit@5)" />
        <Stat label="Score" value={num(o?.mrr, 3)} sub="1.0 = always first" hint="mean reciprocal rank (MRR): 1 when the note is first, 0.5 when second, 0.33 when third…" />
        <Stat label="Questions" value={String(counts.total_active)} sub={last?.metrics.search_ms_p50 != null ? `search ${Math.round(last.metrics.search_ms_p50)} ms` : undefined}
          hint="active questions; the search time is the median measured during the last run" />
      </div>

      {last && last.metrics.by_source && (
        <div className="space-y-1 rounded-xl border border-line bg-panel2/30 px-4 py-3 text-[12.5px] text-slate-300">
          {Object.entries(last.metrics.by_source).map(([s, m]) => (
            <div key={s} className="flex items-center gap-3">
              <span className={`w-28 shrink-0 rounded px-1.5 py-0.5 text-center text-[10.5px] ${SOURCE[s]?.cls}`} title={SOURCE[s]?.hint}>{SOURCE[s]?.label ?? s}</span>
              <Line m={m} />
            </div>
          ))}
          {(last.metrics.stale ?? 0) > 0 && (
            <div className="text-[11.5px] text-mut">{last.metrics.stale} question(s) skipped: their note no longer exists.</div>
          )}
        </div>
      )}

      {canEdit && (
        <div className="flex flex-wrap items-center gap-2">
          <button disabled={busy || running} onClick={() => act(benchRun, "Bench started.")}
            className="rounded-lg bg-sea/80 px-4 py-1.5 text-[13px] font-medium text-white hover:bg-sea disabled:opacity-40">
            {running ? "Measuring…" : "Run the bench now"}
          </button>
          <button disabled={busy} onClick={() => act(async () => {
            const r = await benchHarvest();
            setMsg({ ok: true, text: `${r.new} new question(s) found in ${r.files} session(s).` });
          }, "")} title="look in the sessions for questions followed by the note the agent opened"
            className="rounded-lg border border-line px-4 py-1.5 text-[13px] text-slate-300 hover:bg-panel2 disabled:opacity-40">
            Collect from sessions
          </button>
          <button disabled={busy || !ov.llm || running} onClick={() => act(benchGenerate, "The AI is writing questions from your notes…")}
            title={ov.llm ? "the instance's AI writes questions from note summaries (they count less)" : "no AI model configured on this instance"}
            className="rounded-lg border border-line px-4 py-1.5 text-[13px] text-slate-300 hover:bg-panel2 disabled:opacity-40">
            Write questions with AI
          </button>
          {msg && msg.text && <span className={`text-[12px] ${msg.ok ? "text-emerald-300" : "text-red-300"}`}>{msg.text}</span>}
        </div>
      )}

      {canEdit && (
        <div className="rounded-xl border border-line bg-panel2/30 p-4">
          <div className="mb-2 text-[13px] font-medium text-slate-200">Add a question</div>
          <div className="flex flex-wrap gap-2">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="a question someone could ask, in their words"
              className="min-w-[16rem] flex-[2] rounded-lg border border-line bg-[#0b0f16] px-3 py-1.5 text-[12.5px] text-slate-100 outline-none focus:border-sea/50" />
            <input value={note} onChange={(e) => setNote(e.target.value)} list="bench-notes" placeholder="the note that answers it"
              className="min-w-[12rem] flex-1 rounded-lg border border-line bg-[#0b0f16] px-3 py-1.5 text-[12.5px] text-slate-100 outline-none focus:border-sea/50" />
            <datalist id="bench-notes">{noteNames.map((n) => <option key={n} value={n} />)}</datalist>
            <button disabled={busy || q.trim().length < 8 || !note.trim()} onClick={add}
              className="rounded-lg bg-emerald-500/80 px-4 py-1.5 text-[13px] font-medium text-white hover:bg-emerald-500 disabled:opacity-40">
              Add
            </button>
          </div>
          <div className="mt-1.5 text-[11px] text-mut">Several notes can answer: separate them with commas.</div>
        </div>
      )}

      <div>
        <div className="mb-2 flex flex-wrap items-center gap-1.5 text-[11.5px]">
          {[["all", `All (${qs.length})`], ["transcript", `From sessions (${counts.transcript.active + counts.transcript.disabled})`],
            ["client", `Written (${counts.client.active + counts.client.disabled})`], ["generated", `Generated (${counts.generated.active + counts.generated.disabled})`],
            ["missed", "Not found"]].map(([k, l]) => (
            <button key={k} onClick={() => setFilter(k)}
              className={`rounded-full border px-2.5 py-0.5 ${filter === k ? "border-sea bg-sea/15 text-sea" : "border-line text-mut hover:text-slate-200"}`}>{l}</button>
          ))}
        </div>
        <div className="divide-y divide-line/50 overflow-hidden rounded-xl border border-line">
          {shown.length === 0 && <div className="px-4 py-6 text-center text-[12.5px] text-mut">No question here yet.</div>}
          {shown.map((x) => (
            <div key={x.id} className={`flex items-start gap-3 px-4 py-2 text-[12.5px] ${x.status === "disabled" ? "opacity-45" : ""}`}>
              <span className={`mt-0.5 w-24 shrink-0 rounded px-1.5 py-0.5 text-center text-[10px] ${SOURCE[x.source].cls}`} title={SOURCE[x.source].hint}>
                {SOURCE[x.source].label}{x.seen > 1 ? ` ×${x.seen}` : ""}
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-slate-200">{x.question}</div>
                <div className="mt-0.5 flex flex-wrap gap-1">
                  {x.expected.map((n) => (
                    <button key={n} onClick={() => onPickNote?.(n)} className="rounded bg-brass/10 px-1.5 text-[10.5px] text-brass hover:bg-brass/20">{n}</button>
                  ))}
                </div>
              </div>
              <span className={`mt-0.5 w-20 shrink-0 text-right text-[11.5px] tabular-nums ${!x.measured ? "text-mut" : x.rank === 1 ? "text-emerald-300" : x.rank && x.rank <= 5 ? "text-slate-300" : "text-red-300"}`}
                title="where the expected note came out in the last run">
                {!x.measured ? "not measured" : x.rank ? `#${x.rank}` : "not found"}
              </span>
              {canEdit && (
                <span className="flex shrink-0 gap-2 text-[11px]">
                  <button onClick={() => act(() => benchSetStatus(x.id, x.status === "active" ? "disabled" : "active"), "")}
                    className="text-mut hover:text-slate-200">{x.status === "active" ? "pause" : "resume"}</button>
                  {x.source !== "transcript" && (
                    <button onClick={() => confirm("Delete this question?") && act(() => benchDelete(x.id), "")}
                      className="text-mut hover:text-red-300">delete</button>
                  )}
                </span>
              )}
            </div>
          ))}
        </div>
      </div>

      {(ov.runs || []).length > 0 && (
        <details className="rounded-xl border border-line bg-panel2/20 px-4 py-2 text-[12px] text-slate-300">
          <summary className="cursor-pointer text-mut">History of the runs</summary>
          <table className="mt-2 w-full tabular-nums">
            <thead className="text-left text-[11px] text-mut">
              <tr><th className="py-1">when</th><th>index</th><th>profile</th><th>why</th><th>first</th><th>top 5</th><th>score</th><th /></tr>
            </thead>
            <tbody>
              {(ov.runs || []).map((r) => (
                <tr key={r.id} className="border-t border-line/40">
                  <td className="py-1">{day(r.started_at)}</td>
                  <td>#{r.generation_id}</td>
                  <td>{r.profile || "—"}</td>
                  <td className="text-mut">{r.trigger === "switch" ? "profile change" : r.trigger}</td>
                  <td>{pct(r.metrics.overall?.h1)}</td>
                  <td>{pct(r.metrics.overall?.h5)}</td>
                  <td>{num(r.metrics.overall?.mrr, 3)}</td>
                  <td className={r.status === "failed" ? "text-red-300" : r.regression ? "text-red-300" : "text-mut"}>
                    {r.status === "failed" ? `failed: ${r.error ?? ""}` : r.status === "running" ? "running…" : r.regression ? "drop" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}
