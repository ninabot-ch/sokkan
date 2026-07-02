"use client";
import { useEffect, useState } from "react";
import {
  fetchDiff, fetchEnvs, fetchPreviewRepos, fetchPreviewTrigger, shotUrl, startEnv, stopEnv,
} from "@/lib/api";
import type { DiffData, PreviewEnv, PreviewRepo, PreviewTrigger } from "@/lib/types";
import { ago } from "@/lib/fmt";

const QUICK = ["https://sokkan.ninabot.ch", "https://ninjob.ch", "https://nakisa.ch"];

function DiffView({ text }: { text: string }) {
  return (
    <pre className="overflow-auto rounded-lg border border-line bg-[#0b0f16] p-3 text-[11.5px] leading-[1.5]">
      {text.split("\n").map((l, i) => {
        let cls = "text-slate-300";
        if (l.startsWith("+") && !l.startsWith("+++")) cls = "text-emerald-300";
        else if (l.startsWith("-") && !l.startsWith("---")) cls = "text-red-300";
        else if (l.startsWith("@@")) cls = "text-sky-300";
        else if (l.startsWith("diff --git") || l.startsWith("+++") || l.startsWith("---") || l.startsWith("index "))
          cls = "text-mut font-semibold";
        return <div key={i} className={cls}>{l || " "}</div>;
      })}
    </pre>
  );
}

export default function Preview() {
  const [mode, setMode] = useState<"env" | "web" | "diff">("env");
  const [src, setSrc] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const capture = (full: string) => {
    if (!/^https?:\/\//.test(full)) return;
    setLoading(true);
    setSrc(`${shotUrl(full)}&_=${Date.now()}`);
  };

  // env (WIP dev servers)
  const [envs, setEnvs] = useState<PreviewEnv[]>([]);
  const [path, setPath] = useState("/");
  const [busy, setBusy] = useState<string | null>(null);

  // aperçu poussé par une session (MCP open_preview) — c'est le vrai point
  // d'entrée : la session sait ce qu'elle vient de modifier et où le voir
  const [trig, setTrig] = useState<PreviewTrigger | null>(null);
  const [seenTs, setSeenTs] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => fetchPreviewTrigger().then((r) => alive && setTrig(r.trigger)).catch(() => {});
    load();
    const iv = setInterval(load, 5000);
    return () => { alive = false; clearInterval(iv); };
  }, []);
  const showTrig = (t: PreviewTrigger) => {
    setMode("env");
    setPath(t.path);
    setSeenTs(t.ts);
    if (t.url) capture(`${t.url}${t.path}`);
  };
  useEffect(() => {
    if (mode !== "env") return;
    let alive = true;
    const load = () => fetchEnvs().then((e) => alive && setEnvs(e)).catch(() => {});
    load();
    const iv = setInterval(load, 3000);
    return () => { alive = false; clearInterval(iv); };
  }, [mode]);
  const toggleEnv = async (e: PreviewEnv) => {
    setBusy(e.name);
    try { e.running ? await stopEnv(e.name) : await startEnv(e.name); await fetchEnvs().then(setEnvs); }
    finally { setBusy(null); }
  };

  // web
  const [url, setUrl] = useState("https://sokkan.ninabot.ch");
  const [render, setRender] = useState<"shot" | "iframe">("shot");

  // diff
  const [repos, setRepos] = useState<PreviewRepo[]>([]);
  const [repo, setRepo] = useState("");
  const [diff, setDiff] = useState<DiffData | null>(null);
  useEffect(() => { fetchPreviewRepos().then((r) => { setRepos(r); if (r[0]) setRepo((x) => x || r[0].name); }).catch(() => {}); }, []);
  useEffect(() => {
    if (mode !== "diff" || !repo) return;
    let alive = true;
    const load = () => fetchDiff(repo).then((d) => alive && setDiff(d)).catch(() => {});
    load();
    const iv = setInterval(load, 8000);
    return () => { alive = false; clearInterval(iv); };
  }, [mode, repo]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {trig && (
        <div className={`flex flex-wrap items-center gap-2 border-b px-3 py-1.5 text-[12px] ${trig.ts > seenTs ? "border-violet-500/40 bg-violet-500/10" : "border-line bg-panel/40"}`}>
          <span className="text-violet-300">◉ aperçu poussé</span>
          <span className="text-slate-200">
            {trig.tag ? `par la session « ${trig.tag} »` : trig.user ? `par ${trig.user}` : ""} {ago(trig.ts)}
          </span>
          <span className="text-mut">— {trig.env}{trig.path}</span>
          <div className="ml-auto flex items-center gap-1.5">
            <button onClick={() => showTrig(trig)}
              className="rounded bg-violet-600/25 px-2.5 py-0.5 text-[11.5px] text-violet-200 ring-1 ring-violet-500/40 hover:bg-violet-600/40">
              voir (capture)
            </button>
            {trig.preview_url && (
              <a href={`${trig.preview_url}${trig.path}`} target="_blank" rel="noreferrer"
                className="rounded bg-panel2 px-2.5 py-0.5 text-[11.5px] text-slate-200 ring-1 ring-line hover:bg-line">
                ouvrir ↗
              </a>
            )}
          </div>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel/60 px-3 py-1.5">
        <div className="flex overflow-hidden rounded-md border border-line text-[12px]">
          {(["env", "web", "diff"] as const).map((m) => (
            <button key={m} onClick={() => setMode(m)}
              className={`px-3 py-0.5 ${mode === m ? "bg-panel2 text-slate-100" : "text-mut hover:text-slate-200"}`}>
              {m === "env" ? "Env (WIP)" : m === "web" ? "Web" : "Diff git"}
            </button>
          ))}
        </div>

        {mode === "web" && (
          <>
            <input value={url} onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && capture(url)}
              placeholder="https://…"
              className="min-w-0 flex-1 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
            <div className="flex overflow-hidden rounded-md border border-line text-[11px]">
              {(["shot", "iframe"] as const).map((r) => (
                <button key={r} onClick={() => setRender(r)}
                  className={`px-2 py-0.5 ${render === r ? "bg-panel2 text-slate-100" : "text-mut hover:text-slate-200"}`}>
                  {r === "shot" ? "Screenshot" : "Iframe"}
                </button>
              ))}
            </div>
            {render === "shot" && <button onClick={() => capture(url)} className="rounded bg-sea/80 px-3 py-1 text-[12px] font-medium text-white hover:bg-sea">capturer</button>}
          </>
        )}

        {mode === "diff" && (
          <>
            <select value={repo} onChange={(e) => setRepo(e.target.value)}
              className="rounded border border-line bg-panel2 px-1.5 py-1 text-[12px] text-slate-200">
              {repos.map((r) => <option key={r.name} value={r.name}>{r.name} ({r.branch}, {r.modified})</option>)}
            </select>
            {diff && <span className="text-[11px] text-mut">{diff.truncated ? "diff tronqué" : `${diff.status.split("\n").filter(Boolean).length} fichiers`}</span>}
          </>
        )}

        {mode === "env" && (
          <>
            <input value={path} onChange={(e) => setPath(e.target.value)}
              placeholder="/chemin (ex. /dashboard)"
              className="w-48 rounded border border-line bg-[#0b0f16] px-2 py-1 text-[12px] text-slate-100 outline-none focus:border-sea/50" />
            <span className="text-[11px] text-mut">démarre un dev-server → « capturer » (image) ou « ouvrir ↗ » (interactif)</span>
          </>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-3">
        {mode === "diff" ? (
          diff ? (
            <div className="space-y-3">
              {diff.status.trim()
                ? <pre className="rounded-lg border border-line bg-panel2/40 p-2 text-[11.5px] text-slate-300">{diff.status}</pre>
                : <div className="text-[12px] text-mut">aucun changement non commité dans {diff.repo}</div>}
              {diff.diff.trim() && <DiffView text={diff.diff} />}
            </div>
          ) : <div className="mt-10 text-center text-[12px] text-mut">chargement du diff…</div>
        ) : mode === "env" ? (
          <div className="space-y-4">
            <div className="space-y-2">
              {envs.map((e) => (
                <div key={e.name} className="flex items-center gap-2 rounded-lg border border-line bg-panel2/50 p-2">
                  <span className={`h-2 w-2 rounded-full ${e.running ? "bg-emerald-500" : "bg-slate-600"}`} />
                  <div className="min-w-0">
                    <div className="truncate text-[12.5px] text-slate-100">{e.label}</div>
                    <div className="truncate text-[10.5px] text-mut">{e.url} · {e.cwd}{!e.cwd_exists && " ⚠ introuvable"}</div>
                  </div>
                  <div className="ml-auto flex items-center gap-1.5">
                    <button onClick={() => toggleEnv(e)} disabled={busy === e.name}
                      className={`rounded px-2 py-0.5 text-[11.5px] ${e.running ? "bg-red-600/20 text-red-300 ring-1 ring-red-600/30" : "bg-emerald-600/20 text-emerald-300 ring-1 ring-emerald-600/30"} disabled:opacity-40`}>
                      {busy === e.name ? "…" : e.running ? "arrêter" : "démarrer"}
                    </button>
                    {e.running && (
                      <>
                        <button onClick={() => capture(`${e.url}${path}`)}
                          className="rounded bg-panel px-2 py-0.5 text-[11.5px] text-slate-200 ring-1 ring-line hover:bg-line">capturer</button>
                        <a href={`${e.preview_url}${path}`} target="_blank" rel="noreferrer"
                          className="rounded bg-sea/80 px-2 py-0.5 text-[11.5px] font-medium text-white hover:bg-sea">ouvrir ↗</a>
                      </>
                    )}
                  </div>
                </div>
              ))}
              {!envs.length && <div className="text-[12px] text-mut">aucun environnement configuré (preview-envs.json)</div>}
            </div>
            {loading && <div className="text-[12px] text-mut">capture en cours (la 1re compile Next peut prendre ~10-20s)…</div>}
            {src && /* eslint-disable-next-line @next/next/no-img-element */ (
              <img src={src} alt="preview WIP" onLoad={() => setLoading(false)} onError={() => setLoading(false)}
                className="mx-auto max-w-full rounded-lg border border-line" />
            )}
          </div>
        ) : render === "iframe" ? (
          <iframe title="preview" src={url} className="h-full w-full rounded-lg border border-line bg-white" />
        ) : src ? (
          <>
            {loading && <div className="mb-2 text-[12px] text-mut">capture en cours…</div>}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={src} alt="screenshot" onLoad={() => setLoading(false)} onError={() => setLoading(false)}
              className="mx-auto max-w-full rounded-lg border border-line" />
          </>
        ) : (
          <div className="mt-10 text-center text-[13px] text-mut">
            entre une URL et « capturer »
            <div className="mt-2 flex flex-wrap justify-center gap-1.5">
              {QUICK.map((q) => <button key={q} onClick={() => setUrl(q)} className="rounded border border-line bg-panel2 px-2 py-0.5 text-[11px] text-sea hover:bg-line">{q}</button>)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
