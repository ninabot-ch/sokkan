"use client";
// 3.2 lot 8 — hooks-only sandbox: Bash is refused outside `default`, never asked to a
// human (an unconfined Bash could read the other projects). Say it where people look
// (session pane, agent popout) with the way out, instead of a silent refusal.
import { useFeatures } from "@/lib/features";
import { currentProject } from "@/lib/project";

export const BASH_OFF = "Bash is disabled in this project: sandbox is hooks-only. Enable bubblewrap or "
  + "the Kubernetes runner, or switch the project to default. Admin: SOKKAN_FEATURE_SANDBOX / "
  + "docs/enterprise/OPERATIONS.md § 4.1";

/** True when the project in view runs under a hooks-only sandbox (no Bash). */
export function useBashOff(project?: string): boolean {
  const f = useFeatures();
  return f.sandbox === "hooks-only" && (project ?? currentProject()) !== "default";
}

export default function SandboxNotice({ message = BASH_OFF, className = "" }: { message?: string; className?: string }) {
  return (
    <div role="note" className={`flex items-start gap-2 border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[11.5px] leading-snug text-amber-100 ${className}`}>
      <span aria-hidden>⛔</span>
      <span><span className="sr-only">Warning: </span>{message}</span>
    </div>
  );
}
