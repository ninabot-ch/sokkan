"use client";
// 3.3 — Setup › Secrets: where this instance keeps its secrets and encryption keys (file,
// OpenBao, Kubernetes), why, and a live « Test connection ». Instance admins only: the
// route answers 403 to anyone else and the card is then not rendered.
import { useEffect, useState } from "react";
import { secretsProvider, secretsProviderTest, type SecretsHealth, type SecretsProviderState } from "@/lib/api";

const LABEL: Record<string, string> = { file: "Files on this server", openbao: "OpenBao / Vault", kubernetes: "Kubernetes Secrets" };
const CONFIG_LABEL: Record<string, string> = {
  address: "Address", namespace: "Namespace", auth: "Authentication", tls: "TLS",
  kv: "Secrets (KV v2)", transit: "Key wrapping (transit)", key_files: "Key files", store: "Store",
  api: "API", secrets: "Secrets",
};

function fmtVal(v: string | boolean | string[]) {
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.join(", ");
  return v || "—";
}

function ago(ts: number) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

export default function SecretsProvider() {
  const [st, setSt] = useState<SecretsProviderState | null>(null);
  const [hidden, setHidden] = useState(false);
  const [test, setTest] = useState<SecretsHealth | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    secretsProvider().then((r) => { setSt(r); setTest(r.last_test); })
      .catch((e) => { if (String(e).includes("403")) setHidden(true); else setErr(String(e)); });
  }, []);
  if (hidden) return null;
  if (!st) return err ? <div role="alert" className="text-[11px] text-red-400">{err}</div> : null;
  const run = () => {
    setBusy(true); setErr("");
    secretsProviderTest().then(setTest).catch((e) => setErr(String(e))).finally(() => setBusy(false));
  };
  const cfg = Object.entries(st.config).filter(([k]) => k !== "provider");
  return (
    <section aria-labelledby="secrets-provider-h" className="rounded-lg border border-line bg-panel2/40 p-3 text-[12.5px]">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="secrets-provider-h" className="font-semibold text-slate-100">Where secrets are kept</h3>
        <span className="rounded bg-sea/20 px-1.5 py-px text-[11px] text-sky-200" data-testid="secrets-provider">{LABEL[st.provider] || st.provider}</span>
        <button onClick={run} disabled={busy}
          className="ml-auto rounded border border-line px-3 py-1 text-[12px] text-slate-200 hover:bg-panel2 disabled:opacity-50">
          {busy ? "Testing…" : st.provider === "file" ? "Check key files" : "Test connection"}
        </button>
      </div>
      <div className="mt-1 text-[11px] text-slate-400">Selected because: {st.reason}.</div>

      {st.warning && (
        <div role="status" className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-[11.5px] text-amber-100">
          <b>Keys are stored next to the data.</b> {st.warning}
        </div>
      )}
      {st.clear_keys_on_disk && st.clear_keys_on_disk.length > 0 && (
        <div role="status" className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-[11.5px] text-amber-100">
          Clear key files are still on disk ({st.clear_keys_on_disk.join(", ")}): finish the migration with
          <span className="font-mono"> secrets-migrate.py --purge</span>.
        </div>
      )}
      {st.error && <div role="alert" className="mt-2 text-[11.5px] text-red-300">{st.error}</div>}

      {cfg.length > 0 && (
        <dl className="mt-2 grid grid-cols-1 gap-x-3 gap-y-0.5 text-[11.5px] sm:grid-cols-[max-content_1fr]">
          {cfg.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-slate-400">{CONFIG_LABEL[k] || k}</dt>
              <dd className={`break-all font-mono ${k === "tls" && String(v).startsWith("plain") ? "text-amber-300" : "text-slate-200"}`}>
                {fmtVal(v)}{k === "tls" && String(v).startsWith("plain") ? " ⚠" : ""}
              </dd>
            </div>
          ))}
        </dl>
      )}

      {test && (
        <div role="status" aria-live="polite"
          className={`mt-2 rounded border p-2 text-[11.5px] ${test.ok ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-100" : "border-red-500/40 bg-red-500/10 text-red-100"}`}>
          <b>{test.provider === "file" ? (test.ok ? "Check OK" : "Check failed") : (test.ok ? "Connection OK" : "Connection failed")}</b> — {test.detail}
          <span className="text-slate-400"> · {ago(test.checked_at)}{test.duration_ms !== undefined ? ` · ${test.duration_ms} ms` : ""}</span>
          {Object.keys(test.checks || {}).length > 0 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-slate-300">Details</summary>
              <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-slate-300">
                {Object.entries(test.checks).map(([k, v]) => (
                  <li key={k} className="break-all">{k}: {typeof v === "string" || typeof v === "number" ? String(v) : JSON.stringify(v)}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
      {err && <div role="alert" className="mt-2 text-[11px] text-red-400">{err}</div>}
      <div className="mt-2 text-[11px] text-slate-400">
        Guide: <span className="font-mono">docs/enterprise/SECRETS.md</span> — OpenBao, migration, rotation, backups.
      </div>
    </section>
  );
}
