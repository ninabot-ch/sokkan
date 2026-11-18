// 4.0.1 — the journal of validations in words (pure, tested by tests/nav/validations_test.ts).
import type { OKey } from "./dictOnboarding";

/** 4.0.1: every decision the journal writes, in words. A kind this page does not know reads
 * « Inconnu », never « Accepté » (a refusal of the quality check once read as accepted). */
const KNOWN_DECISIONS = ["validate", "allow", "auto_allow", "edit", "deny", "auto_deny", "answer", "pending",
  "mode_change", "cancelled", "interrupted", "claude", "anonymize", "local", "declined"] as const;
export function decisionKey(d: string): OKey {
  return `va.d.${(KNOWN_DECISIONS as readonly string[]).includes(d) ? d : "unknown"}` as OKey;
}
export type Tone = "ok" | "refused" | "neutral" | "unknown";
export function decisionTone(d: string): Tone {
  if (["deny", "auto_deny", "declined"].includes(d)) return "refused";
  if (["validate", "allow", "auto_allow", "edit", "answer", "claude", "anonymize", "local"].includes(d)) return "ok";
  if (["pending", "mode_change", "cancelled", "interrupted"].includes(d)) return "neutral";
  return "unknown";
}
/** Why nobody clicked, in words (`policy:quality_gate` → « par la vérification de qualité »). */
export function reasonKey(r?: string): OKey | "" {
  if (!r) return "";
  if (r === "policy:quality_gate") return "va.r.quality";
  if (r === "policy:simple_same_document_same_turn") return "va.r.same_doc";
  if (r.startsWith("mode:")) return "va.r.mode";
  if (r === "sandbox") return "va.r.sandbox";
  if (r.startsWith("policy:")) return "va.r.rule";
  return "";
}
/** Last resort when the server sent no `label`: never a server path or a raw tool name. */
export function readableTitle(s: string): string {
  if (!s) return "";
  if (s.startsWith("mcp__") || /^[a-z0-9-]+__[a-z0-9_]+$/i.test(s)) {
    const n = s.split("__").pop() || s;
    return n.replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase());
  }
  return s.replace(/(?:\/[^\s/]+)+\/([^\s/]+)/g, "$1").replace(/\.(md|markdown|txt)\b/gi, "");
}
