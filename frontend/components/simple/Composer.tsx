"use client";
// Simple profile — THE composer: one text area, « Attach documents » (picker + drag and drop),
// the attached files as chips, one main action. Used on the home (start a work) and under a
// thread (reply). A file is sent as soon as it is chosen; a refusal is explained in words.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, uploadDocument, type Doc, type Limits } from "@/lib/simple/api";
import { useT, type T } from "@/lib/simple/i18n";
import type { Key } from "@/lib/simple/dict";
import Icon from "./icons";

interface Pending { key: string; name: string; doc?: Doc; error?: string }

export function uploadError(t: T, e: unknown, name: string, limits: Limits | null): string {
  const mb = limits ? Math.round(limits.max_bytes / 1024 / 1024) : 25;
  if (e instanceof ApiError) {
    const k = `up.err.${e.code}` as Key;
    if (["type", "too_large", "disguised", "empty", "quota", "classified"].includes(e.code)) return t(k, { name, mb });
    if (e.status === 403) return t("up.err.forbidden");
    if (e.status === 413) return t("up.err.too_large", { name, mb });
  }
  return t("up.err.other", { name });
}

/** Refusals a browser can tell at once (the server checks again, on the content). */
export function precheck(t: T, f: File, limits: Limits | null): string | null {
  if (!limits) return null;
  const ext = f.name.includes(".") ? f.name.split(".").pop()!.toLowerCase() : "";
  if (!limits.extensions.includes(ext)) return t("up.err.type", { name: f.name });
  if (f.size > limits.max_bytes) return t("up.err.too_large", { name: f.name, mb: Math.round(limits.max_bytes / 1024 / 1024) });
  if (f.size === 0) return t("up.err.empty", { name: f.name });
  return null;
}

export default function Composer({
  placeholder, submitLabel, busyLabel, limits, session, canAttach = true, disabled = false, compact = false,
  autoFocus = false, allowEmptyWithDocs = false, emptyHint, header, focusKey, onSubmit,
}: {
  placeholder: string; submitLabel: string; busyLabel?: string; limits: Limits | null;
  /** attach uploads to this session at once (thread); none on the home (attached at start) */
  session?: string; canAttach?: boolean; disabled?: boolean; compact?: boolean; autoFocus?: boolean;
  /** the home with a template: documents alone are a valid request */
  allowEmptyWithDocs?: boolean; emptyHint?: string; header?: React.ReactNode; focusKey?: string;
  onSubmit: (text: string, docs: Doc[]) => Promise<void>;
}) {
  const t = useT();
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Pending[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [drop, setDrop] = useState(false);
  const [busy, setBusy] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const depth = useRef(0);

  useEffect(() => { if (focusKey !== undefined) area.current?.focus(); }, [focusKey]);
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [text]);

  const add = useCallback((list: FileList | File[]) => {
    const picked = Array.from(list).slice(0, 20);
    const errs: string[] = [];
    for (const f of picked) {
      const refusal = precheck(t, f, limits);
      if (refusal) { errs.push(refusal); continue; }
      const key = `${f.name}-${f.size}-${f.lastModified}-${Math.random().toString(36).slice(2, 7)}`;
      setFiles((cur) => [...cur, { key, name: f.name }]);
      uploadDocument(f, session)
        .then((doc) => setFiles((cur) => cur.map((p) => (p.key === key ? { ...p, doc, name: doc.name } : p))))
        .catch((e) => {
          setFiles((cur) => cur.filter((p) => p.key !== key));
          setErrors((cur) => [...cur, uploadError(t, e, f.name, limits)]);
        });
    }
    setErrors(errs);
  }, [limits, session, t]);

  const uploading = files.some((f) => !f.doc);
  const docs = files.filter((f) => f.doc).map((f) => f.doc!);
  const canSend = !disabled && !busy && !uploading && (text.trim().length > 0 || (allowEmptyWithDocs && docs.length > 0));

  const submit = async () => {
    if (disabled || busy || uploading) return;
    if (!text.trim() && !(allowEmptyWithDocs && docs.length > 0)) {
      if (emptyHint) setErrors([emptyHint]);
      area.current?.focus();
      return;
    }
    setBusy(true);
    setErrors([]);
    try {
      await onSubmit(text.trim(), docs);
      setText("");
      setFiles([]);
    } catch (e) {
      setErrors([e instanceof Error && e.message ? e.message : t("home.failed")]);
    } finally { setBusy(false); }
  };

  const onDrag = (e: React.DragEvent, enter: boolean) => {
    if (!canAttach || disabled || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
    e.preventDefault();
    depth.current += enter ? 1 : -1;
    setDrop(depth.current > 0);
  };

  return (
    <div className={`sk-composer${compact ? " sk-composer-compact" : ""}${drop ? " is-drop" : ""}`}
      onDragEnter={(e) => onDrag(e, true)} onDragLeave={(e) => onDrag(e, false)}
      onDragOver={(e) => { if (canAttach && !disabled) e.preventDefault(); }}
      onDrop={(e) => {
        if (!canAttach || disabled) return;
        e.preventDefault(); depth.current = 0; setDrop(false);
        if (e.dataTransfer.files?.length) add(e.dataTransfer.files);
      }}>
      {header}
      <label className="sk-visually-hidden" htmlFor={`sk-area-${session || "home"}`}>{placeholder}</label>
      <textarea id={`sk-area-${session || "home"}`} ref={area} value={text} rows={compact ? 1 : 2}
        placeholder={placeholder} disabled={disabled} autoFocus={autoFocus}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }} />
      {files.length > 0 && (
        <ul className="sk-attached" aria-live="polite" style={{ listStyle: "none", margin: 0 }}>
          {files.map((f) => (
            <li key={f.key} className="sk-chip">
              {f.doc ? <Icon name="page" small /> : <span className="sk-spin" aria-hidden />}
              <span className="sk-chip-name">{f.doc ? f.name : t("up.sending", { name: f.name })}</span>
              {f.doc && (
                <button type="button" className="sk-chip-x" aria-label={t("up.remove", { name: f.name })}
                  onClick={() => setFiles((cur) => cur.filter((p) => p.key !== f.key))}><Icon name="close" small /></button>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="sk-composer-bar">
        {canAttach && (
          <>
            <input ref={picker} type="file" multiple hidden tabIndex={-1} aria-hidden
              accept={(limits?.extensions || []).map((x) => `.${x}`).join(",")}
              onChange={(e) => { if (e.target.files?.length) add(e.target.files); e.target.value = ""; }} />
            <button type="button" className="sk-btn" disabled={disabled} aria-label={t("home.attach")} onClick={() => picker.current?.click()}>
              <Icon name="clip" /><span className="sk-hide-narrow">{t("home.attach")}</span>
              <span className="sk-only-narrow" aria-hidden>{t("home.attach.short")}</span>
            </button>
          </>
        )}
        <button type="button" className="sk-btn sk-btn-primary sk-push" disabled={!canSend && !emptyHint} aria-disabled={!canSend}
          onClick={submit}>
          {busy ? <><span className="sk-spin" aria-hidden />{busyLabel || submitLabel}</> : <>{submitLabel}<Icon name="send" /></>}
        </button>
      </div>
      {errors.length > 0 && (
        <div role="alert" style={{ display: "flex", flexDirection: "column", gap: "var(--sk-s1)" }}>
          {errors.map((e, i) => <div key={i} className="sk-error"><Icon name="alert" small /><span>{e}</span></div>)}
        </div>
      )}
      {drop && <div className="sk-drop-veil"><span className="sk-row"><Icon name="upload" />{t("home.drop")}</span></div>}
    </div>
  );
}
