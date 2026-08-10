"use client";
import { useEffect, useState } from "react";
import {
  magnitudeCmd, magnitudeConnect, magnitudePair, magnitudeState, magnitudeUnpair,
} from "@/lib/api";
import type {
  MagnitudeBench, MagnitudeModel, MagnitudeProfile, MagnitudeServing, MagnitudeState,
  MagnitudeStatus,
} from "@/lib/api";
import { useCan } from "@/lib/me";

// ————— formatting helpers —————

const gb = (v: number | null | undefined) =>
  v == null ? "—" : v >= 10 ? `${Math.round(v)}` : v.toFixed(1);

function upFor(since: number) {
  const s = Math.max(0, Date.now() / 1000 - since);
  if (s < 60) return `${Math.floor(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} min`;
}

// ————— small building blocks —————

function Progress({ pct }: { pct: number | null | undefined }) {
  // thin gradient bar; indeterminate (soft pulse) when pct is unknown
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-line/60">
      <div
        className={`h-full rounded-full bg-gradient-to-r from-sea to-brass transition-all duration-500 ${
          pct == null ? "w-full animate-pulse opacity-50" : ""
        }`}
        style={pct != null ? { width: `${Math.min(100, Math.max(2, pct))}%` } : undefined}
      />
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  const copy = () =>
    navigator.clipboard.writeText(text).then(() => {
      setDone(true);
      setTimeout(() => setDone(false), 1600);
    }).catch(() => {});
  return (
    <button
      onClick={copy}
      className="shrink-0 rounded-lg border border-line px-3 py-1.5 text-[12.5px] text-slate-300 transition-all duration-300 hover:bg-panel2"
    >
      {done ? "Copied ✓" : "Copy"}
    </button>
  );
}

function LiveDot() {
  return (
    <span className="relative flex h-3 w-3 shrink-0">
      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400/50" />
      <span className="relative inline-flex h-3 w-3 rounded-full bg-emerald-400" />
    </span>
  );
}

// ————— state screens —————

function Hero({ admin, busy, onPair }: { admin: boolean; busy: boolean; onPair: () => void }) {
  return (
    <div className="flex flex-col items-center gap-6 pt-16 text-center transition-all duration-500">
      <h1 className="text-4xl font-semibold tracking-tight text-slate-100 md:text-5xl">Magnitude</h1>
      <p className="max-w-md text-[15px] leading-relaxed text-mut">
        Measure what your machine can really run. Locally. Privately.
      </p>
      {admin ? (
        <button
          onClick={onPair}
          disabled={busy}
          className="mt-4 rounded-xl bg-sea/80 px-8 py-3 text-[15px] font-medium text-white transition-all duration-300 hover:bg-sea active:scale-[0.98] disabled:opacity-40"
        >
          Pair this machine
        </button>
      ) : (
        <p className="mt-4 text-[13px] text-mut">An admin can pair a machine to get started.</p>
      )}
    </div>
  );
}

function PairingCard({ command }: { command: string }) {
  return (
    <div className="flex flex-col items-center gap-6 pt-16 transition-all duration-500">
      <h1 className="text-4xl font-semibold tracking-tight text-slate-100 md:text-5xl">Magnitude</h1>
      <p className="max-w-md text-center text-[15px] leading-relaxed text-mut">
        Run this on the machine you want to measure. The token is shown only once.
      </p>
      <div className="w-full rounded-2xl border border-line bg-panel2/40 p-6 transition-all duration-500">
        <div className="flex items-start gap-3">
          <code className="min-w-0 flex-1 select-all break-all rounded-lg border border-line bg-ink px-4 py-3 font-mono text-[12.5px] leading-relaxed text-slate-200">
            {command}
          </code>
          <CopyButton text={command} />
        </div>
        <div className="mt-5 flex items-center gap-2.5 text-[13px] text-mut">
          <span className="h-2 w-2 animate-pulse rounded-full bg-sea/80" />
          <span className="animate-pulse">waiting for the agent…</span>
        </div>
      </div>
    </div>
  );
}

function OpBanner({ status, label }: { status: MagnitudeStatus; label: string }) {
  const text =
    status.phase === "downloading"
      ? `Downloading ${label}${status.pct != null ? ` — ${Math.round(status.pct)} %` : "…"}`
      : status.phase === "benching"
      ? `Benchmarking ${label}…`
      : status.phase === "starting"
      ? `Starting ${label}…`
      : status.detail || "Something went wrong on the agent";
  const isError = status.phase === "error";
  return (
    <div
      className={`sticky top-0 z-10 rounded-2xl border p-5 backdrop-blur transition-all duration-500 ${
        isError ? "border-red-400/30 bg-panel2/80" : "border-line bg-panel2/80"
      }`}
    >
      <div className={`text-[14px] font-medium ${isError ? "text-red-300" : "text-slate-100"}`}>{text}</div>
      {!isError && (
        <div className="mt-3">
          <Progress pct={status.pct} />
        </div>
      )}
      {!isError && status.detail && <div className="mt-2 text-[12px] text-mut">{status.detail}</div>}
    </div>
  );
}

function HardwareCard({ profile, online, lastSeen }: { profile: MagnitudeProfile; online: boolean; lastSeen: number | null }) {
  const g = profile.gpu;
  const cls = profile.class;
  return (
    <div className={`rounded-2xl border border-line bg-panel2/40 p-6 transition-all duration-500 ${online ? "" : "opacity-60"}`}>
      <div className="flex items-center gap-5">
        <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-full border border-brass/40 bg-brass/10">
          <span className={`font-semibold text-brass ${cls.length > 2 ? "text-[13px]" : "text-xl"}`}>
            {cls === "unsupported" ? "—" : cls}
          </span>
        </div>
        <div className="min-w-0">
          <div className="truncate text-2xl font-semibold tracking-tight text-slate-100 md:text-3xl">
            {g ? g.name : profile.cpu}
          </div>
          <div className="mt-1 text-[13px] text-mut">
            {g ? `${g.vendor} · ${g.backend}` : "CPU inference"}
            {!online && <span className="ml-2 text-amber-300/80">· agent offline{lastSeen ? ` — last seen ${upFor(lastSeen)} ago` : ""}</span>}
          </div>
        </div>
      </div>
      {/* vulkaninfo/lspci detection has no VRAM figures — hide the gauge then */}
      {g && g.vram_total_gb != null && (
        <div className="mt-6">
          <div className="mb-1.5 flex items-baseline justify-between text-[12.5px]">
            <span className="text-mut">VRAM</span>
            <span className="tabular-nums text-slate-300">
              {g.vram_free_gb != null ? `${gb(g.vram_free_gb)} GB free of ` : ""}{gb(g.vram_total_gb)} GB
            </span>
          </div>
          <Progress
            pct={g.vram_free_gb != null && g.vram_total_gb > 0
              ? ((g.vram_total_gb - g.vram_free_gb) / g.vram_total_gb) * 100
              : 0}
          />
        </div>
      )}
      <div className="mt-5 text-[13px] text-mut">
        {profile.cpu} · {profile.cores} cores · {gb(profile.ram_gb)} GB RAM
        {g?.driver ? ` · driver ${g.driver}` : ""}
      </div>
    </div>
  );
}

function ServingCard({
  serving, label, connected, admin, online, busy, onConnect, onStop,
}: {
  serving: MagnitudeServing; label: string; connected: boolean; admin: boolean;
  online: boolean; busy: boolean; onConnect: () => void; onStop: () => void;
}) {
  return (
    <div className="rounded-2xl border border-emerald-400/25 bg-panel2/40 p-6 transition-all duration-500">
      <div className="flex items-center gap-3.5">
        <LiveDot />
        <div className="min-w-0">
          <div className="truncate text-xl font-semibold tracking-tight text-slate-100">
            {label} is live on this machine
          </div>
          <div className="mt-0.5 text-[13px] tabular-nums text-mut">up {upFor(serving.since)}</div>
        </div>
      </div>
      {admin && (
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <button
            onClick={onConnect}
            disabled={connected || busy || !online}
            className={`rounded-xl px-6 py-2.5 text-[14px] font-medium transition-all duration-300 active:scale-[0.98] ${
              connected
                ? "cursor-default border border-emerald-400/30 bg-emerald-400/10 text-emerald-300"
                : "bg-sea/80 text-white hover:bg-sea disabled:opacity-40"
            }`}
          >
            {connected ? "Connected ✓" : "Connect to SOKKAN"}
          </button>
          <button
            onClick={onStop}
            disabled={busy || !online}
            className="rounded-xl border border-line px-5 py-2.5 text-[13.5px] text-slate-300 transition-all duration-300 hover:bg-panel2 disabled:opacity-40"
          >
            Stop
          </button>
        </div>
      )}
      <p className="mt-4 text-[13px] leading-relaxed text-mut">
        Every new session will run on your own hardware. Zero cloud. Zero cost per token.
      </p>
    </div>
  );
}

function ModelCard({
  m, bench, live, admin, canAct, onBench, onRun,
}: {
  m: MagnitudeModel; bench: MagnitudeBench | undefined; live: boolean; admin: boolean;
  canAct: boolean; onBench: () => void; onRun: () => void;
}) {
  const fitBadge =
    m.fit === "comfortable" ? (
      <span className="text-[12px] font-medium text-emerald-300">Fits comfortably</span>
    ) : m.fit === "tight" ? (
      <span className="text-[12px] font-medium text-amber-300">Tight fit</span>
    ) : m.fit === "no" ? (
      <span className="text-[12px] text-mut">Not enough memory</span>
    ) : null;
  return (
    <div
      className={`flex flex-col rounded-2xl border border-line bg-panel2/40 p-6 transition-all duration-500 ${
        m.fit === "no" ? "opacity-40" : m.fit === "comfortable" ? "ring-1 ring-emerald-400/20" : ""
      }`}
    >
      <div className="flex items-baseline gap-2.5">
        <span className="text-[17px] font-semibold tracking-tight text-slate-100">{m.label}</span>
        {live && (
          <span className="flex items-center gap-1.5 text-[11px] font-medium text-emerald-300">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> live
          </span>
        )}
        <span className="ml-auto text-[12px] tabular-nums text-mut">{m.params}</span>
        {m.moe && (
          <span className="rounded bg-brass/15 px-1.5 py-0.5 text-[10px] font-medium text-brass">MoE</span>
        )}
      </div>
      <div className="mt-1 text-[13px] text-mut">{m.note}</div>
      <div className="mt-2.5 flex items-baseline gap-3">
        <span className="text-[12px] tabular-nums text-mut">{gb(m.weights_gb)} GB weights</span>
        {fitBadge}
      </div>
      {bench && bench.gen_tok_s != null && (
        <div className="mt-4 border-t border-line/60 pt-4">
          <div className="flex items-baseline gap-1.5">
            <span className="text-2xl font-semibold tabular-nums text-slate-100">
              {bench.gen_tok_s.toFixed(bench.gen_tok_s >= 100 ? 0 : 1)}
            </span>
            <span className="text-[12px] text-mut">tok/s</span>
          </div>
          <div className="mt-1 text-[12px] tabular-nums text-mut">
            {bench.prefill_tok_s != null && `${Math.round(bench.prefill_tok_s)} tok/s prefill`}
            {bench.power_avg_w != null && ` · ${Math.round(bench.power_avg_w)} W`}
            {bench.eur_per_mtok_gen != null && ` · €${bench.eur_per_mtok_gen.toFixed(2)}/Mtok`}
          </div>
        </div>
      )}
      {admin && m.fits && (
        <div className="mt-5 flex gap-2.5">
          <button
            onClick={onBench}
            disabled={!canAct}
            className="rounded-lg border border-line px-4 py-1.5 text-[13px] text-slate-300 transition-all duration-300 hover:bg-panel2 disabled:opacity-40"
          >
            Benchmark
          </button>
          <button
            onClick={onRun}
            disabled={!canAct || live}
            className="rounded-lg bg-sea/80 px-4 py-1.5 text-[13px] font-medium text-white transition-all duration-300 hover:bg-sea disabled:opacity-40"
          >
            Run
          </button>
        </div>
      )}
    </div>
  );
}

// ————— the tab —————

export default function Magnitude() {
  const admin = useCan("admin");
  const [st, setSt] = useState<MagnitudeState | null>(null);
  const [pairCmd, setPairCmd] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    const load = () => magnitudeState().then((d) => alive && setSt(d)).catch(() => {});
    load();
    const iv = setInterval(load, 2000);
    return () => { alive = false; clearInterval(iv); };
  }, []);

  const act = async (fn: () => Promise<unknown>, failMsg: string) => {
    setBusy(true);
    setErr("");
    try { await fn(); } catch { setErr(failMsg); } finally { setBusy(false); }
  };

  const pair = () =>
    act(async () => {
      const r = await magnitudePair();
      setPairCmd(r.command);
    }, "pairing failed");

  const unpair = () => {
    if (!confirm("Unpair this machine? The agent will stop syncing and all bench data is cleared.")) return;
    act(async () => {
      await magnitudeUnpair();
      setPairCmd(null);
      setSt(await magnitudeState());
    }, "unpair failed");
  };

  if (!st) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-[13px] text-mut">…</div>
    );
  }

  const status = st.status;
  const labelOf = (id: string | null | undefined) =>
    (id && st.catalog.find((m) => m.id === id)?.label) || id || "model";
  // agent busy on a long operation → hold new commands
  const opRunning = !!status && ["benching", "downloading", "starting"].includes(status.phase);
  const canAct = st.online && !busy && !opRunning;

  let body: React.ReactNode;
  if (pairCmd && !st.online) {
    body = <PairingCard command={pairCmd} />;
  } else if (!st.paired) {
    body = <Hero admin={admin} busy={busy} onPair={pair} />;
  } else {
    body = (
      <div className="space-y-6">
        <div className="flex items-baseline justify-between pb-2">
          <h1 className="text-4xl font-semibold tracking-tight text-slate-100 md:text-5xl">Magnitude</h1>
          {admin && (
            <button
              onClick={unpair}
              className="text-[12px] text-mut transition-colors hover:text-slate-300"
            >
              Unpair
            </button>
          )}
        </div>

        {status && status.phase !== "idle" && status.phase !== "serving" && (
          <OpBanner status={status} label={labelOf(status.model)} />
        )}

        {st.serving && (
          <ServingCard
            serving={st.serving}
            label={labelOf(st.serving.model)}
            connected={st.connected}
            admin={admin}
            online={st.online}
            busy={busy}
            onConnect={() => act(() => magnitudeConnect(), "connect failed — model not serving or LLM config is operator-managed")}
            onStop={() => act(() => magnitudeCmd("stop"), "stop failed")}
          />
        )}

        {st.profile ? (
          <HardwareCard profile={st.profile} online={st.online} lastSeen={st.last_seen} />
        ) : (
          <div className="rounded-2xl border border-line bg-panel2/40 p-6 text-[13px] text-mut transition-all duration-500">
            <span className="animate-pulse">
              {st.online ? "profiling hardware…" : "waiting for the agent…"}
            </span>
          </div>
        )}

        <div>
          <div className="mb-3 mt-2 text-[15px] text-mut">
            Models this machine can serve — benchmarked on your silicon, not on paper.
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {st.catalog.map((m) => (
              <ModelCard
                key={m.id}
                m={m}
                bench={st.bench[m.id]}
                live={st.serving?.model === m.id}
                admin={admin}
                canAct={canAct}
                onBench={() => act(() => magnitudeCmd("bench", m.id), `benchmark of ${m.label} failed to start`)}
                onRun={() => act(() => magnitudeCmd("run", m.id), `run of ${m.label} failed to start`)}
              />
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-2xl px-6 py-16">
        {err && (
          <div className="mb-5 rounded-xl border border-red-400/30 bg-red-400/5 px-4 py-2.5 text-[13px] text-red-300">
            {err}
          </div>
        )}
        {body}
      </div>
    </div>
  );
}
