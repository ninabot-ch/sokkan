"use client";
import { useEffect, useState } from "react";
import {
  magnitudeCmd, magnitudeConnect, magnitudeNodeConfig, magnitudePair,
  magnitudeState, magnitudeUnpair,
} from "@/lib/api";
import type {
  MagnitudeBench, MagnitudeEngine, MagnitudeGpuMetrics, MagnitudeMetrics, MagnitudeModel, MagnitudeNode,
  MagnitudeProfile, MagnitudeRunTarget, MagnitudeServing, MagnitudeState, MagnitudeStatus,
} from "@/lib/api";
import { useCan } from "@/lib/me";
import MemoryCard from "@/components/MemoryCard";

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

/** Labelled usage bar (CPU, RAM, GPU busy, VRAM). Amber from 85 %, red from 95 %. */
function Meter({ label, pct, value, title, alarm = true }: {
  label: string; pct: number | null | undefined; value: string; title?: string; alarm?: boolean;
}) {
  const p = pct == null ? null : Math.min(100, Math.max(0, pct));
  // a busy GPU is a working GPU: only memory gets warning colours
  const tone = p == null ? "bg-line" : !alarm ? "bg-sky-400/80" : p >= 95 ? "bg-red-400" : p >= 85 ? "bg-amber-400" : "bg-sea";
  return (
    <div title={title}>
      <div className="flex items-baseline justify-between gap-2 text-[11px]">
        <span className="text-mut">{label}</span>
        <span className="tabular-nums text-slate-300">{value}</span>
      </div>
      <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-line/60" role="progressbar"
        aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={p == null ? undefined : Math.round(p)}>
        {p != null && <div className={`h-full rounded-full ${tone} transition-all duration-700`} style={{ width: `${Math.max(1.5, p)}%` }} />}
      </div>
    </div>
  );
}

/** Tiny busy-% history (last ~2 min). */
function Spark({ values, label }: { values: (number | null)[]; label: string }) {
  const pts = values.map((v, i) => (v == null ? null : [i, v] as const)).filter(Boolean) as (readonly [number, number])[];
  if (pts.length < 2) return null;
  const n = Math.max(values.length - 1, 1);
  const d = pts.map(([i, v], k) => `${k ? "L" : "M"}${(i / n) * 100},${20 - (v / 100) * 18}`).join(" ");
  return (
    <svg viewBox="0 0 100 20" preserveAspectRatio="none" className="h-4 w-full text-sea/70" role="img" aria-label={label}>
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

const pctOf = (used: number | null | undefined, total: number | null | undefined) =>
  used == null || !total ? null : (used / total) * 100;

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
        Measure what your machines can really run. Locally. Privately.
      </p>
      {admin ? (
        <button
          onClick={onPair}
          disabled={busy}
          className="mt-4 rounded-xl bg-sea/80 px-8 py-3 text-[15px] font-medium text-white transition-all duration-300 hover:bg-sea active:scale-[0.98] disabled:opacity-40"
        >
          Pair a machine
        </button>
      ) : (
        <p className="mt-4 text-[13px] text-mut">An admin can pair a machine to get started.</p>
      )}
    </div>
  );
}

function PairingCard({ command }: { command: string }) {
  return (
    <div className="w-full rounded-2xl border border-line bg-panel2/40 p-6 transition-all duration-500">
      <p className="mb-4 text-[14px] leading-relaxed text-mut">
        Run this on the machine to pair. The token is shown only once.
      </p>
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
  const d = (status.detail || "").toLowerCase();
  const hint = !isError ? "" : d.includes("prebuilt") || d.includes("no recent llama.cpp")
    ? "On the machine: install Docker (Intel cards use the SYCL image), or set MAGNITUDE_LLAMA_TAG to a llama.cpp release that has a build for it."
    : d.includes("exited early") || d.includes("not healthy")
    ? "The reason is in ~/.sokkan/magnitude/run/llama-server.log on the machine. Out of memory: a smaller model, or a lower MAGNITUDE_CTX."
    : d.includes("docker") ? "Check that the Docker daemon runs and that the agent's user may use it."
    : d.includes("cannot reach github") ? "The machine needs internet access once to download llama.cpp, or a build copied under ~/.sokkan/magnitude/bin." : "";
  return (
    <div
      className={`rounded-2xl border p-5 backdrop-blur transition-all duration-500 ${
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
      {hint && <div className="mt-2 text-[12.5px] leading-relaxed text-slate-300">{hint}</div>}
    </div>
  );
}

const ENGINE_LABEL: Record<string, string> = {
  vllm: "vLLM", "llama.cpp": "llama.cpp", ollama: "ollama", "openai-compatible": "OpenAI-compatible",
};
const shortGpu = (name: string) => name.replace(/\(R\)|\(TM\)/g, "").replace(/^Intel\s+/, "").replace(/\s+Graphics$/, "").replace(/\s+/g, " ").trim();

function CardTile({ d, live, engines, allowed, hist }: {
  d: { index: number; name: string; vram_gb: number | null };
  live: MagnitudeGpuMetrics | undefined; engines: MagnitudeEngine[]; allowed: boolean | null; hist: (number | null)[];
}) {
  const on = engines.filter((e) => e.cards?.includes(d.index));
  const total = live?.vram_total_gb ?? d.vram_gb;
  return (
    <div className="rounded-xl border border-line bg-ink/40 px-3.5 py-3">
      <div className="flex items-baseline gap-2">
        <span className="text-[11px] tabular-nums text-mut">#{d.index}</span>
        <span className="truncate text-[13px] font-medium text-slate-200" title={d.name}>{shortGpu(d.name)}</span>
        {allowed === true && (
          <span className="ml-auto shrink-0 rounded bg-emerald-400/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-300">Run</span>
        )}
        {allowed === false && on.length > 0 && (
          <span className="ml-auto shrink-0 rounded bg-amber-400/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-200" title="serves other engines: Magnitude's Run does not use it">prod</span>
        )}
      </div>
      <div className="mt-0.5 truncate text-[11.5px] text-mut" title={on.map((e) => e.model).join(", ")}>
        {on.length ? on.map((e) => e.model).join(" · ") : "no engine found on this card"}
      </div>
      {live ? (
        <div className="mt-2.5 space-y-2">
          <Meter label="VRAM" pct={pctOf(live.vram_used_gb, total)}
            value={live.vram_used_gb != null && total ? `${live.vram_used_gb.toFixed(1)} / ${total.toFixed(1)} GB` : "—"} />
          <Meter label="busy" alarm={false} pct={live.util_pct} value={live.util_pct != null ? `${Math.round(live.util_pct)} %` : "—"} />
          <Spark values={hist} label={`card ${d.index} busy over the last 2 minutes`} />
          <div className="flex gap-3 text-[11px] tabular-nums text-mut">
            {live.temp_c != null && <span className={live.temp_c >= 90 ? "text-red-300" : live.temp_c >= 80 ? "text-amber-300" : ""}>{Math.round(live.temp_c)} °C</span>}
            {live.power_w != null && <span>{Math.round(live.power_w)} W</span>}
          </div>
        </div>
      ) : (
        <div className="mt-1.5 text-[11.5px] tabular-nums text-slate-400">{d.vram_gb != null ? `${d.vram_gb.toFixed(1)} GB` : "—"}</div>
      )}
    </div>
  );
}

/** One sentence: which cards Magnitude may use for Run, which ones serve other engines. */
function RunScope({ profile, target, engines }: { profile: MagnitudeProfile; target: MagnitudeRunTarget | null | undefined; engines: MagnitudeEngine[] }) {
  const devs = profile.gpu?.devices ?? [];
  if (!target || devs.length === 0) return null;
  const allowed = profile.gpu?.run_devices;
  const prod = devs.filter((d) => engines.some((e) => e.cards?.includes(d.index))).map((d) => d.index);
  const list = (xs: number[]) => xs.map((x) => `#${x}`).join(", ");
  return (
    <div className="mt-5 rounded-xl border border-line/80 bg-ink/30 px-4 py-3 text-[12.5px] leading-relaxed text-slate-300">
      <div>
        <span className="text-mut">Cards for Run: </span>
        {allowed == null ? (
          <b className="font-medium text-amber-200">all ({list(devs.map((d) => d.index))}) — not restricted</b>
        ) : allowed.length === 0 ? (
          <b className="font-medium text-slate-100">none — models run on the CPU</b>
        ) : (
          <b className="font-medium text-emerald-300">{list(allowed)}</b>
        )}
        {prod.length > 0 && (<><span className="text-mut"> · production: </span><span>{list(prod)}</span></>)}
      </div>
      <div className="mt-0.5 text-[11.5px] text-mut">
        {allowed == null && prod.length > 0
          ? "A Run would spread over cards that serve other engines. Set MAGNITUDE_GPU_DEVICES on the agent (e.g. 2, or none)."
          : "Set on the machine by MAGNITUDE_GPU_DEVICES (card numbers, or none); the cockpit cannot widen it."}
        {target.usable_gb != null && ` Room for a model: ${target.usable_gb.toFixed(1)} GB ${target.basis === "free" ? "free on those cards now" : target.basis === "ram" ? "of RAM" : "on those cards"}.`}
      </div>
    </div>
  );
}

function NodeLoad({ m, cores }: { m: MagnitudeMetrics; cores: number }) {
  return (
    <div className="mt-5 grid grid-cols-2 gap-4" aria-label="Machine load">
      <Meter label={`CPU · ${cores} cores${m.load1 != null ? ` · load ${m.load1.toFixed(1)}` : ""}`} pct={m.cpu_pct} alarm={false}
        value={m.cpu_pct != null ? `${Math.round(m.cpu_pct)} %` : "—"} />
      <Meter label="RAM" pct={pctOf(m.ram_used_gb, m.ram_total_gb)}
        value={m.ram_used_gb != null && m.ram_total_gb ? `${m.ram_used_gb.toFixed(0)} / ${m.ram_total_gb.toFixed(0)} GB` : "—"} />
    </div>
  );
}

function HardwareCard({ profile, online, lastSeen, engines, metrics, target }: {
  profile: MagnitudeProfile; online: boolean; lastSeen: number | null; engines: MagnitudeEngine[];
  metrics: MagnitudeMetrics | null | undefined; target: MagnitudeRunTarget | null | undefined;
}) {
  const g = profile.gpu;
  const cls = profile.class;
  const devices = g?.devices && g.devices.length > 1 ? g.devices : null;
  const live = (i: number, pci?: string | null) =>
    metrics?.gpus.find((x) => (pci && x.pci ? x.pci === pci : x.index === i));
  const allowedOf = (i: number): boolean | null => (g?.run_devices == null ? null : g.run_devices.includes(i));
  const hist = (i: number) => (metrics?.history ?? []).map((h) => h.gpu[i] ?? null);
  const single = !devices && g ? live(0) : undefined;
  return (
    <div className={`rounded-2xl border border-line bg-panel2/40 p-6 transition-all duration-500 ${online ? "" : "opacity-60"}`}>
      <div className="flex items-center gap-5">
        <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-full border border-brass/40 bg-brass/10">
          <span className={`font-semibold text-brass ${cls.length > 2 ? "text-[13px]" : "text-xl"}`}>
            {cls === "unsupported" ? "—" : cls}
          </span>
        </div>
        <div className="min-w-0">
          <div className="text-2xl font-semibold tracking-tight text-slate-100 md:text-3xl" title={g ? g.name : profile.cpu}>
            {g ? (devices ? shortGpu(g.name) : g.name) : profile.cpu}
          </div>
          <div className="mt-1 text-[13px] text-mut">
            {g ? `${g.vendor} · ${g.backend.replace("_", " ")}` : "CPU inference"}
            {devices && profile.class_per_card ? ` · class ${profile.class_per_card} per card` : ""}
            {devices && g?.vram_total_gb != null ? ` · ${g.vram_total_gb.toFixed(1)} GB in total` : ""}
            {!online && <span className="ml-2 text-amber-300/80">· agent offline{lastSeen ? ` — last seen ${upFor(lastSeen)} ago` : ""}</span>}
          </div>
        </div>
      </div>
      {/* the machine itself: CPU model, cores, RAM — always visible, live load when the agent sends it */}
      <div className="mt-5 text-[13px] text-slate-300">
        {profile.cpu}
        <span className="text-mut"> · {profile.cores} cores · {gb(profile.ram_gb)} GB RAM{g?.driver ? ` · driver ${g.driver}` : ""}</span>
      </div>
      {metrics && <NodeLoad m={metrics} cores={profile.cores} />}
      {devices && (
        <div className="mt-5 grid grid-cols-1 gap-2.5 sm:grid-cols-2" aria-label="GPUs of this node">
          {devices.map((d) => (
            <CardTile key={d.index} d={d} live={live(d.index, d.pci)} engines={engines} allowed={allowedOf(d.index)} hist={hist(d.index)} />
          ))}
        </div>
      )}
      {!devices && g && (single ? (
        <div className="mt-5 grid grid-cols-2 gap-4">
          <Meter label="VRAM" pct={pctOf(single.vram_used_gb, single.vram_total_gb ?? g.vram_total_gb)}
            value={single.vram_used_gb != null ? `${single.vram_used_gb.toFixed(1)} / ${(single.vram_total_gb ?? g.vram_total_gb ?? 0).toFixed(1)} GB` : "—"} />
          <Meter label={`GPU busy${single.temp_c != null ? ` · ${Math.round(single.temp_c)} °C` : ""}${single.power_w != null ? ` · ${Math.round(single.power_w)} W` : ""}`}
            pct={single.util_pct} alarm={false} value={single.util_pct != null ? `${Math.round(single.util_pct)} %` : "—"} />
        </div>
      ) : g.vram_total_gb != null && (
        /* older agents: no live figures — free memory from the profile when known */
        <div className="mt-6">
          <div className="mb-1.5 flex items-baseline justify-between text-[12.5px]">
            <span className="text-mut">VRAM</span>
            <span className="tabular-nums text-slate-300">
              {g.vram_free_gb != null ? `${gb(g.vram_free_gb)} GB free of ` : ""}{gb(g.vram_total_gb)} GB
            </span>
          </div>
          {g.vram_free_gb != null && (
            <Progress pct={g.vram_total_gb > 0 ? ((g.vram_total_gb - g.vram_free_gb) / g.vram_total_gb) * 100 : 0} />
          )}
        </div>
      ))}
      {devices && <RunScope profile={profile} target={target} engines={engines} />}
      {online && !metrics && (
        <div className="mt-4 text-[11.5px] text-mut">
          Live load (busy %, memory, temperature, power) needs agent 0.3 or later{profile.agent_version ? ` — this one is ${profile.agent_version}` : ""}.
        </div>
      )}
    </div>
  );
}

function ServingCard({
  serving, label, connected, admin, online, busy, onConnect, onStop, where,
}: {
  serving: MagnitudeServing; label: string; connected: boolean; admin: boolean; where?: string;
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
          <div className="mt-0.5 text-[13px] tabular-nums text-mut">
            {serving.external ? `${ENGINE_LABEL[serving.engine || ""] || serving.engine} on port ${serving.port} · attached ` : `${where ? `${where} · ` : ""}up `}
            {upFor(serving.since)}{serving.external ? " ago" : ""}
          </div>
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
            {serving.external ? "Detach" : "Stop"}
          </button>
        </div>
      )}
      <p className="mt-4 text-[13px] leading-relaxed text-mut">
        Every new session will run on your own hardware. Zero cloud. Zero cost per token.
        {serving.external ? " Detach only removes Magnitude's bridge — the engine keeps running." : ""}
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
            {bench.on ? `${bench.on.toUpperCase()} · ` : ""}
            {bench.prefill_tok_s != null && `${Math.round(bench.prefill_tok_s)} tok/s prefill`}
            {bench.power_avg_w != null && ` · ${Math.round(bench.power_avg_w)} W`}
            {bench.eur_per_mtok_gen != null && ` · €${bench.eur_per_mtok_gen.toFixed(2)}/Mtok`}
          </div>
          <div className="mt-0.5 text-[11px] text-mut/80">
            measured {new Date(bench.at * 1000).toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" })}
            {!bench.on ? " — on the hardware of that day" : ""}
          </div>
        </div>
      )}
      {admin && !m.fits && m.fit === "no" && (
        <div className="mt-4 text-[11.5px] text-mut">Needs about {gb(m.weights_gb + 1.2)} GB where it would run.</div>
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

/** Engines found running on the node (vLLM, llama.cpp, ollama…), not started by Magnitude. */
function EnginesCard({
  engines, admin, canAct, onAttach,
}: {
  engines: MagnitudeEngine[]; admin: boolean; canAct: boolean;
  onAttach: (e: MagnitudeEngine) => void;
}) {
  return (
    <div className="rounded-2xl border border-line bg-panel2/40 p-6 transition-all duration-500">
      <div className="flex items-baseline gap-2">
        <h3 className="text-[15px] font-semibold tracking-tight text-slate-100">Engines running</h3>
        <span className="text-[12px] text-mut">already served on this machine — use one for SOKKAN sessions</span>
      </div>
      <ul className="mt-4 divide-y divide-line/60">
        {engines.map((e) => (
          <li key={`${e.port}-${e.model}`} className="flex items-center gap-3 py-2.5">
            <span className={`h-2 w-2 shrink-0 rounded-full ${e.healthy ? "bg-emerald-400" : "bg-amber-400"}`}
              title={e.healthy ? "healthy" : "not answering /health"} />
            <span className="sr-only">{e.healthy ? "healthy" : "not answering"}</span>
            <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1">
            <span className="min-w-0 font-mono text-[13px] text-slate-100">{e.model}</span>
            <span className="rounded bg-sea/15 px-1.5 py-0.5 text-[10.5px] font-medium text-sky-200">{ENGINE_LABEL[e.engine] || e.engine}</span>
            <span className="text-[12px] tabular-nums text-mut">:{e.port}</span>
            <span className="text-[12px] text-mut">
              {e.cards?.length ? `GPU ${e.cards.map((c) => `#${c}`).join("+")}` : "card unknown"}
            </span>
            {e.ctx != null && (
              <span className={`text-[12px] tabular-nums ${e.ctx_ok === false ? "text-amber-300" : "text-mut"}`}
                title={e.ctx_ok === false ? "a Claude Code session opens at ~41k tokens of prompt: this context is too short for it" : undefined}>
                {Math.round(e.ctx / 1024)}k ctx{e.ctx_ok === false ? " — short for a session" : ""}
              </span>
            )}
            </span>
            <span className="shrink-0">
              {e.serving ? (
                <span className="text-[12px] font-medium text-emerald-300">bridged ✓</span>
              ) : admin ? (
                <button onClick={() => onAttach(e)} disabled={!canAct || !e.healthy}
                  className="rounded-lg border border-line px-3 py-1 text-[12.5px] text-slate-300 transition-all duration-300 hover:bg-panel2 disabled:opacity-40">
                  Use for sessions
                </button>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-[12px] leading-relaxed text-mut">
        « Use for sessions » puts Magnitude&apos;s bridge (Anthropic ⇄ OpenAI) in front of the engine; then « Connect to SOKKAN ».
        The engine itself is not touched.
      </p>
    </div>
  );
}

/** Endpoint of a node as sessions reach it — subtle, admin-editable inline. */
function NodeEndpoint({
  node, admin, busy, onSave,
}: {
  node: MagnitudeNode; admin: boolean; busy: boolean; onSave: (url: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [url, setUrl] = useState(node.shim_url);
  if (!editing) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-mut">
        <span>
          Sessions reach this node at <code className="rounded bg-ink px-1.5 py-0.5 font-mono text-[11.5px] text-slate-300">{node.shim_url}</code>
        </span>
        {admin && (
          <button
            onClick={() => { setUrl(node.shim_url); setEditing(true); }}
            className="min-h-6 px-1 text-sky-300 transition-colors hover:text-sky-200"
          >
            edit
          </button>
        )}
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2">
      <input
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="http://<node-host>:8790"
        className="w-72 rounded-lg border border-line bg-[#0b0f16] px-3 py-1.5 font-mono text-[12px] text-slate-200 outline-none focus:border-sea/50"
      />
      <button
        onClick={() => { onSave(url); setEditing(false); }}
        disabled={busy}
        className="rounded-lg bg-sea/80 px-3 py-1.5 text-[12px] font-medium text-white transition-all hover:bg-sea disabled:opacity-40"
      >
        Save
      </button>
      <button onClick={() => setEditing(false)} className="text-[12px] text-mut hover:text-slate-300">
        cancel
      </button>
    </div>
  );
}

/** Where a Run would execute, in words (header of the catalogue). */
function whereText(p: MagnitudeProfile, t: MagnitudeRunTarget | null | undefined): string {
  const room = t?.usable_gb != null ? ` — ${t.usable_gb.toFixed(1)} GB ${t.basis === "free" ? "free right now" : t.basis === "ram" ? "of RAM for the model" : "available"}` : "";
  if (!t) return "";
  if (t.where === "cpu") {
    if (p.gpu?.run_devices && p.gpu.run_devices.length === 0) return `On the CPU: no card is open to Magnitude on this machine${room}. Slower, but it leaves the GPUs to production.`;
    if (p.gpu?.vendor === "intel" && !p.gpu.offload) return `On the CPU: no Vulkan driver for these cards${room}.`;
    return `On the CPU${room}.`;
  }
  const cards = t.cards?.length ? ` card${t.cards.length > 1 ? "s" : ""} ${t.cards.map((c) => `#${c}`).join(", ")}` : " the GPU";
  const how = p.runtime === "docker" ? " with the llama.cpp SYCL image (Docker)" : p.gpu?.offload === "vulkan" || p.gpu?.backend === "vulkan" ? " with llama.cpp Vulkan" : "";
  return `On${cards}${how}${room}. Fit is computed on that memory, with room for the context.`;
}

function NodeSection({
  node, admin, busy, act,
}: {
  node: MagnitudeNode; admin: boolean; busy: boolean;
  act: (fn: () => Promise<unknown>, failMsg: string) => void;
}) {
  const labelOf = (id: string | null | undefined) =>
    (id && node.catalog.find((m) => m.id === id)?.label) || id || "model";
  const engines = node.engines ?? [];
  const opRunning = ["benching", "downloading", "starting"].includes(node.status.phase);
  const canAct = node.online && !busy && !opRunning;

  const unpair = () => {
    if (!confirm(`Unpair ${node.name}? Its agent token is revoked and its bench data cleared.`)) return;
    act(() => magnitudeUnpair(node.id), "unpair failed");
  };

  return (
    <section className="space-y-4">
      <div className="flex items-baseline gap-2.5">
        <span className={`h-2 w-2 shrink-0 self-center rounded-full ${node.online ? "bg-emerald-400" : "bg-line"}`} />
        <span className="text-[17px] font-semibold tracking-tight text-slate-100">{node.name}</span>
        {node.connected && (
          <span className="rounded-full border border-emerald-400/30 bg-emerald-400/10 px-2 py-0.5 text-[10.5px] font-medium text-emerald-300">
            powering SOKKAN
          </span>
        )}
        {admin && (
          <button onClick={unpair} className="ml-auto min-h-6 px-1 text-[12px] text-mut transition-colors hover:text-slate-300">
            Unpair
          </button>
        )}
      </div>

      {node.status.phase !== "idle" && node.status.phase !== "serving" && (
        <OpBanner status={node.status} label={labelOf(node.status.model)} />
      )}

      {node.serving && (
        <ServingCard
          serving={node.serving}
          label={labelOf(node.serving.model)}
          connected={node.connected}
          admin={admin}
          online={node.online}
          busy={busy}
          onConnect={() => act(() => magnitudeConnect(node.id), "connect failed — model not serving or LLM config is operator-managed")}
          onStop={() => act(() => magnitudeCmd(node.id, "stop"), "stop failed")}
          where={node.run_target?.where === "cpu" ? "on the CPU" : node.run_target?.cards?.length ? `on card ${node.run_target.cards.map((c) => `#${c}`).join("+")}` : undefined}
        />
      )}

      {node.stale && !node.online && (
        <div className="rounded-2xl border border-amber-400/30 bg-amber-400/5 p-5 text-[13px] text-amber-100">
          <div className="font-medium">
            {node.last_seen
              ? `Offline since ${new Date(node.last_seen * 1000).toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}`
              : `Never connected — paired ${node.paired_at ? `${upFor(node.paired_at)} ago` : "a while ago"}`}
          </div>
          <div className="mt-1 text-[12.5px] text-amber-100/80">
            {node.last_seen
              ? "Its agent stopped syncing: check the magnitude-agent service on the machine (and the tunnel, if any)."
              : "The install command was never run on a machine, or its token was lost. Unpair it, then « Pair a machine » again."}
          </div>
          {admin && (
            <button onClick={unpair}
              className="mt-3 rounded-lg border border-amber-300/40 px-4 py-1.5 text-[12.5px] text-amber-100 transition-colors hover:bg-amber-400/10">
              Unpair {node.name}
            </button>
          )}
        </div>
      )}

      {node.profile ? (
        <HardwareCard profile={node.profile} online={node.online} lastSeen={node.last_seen} engines={engines}
          metrics={node.metrics} target={node.run_target} />
      ) : !node.stale && (
        <div className="rounded-2xl border border-line bg-panel2/40 p-6 text-[13px] text-mut transition-all duration-500">
          <span className="animate-pulse">
            {node.online ? "profiling hardware…" : "waiting for the agent…"}
          </span>
        </div>
      )}

      {engines.length > 0 && (
        <EnginesCard
          engines={engines}
          admin={admin}
          canAct={canAct}
          onAttach={(e) => act(() => magnitudeCmd(node.id, "attach", e.model, e.port), `could not bridge ${e.model}`)}
        />
      )}

      <NodeEndpoint
        node={node}
        admin={admin}
        busy={busy}
        onSave={(url) => act(() => magnitudeNodeConfig(node.id, { shim_url: url }), "saving endpoint failed")}
      />

      {node.profile && (
        <div className="pt-2">
          <h3 className="text-[15px] font-semibold tracking-tight text-slate-100">Models Magnitude can download and run</h3>
          <div className="mt-0.5 text-[12px] text-mut">{whereText(node.profile, node.run_target)}</div>
        </div>
      )}
      {node.profile && (
        <div className="grid grid-cols-1 gap-4 pt-1 md:grid-cols-2">
          {node.catalog.map((m) => (
            <ModelCard
              key={m.id}
              m={m}
              bench={node.bench[m.id]}
              live={node.serving?.model === m.id}
              admin={admin}
              canAct={canAct}
              onBench={() => act(() => magnitudeCmd(node.id, "bench", m.id), `benchmark of ${m.label} failed to start`)}
              onRun={() => act(() => magnitudeCmd(node.id, "run", m.id), `run of ${m.label} failed to start`)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

// ————— the tab —————

export default function Magnitude() {
  const admin = useCan("admin");
  const [st, setSt] = useState<MagnitudeState | null>(null);
  const [pairing, setPairing] = useState<{ node: string; command: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    const load = () => magnitudeState().then((d) => alive && setSt(d)).catch(() => {});
    load();
    const iv = setInterval(load, 2000);
    return () => { alive = false; clearInterval(iv); };
  }, []);

  // le node en cours d'appairage vient en ligne → la carte pairing a fini son travail
  useEffect(() => {
    if (pairing && st?.nodes.some((n) => n.id === pairing.node && n.online)) setPairing(null);
  }, [st, pairing]);

  const act = async (fn: () => Promise<unknown>, failMsg: string) => {
    setBusy(true);
    setErr("");
    try { await fn(); } catch (e) {
      // the server says why (e.g. « qwen3-32b does not fit on the CPU (12.6 GB available) »)
      const m = (e as Error)?.message || "";
      setErr(m && !m.includes(" → ") ? `${failMsg}: ${m}` : failMsg);
    } finally { setBusy(false); }
  };

  const pair = () =>
    act(async () => {
      const r = await magnitudePair();
      setPairing({ node: r.node, command: r.command });
    }, "pairing failed");

  if (!st) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-[13px] text-mut">…</div>
    );
  }

  // les nodes en attente de premier contact (jamais vus online, sans profil) ne
  // s'affichent que via la carte pairing — sauf s'ils ont déjà vécu
  const nodes = st.nodes.filter((n) => n.profile || n.online || n.last_seen || n.id !== pairing?.node);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-2xl px-6 py-12">
        {err && (
          <div className="mb-5 rounded-xl border border-red-400/30 bg-red-400/5 px-4 py-2.5 text-[13px] text-red-300">
            {err}
          </div>
        )}
        {!st.paired && !pairing ? (
          <Hero admin={admin} busy={busy} onPair={pair} />
        ) : (
          <div className="space-y-10">
            <div className="flex items-baseline justify-between">
              <h1 className="text-4xl font-semibold tracking-tight text-slate-100 md:text-5xl">Magnitude</h1>
              {admin && !pairing && (
                <button
                  onClick={pair}
                  disabled={busy}
                  className="rounded-lg border border-line px-4 py-1.5 text-[13px] text-slate-300 transition-all duration-300 hover:bg-panel2 disabled:opacity-40"
                >
                  Pair a machine
                </button>
              )}
            </div>
            {nodes.map((n) => (
              <NodeSection key={n.id} node={n} admin={admin} busy={busy} act={act} />
            ))}
            {pairing && <PairingCard command={pairing.command} />}
          </div>
        )}
        <div className="mt-12">
          <MemoryCard admin={admin} />
        </div>
      </div>
    </div>
  );
}
