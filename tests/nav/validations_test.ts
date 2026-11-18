// 4.0.1 — the journal of validations never lies: every decision kind in words, an unknown one
// reads « Inconnu » (never « Accepté »), no server path or raw tool name on screen.
import { test } from "node:test";
import assert from "node:assert/strict";
import { decisionKey, decisionTone, readableTitle, reasonKey } from "../../frontend/lib/simple/validations.ts";
import { readFileSync } from "node:fs";

// the dictionary imports React: read its two tables as text (one `"key": "sentence",` per line)
const src = readFileSync(new URL("../../frontend/lib/simple/dictOnboarding.ts", import.meta.url), "utf8");
const cut = src.indexOf("export const obEn");
const table = (part: string) => Object.fromEntries([...part.matchAll(/^\s+"([\w.]+)": "(.*)",$/gm)].map((m) => [m[1], m[2]]));
const OB_FR: Record<string, string> = table(src.slice(0, cut));
const OB_EN: Record<string, string> = table(src.slice(cut));

test("a refusal of the quality check reads as refused, never accepted", () => {
  assert.equal(decisionKey("auto_deny"), "va.d.auto_deny");
  assert.equal(decisionTone("auto_deny"), "refused");
  assert.equal(OB_FR["va.d.auto_deny"], "Refusé");
  assert.equal(reasonKey("policy:quality_gate"), "va.r.quality");
});

test("every decision the journal writes has words in both languages", () => {
  for (const d of ["validate", "allow", "auto_allow", "edit", "deny", "auto_deny", "answer", "pending",
    "mode_change", "cancelled", "interrupted", "claude", "anonymize", "local", "declined"]) {
    const k = decisionKey(d);
    assert.notEqual(k, "va.d.unknown", d);
    assert.ok(OB_FR[k] && OB_EN[k], d);
  }
});

test("an unknown decision reads « Inconnu », in a neutral pill", () => {
  assert.equal(decisionKey("something_new"), "va.d.unknown");
  assert.equal(decisionTone("something_new"), "unknown");
  assert.equal(OB_FR["va.d.unknown"], "Inconnu");
  assert.notEqual(OB_FR[decisionKey("")], "Accepté");
});

test("no server path nor raw tool name on screen", () => {
  assert.equal(readableTitle("mcp__sokkan-connectors__mail_draft"), "Mail draft");
  assert.equal(readableTitle("/data/projects/clients/work/deliverables/673d3a/offre-pre-vert.md"), "offre-pre-vert");
  assert.equal(readableTitle("Synthèse.md"), "Synthèse");
  assert.equal(reasonKey("mode:bypassPermissions"), "va.r.mode");
  assert.equal(reasonKey("policy:other"), "va.r.rule");
});
