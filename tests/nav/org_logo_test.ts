// 4.0.1 — the logo URL changes with the logo itself (cache), never NaN.
import { test } from "node:test";
import assert from "node:assert/strict";
import { logoUrl } from "../../frontend/lib/simple/orgLogo.ts";

test("the logo URL is versioned by the logo's date", () => {
  assert.equal(logoUrl(1731916800.75), "/api/org/logo?v=1731916801");
  assert.equal(logoUrl(undefined), "/api/org/logo?v=0");
  assert.equal(logoUrl(Number.NaN), "/api/org/logo?v=0");
});
