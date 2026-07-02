"use client";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Message } from "@/lib/types";
import ToolCard from "./ToolCard";

function Thinking({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="my-1">
      <button
        onClick={() => setOpen(!open)}
        className="text-[11.5px] italic text-mut hover:text-slate-300"
      >
        💭 réflexion {open ? "▾" : "▸"}
      </button>
      {open && (
        <div className="mt-1 whitespace-pre-wrap border-l-2 border-line pl-3 text-[12px] italic text-mut">
          {text}
        </div>
      )}
    </div>
  );
}

export default function ChatMessage({ m }: { m: Message }) {
  if (m.kind === "tool") return <ToolCard m={m} />;
  if (m.kind === "thinking") return <Thinking text={m.text || ""} />;

  if (m.kind === "chip")
    return (
      <div className="my-2 text-center">
        <span className="rounded-full border border-line bg-panel2 px-2 py-0.5 text-[11px] text-mut">
          {m.text}
        </span>
      </div>
    );

  if (m.kind === "note")
    return (
      <div className="my-1 rounded border border-line/60 bg-panel2/40 px-2 py-1 text-[11.5px] text-mut">
        {m.text}
      </div>
    );

  if (m.kind === "tool_result_orphan")
    return (
      <pre className={`my-1 overflow-x-auto rounded bg-[#0b0f16] p-2 text-[11.5px] ${m.is_error ? "text-red-300" : "text-mut"}`}>
        {m.text}
      </pre>
    );

  // text — user vs assistant
  if (m.role === "user")
    return (
      <div className="my-2 flex justify-end">
        <div className="max-w-[88%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-sea/15 px-3 py-2 text-[13px] text-slate-100 ring-1 ring-sea/30">
          {m.text}
        </div>
      </div>
    );

  return (
    <div className="my-2 md text-slate-200">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text || ""}</ReactMarkdown>
    </div>
  );
}
