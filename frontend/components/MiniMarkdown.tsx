"use client";
import { Fragment, type ReactNode } from "react";

// 3.4 — the little Markdown a model writes (headings, lists, **bold**, *italic*, `code`),
// rendered as React elements: no HTML is ever injected. Nina's bubbles showed raw `**`
// and `*` (manager journey of 08.10); the morning brief is Markdown too.

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\s][^*\n]*\*|_[^_\s][^_\n]*_)/g;

export function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let k = 0;
  for (const m of text.matchAll(INLINE)) {
    const i = m.index ?? 0;
    if (i > last) out.push(text.slice(last, i));
    const t = m[0];
    if (t.startsWith("**")) out.push(<strong key={k++} className="font-semibold text-slate-100">{t.slice(2, -2)}</strong>);
    else if (t.startsWith("`")) out.push(<code key={k++} className="rounded bg-black/30 px-1 text-[0.92em]">{t.slice(1, -1)}</code>);
    else out.push(<em key={k++}>{t.slice(1, -1)}</em>);
    last = i + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export default function MiniMarkdown({ text, className = "" }: { text: string; className?: string }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let para: string[] = [];
  let k = 0;
  const flushPara = () => {
    if (para.length) blocks.push(<p key={k++} className="whitespace-pre-wrap">{inline(para.join("\n"))}</p>);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const items = list.items.map((it, i) => <li key={i}>{inline(it)}</li>);
    blocks.push(list.ordered
      ? <ol key={k++} className="list-decimal space-y-0.5 pl-5">{items}</ol>
      : <ul key={k++} className="list-disc space-y-0.5 pl-5">{items}</ul>);
    list = null;
  };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    const li = /^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/.exec(line);
    if (h) {
      flushPara(); flushList();
      const cls = h[1].length === 1 ? "text-[14px] font-semibold text-slate-100" : "mt-1 text-[12.5px] font-semibold uppercase tracking-wide text-mut";
      blocks.push(<div key={k++} className={cls}>{inline(h[2])}</div>);
    } else if (li) {
      flushPara();
      const ordered = !!li[1];
      if (list && list.ordered !== ordered) flushList();
      if (!list) list = { ordered, items: [] };
      list.items.push(li[2]);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList();
      para.push(line);
    }
  }
  flushPara(); flushList();
  return <div className={`space-y-1.5 ${className}`}>{blocks.map((b, i) => <Fragment key={i}>{b}</Fragment>)}</div>;
}
