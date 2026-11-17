// 4.0 (audit coworker) — an approval card is never « the agent wants to carry out an action »
// with nothing else: every kind the backend sends (backend/approvalcard.py) has its headline and
// what « Allow » does, in French and in English, with the card's title in it. Run by
// tests/test_nav_planes.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fr, en } from "../../frontend/lib/simple/dict.ts";
import { APPROVAL_KINDS } from "../../frontend/lib/simple/threadModel.ts";

const dicts = { fr, en } as Record<string, Record<string, string>>;

test("every card kind has a headline and a consequence in both languages", () => {
  for (const [lang, d] of Object.entries(dicts)) {
    for (const k of APPROVAL_KINDS) {
      assert.ok(d[`ap.${k}`], `${lang}: ap.${k}`);
      assert.ok(d[`ap.yes.${k}`], `${lang}: ap.yes.${k}`);
      if (k !== "run_command" && k !== "delegate") assert.match(d[`ap.${k}`], /\{title\}/, `${lang}: ap.${k} names it`);
    }
    for (const k of ["ap.if.yes", "ap.if.no", "ap.no.default", "ap.no.write_draft", "ap.f.where", "ap.diff.del", "ap.diff.add", "ap.diff.new"]) assert.ok(d[k], `${lang}: ${k}`);
  }
});

test("no sentence of a card says « an action » without saying which", () => {
  assert.doesNotMatch(fr["ap.other"], /effectuer une action$/);
  assert.doesNotMatch(en["ap.other"], /carry out an action$/);
});

test("the French cards say « vous », never « tu »", () => {
  for (const [k, v] of Object.entries(fr)) {
    if (!k.startsWith("ap.")) continue;
    assert.doesNotMatch(v, /(?<![\p{L}'’])(tu|ton|ta|tes|toi)(?!\p{L})/iu, k);
  }
});
