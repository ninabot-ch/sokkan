"use client";
// Simple profile — « Memory »: what SOKKAN retains for this project, to READ. The list and
// the note come from the same two routes as the cockpit's Memory tab (GET /api/memory/notes,
// GET /api/memory/note/{name}): the project, the shared notes and the person's clearance are
// applied there, and a note that is read is journaled there as it is from the cockpit. Nothing
// is written from this page.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useT, useUI, when } from "@/lib/simple/i18n";
import Icon from "./icons";
import Markdown from "./Markdown";

// `title`: the title a person reads, said by the server (3.6.4: frontmatter title, then the
// note's `# heading`, then its description, the name made readable last)
interface Note { name: string; title?: string; description: string; mtime: number | null }

/** « procedure-de-devis » → « Procedure de devis » (the name of a note is a slug). */
export function noteTitle(name: string): string {
  const s = name.replace(/\.md$/i, "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : name;
}

/** What a person reads of a note: without the technical first line (« [note x — updated …] »)
 *  and the front matter; its own first heading, when it has one, is its title. */
export function readable(name: string, raw: string, known = ""): { title: string; text: string } {
  let text = raw.replace(/^\s*\[note [^\]\n]*\]\s*\n?/, "");
  text = text.replace(/^\s*---\n[\s\S]*?\n---\s*\n?/, "").trim();
  let title = known || noteTitle(name);
  const m = text.match(/^#\s+(.+?)\s*#*\s*(?:\n|$)/);
  // its opening heading is the title (unless the server already said one) and is not repeated below
  if (m) { title = known || m[1].replace(/[*_`]/g, "").trim() || title; text = text.slice(m[0].length).trim(); }
  // a link to another note is shown as that note's name
  text = text.replace(/\[\[([^\]|\n]+)(?:\|([^\]\n]+))?\]\]/g, (_x, target: string, label?: string) => `*${label || noteTitle(target)}*`);
  return { title, text };
}

/** The first lines of a note as plain words (no heading mark, no list bullet, no emphasis). */
function opening(text: string): string {
  const flat = text.replace(/^\s*(?:#{1,6}|[-*+]|\d+[.)])\s+/gm, "").replace(/[*_`|]/g, "").replace(/\s+/g, " ").trim();
  return flat.length > 220 ? `${flat.slice(0, 219).trimEnd()}…` : flat;
}

const ENRICH_MAX = 40;   // openings are read for the first notes WITHOUT a description only

export default function Memory() {
  const t = useT();
  const { lang } = useUI();
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Note | null>(null);
  // the title comes with the list (the server reads it from the note); the first lines of a
  // note are read here only for a note that has no description to show under its title
  const [own, setOwn] = useState<Record<string, { title: string; start: string }>>({});
  useEffect(() => {
    if (!notes) return;
    let stop = false;
    const todo = notes.filter((n) => !n.description).slice(0, ENRICH_MAX);
    let i = 0;
    const worker = async () => {
      while (!stop && i < todo.length) {
        const n = todo[i++];
        try {
          const r = await fetch(`/api/memory/note/${encodeURIComponent(n.name)}`, { cache: "no-store" });
          if (!r.ok) continue;
          const d: { body: string | null } = await r.json();
          if (stop || typeof d.body !== "string") continue;
          const x = readable(n.name, d.body, n.title);
          setOwn((cur) => ({ ...cur, [n.name]: { title: x.title, start: opening(x.text) } }));
        } catch { /* the name and the description stay */ }
      }
    };
    for (let k = 0; k < 3; k++) void worker();
    return () => { stop = true; };
  }, [notes]);
  const titleOf = useCallback((n: Note) => n.title || own[n.name]?.title || noteTitle(n.name), [own]);

  const load = useCallback(() => {
    setFailed(false);
    fetch("/api/memory/notes", { cache: "no-store" })
      .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then((rows: Note[]) => setNotes([...rows].sort((a, b) => (b.mtime || 0) - (a.mtime || 0))))
      .catch(() => setFailed(true));
  }, []);
  useEffect(load, [load]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!notes || !needle) return notes;
    return notes.filter((n) => `${titleOf(n)} ${noteTitle(n.name)} ${n.description || ""}`.toLowerCase().includes(needle));
  }, [notes, q, titleOf]);

  if (open) return <NoteView note={open} onBack={() => setOpen(null)} />;

  return (
    <div className="sk-scroll"><div className="sk-page sk-page-wide">
      <div><h1 className="sk-h1">{t("mem.title")}</h1><p className="sk-lede">{t("mem.lede")}</p></div>
      {failed ? (
        <div className="sk-empty" role="alert"><Icon name="alert" /><p>{t("mem.failed")}</p><button className="sk-btn" onClick={load}>{t("retry")}</button></div>
      ) : notes === null ? (
        <div className="sk-list" aria-busy="true" aria-label={t("loading")}><div className="sk-skel" /><div className="sk-skel" /></div>
      ) : notes.length === 0 ? (
        <div className="sk-empty"><Icon name="memory" /><p>{t("mem.empty")}</p></div>
      ) : (
        <>
          <div className="sk-row sk-mem-tools">
            <label className="sk-grow sk-mem-search">
              <span className="sk-visually-hidden">{t("mem.search")}</span>
              <input className="sk-input" type="search" value={q} placeholder={t("mem.search")} onChange={(e) => setQ(e.target.value)} />
            </label>
            <span className="sk-small sk-muted" role="status">{t(notes.length > 1 ? "mem.count.n" : "mem.count", { n: notes.length })}</span>
          </div>
          {shown && shown.length === 0 ? <p className="sk-muted">{t("mem.none")}</p> : (
            <ul className="sk-list sk-plain-list">
              {(shown || []).map((n) => (
                <li key={n.name}>
                  <button className="sk-card sk-card-btn" onClick={() => setOpen(n)}>
                    <span className="sk-card-title">{titleOf(n)}</span>
                    {(n.description || own[n.name]?.start) && <span className="sk-mem-excerpt">{n.description || own[n.name]?.start}</span>}
                    {n.mtime ? <span className="sk-small sk-muted">{t("mem.updated", { when: when(t, n.mtime, lang) })}</span> : null}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div></div>
  );
}

function NoteView({ note, onBack }: { note: Note; onBack: () => void }) {
  const t = useT();
  const { lang } = useUI();
  const [body, setBody] = useState<string | null | undefined>(undefined);   // undefined = loading · null = not readable
  const load = useCallback(() => {
    setBody(undefined);
    fetch(`/api/memory/note/${encodeURIComponent(note.name)}`, { cache: "no-store" })
      .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then((d: { body: string | null }) => setBody(typeof d.body === "string" ? d.body : null))
      .catch(() => setBody(null));
  }, [note.name]);
  useEffect(load, [load]);
  const r = typeof body === "string" ? readable(note.name, body, note.title) : null;
  return (
    <div className="sk-scroll"><div className="sk-page">
      <div><button className="sk-btn sk-btn-quiet" onClick={onBack}><Icon name="back" />{t("mem.back")}</button></div>
      <article className="sk-card sk-mem-note" aria-busy={body === undefined}>
        <h1 className="sk-h2">{r?.title || note.title || noteTitle(note.name)}</h1>
        <p className="sk-small sk-muted">
          {note.mtime ? `${t("mem.updated", { when: when(t, note.mtime, lang) })} · ` : ""}{t("mem.readonly")}
        </p>
        {body === undefined ? <div className="sk-skel" aria-label={t("loading")} />
          : r ? (r.text ? <Markdown text={r.text} /> : <p className="sk-muted">{note.description}</p>)
          : <div className="sk-empty" role="alert"><Icon name="alert" /><p>{t("mem.note.failed")}</p><button className="sk-btn" onClick={load}>{t("retry")}</button></div>}
      </article>
    </div></div>
  );
}
