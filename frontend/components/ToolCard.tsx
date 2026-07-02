"use client";
import { useState } from "react";
import type { Message } from "@/lib/types";

const ICON: Record<string, string> = {
  Bash: "$_", Read: "📄", Edit: "✏️", Write: "🖊️", Glob: "🔎", Grep: "🔍",
  Task: "🤖", Agent: "🤖", WebFetch: "🌐", WebSearch: "🌐", Skill: "⚡",
};

function primaryInput(m: Message): string | null {
  const i = m.input || {};
  if (m.tool === "Bash") return (i.command as string) || null;
  if (m.tool === "Edit") return (i.new_string as string) ? `${i.file_path}\n--- ${i.old_string}\n+++ ${i.new_string}` : (i.file_path as string);
  if (m.tool === "Write") return (i.content as string) || (i.file_path as string);
  return null;
}

export default function ToolCard({ m }: { m: Message }) {
  const [open, setOpen] = useState(false);
  const res = m.result;
  const err = res?.is_error;
  const dot = err ? "bg-red-500" : res ? "bg-emerald-500" : "bg-amber-400 animate-pulse";
  const body = primaryInput(m);

  return (
    <div className="my-1 rounded-lg border border-line bg-panel2/60">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12.5px]"
      >
        <span className={`h-2 w-2 shrink-0 rounded-full ${dot}`} />
        <span className="font-mono text-mut">{ICON[m.tool || ""] || "🔧"}</span>
        <span className="font-medium text-slate-200">{m.tool}</span>
        <span className="truncate text-mut">{m.title}</span>
        <span className="ml-auto text-[11px] text-mut">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="border-t border-line px-2.5 py-2 text-[12px]">
          {body && (
            <pre className="mb-2 overflow-x-auto rounded bg-[#0b0f16] p-2 font-mono text-[11.5px] text-slate-300">
              {body.length > 4000 ? body.slice(0, 4000) + "\n…" : body}
            </pre>
          )}
          {res && (
            <pre
              className={`overflow-x-auto rounded bg-[#0b0f16] p-2 font-mono text-[11.5px] ${
                err ? "text-red-300" : "text-mut"
              }`}
            >
              {res.text || "(vide)"}
              {res.truncated ? "\n… (tronqué)" : ""}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
