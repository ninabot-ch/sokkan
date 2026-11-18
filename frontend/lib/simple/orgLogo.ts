// 4.0.1 — the organisation's logo URL, versioned by the file's own date (a new logo is never
// served from the browser cache of the old one). Pure: tests/nav/org_logo_test.ts.
export function logoUrl(v?: number | null): string {
  return `/api/org/logo?v=${Math.round(Number(v) || 0)}`;
}
