// 3.2 multi-user — the project the cockpit works in.
// Chosen in the header selector, kept per browser (localStorage) and in the URL
// (?project=<slug>, wins over the stored one), sent with EVERY /api call as the
// `x-sokkan-project` header: the backend (projectgate) answers with that project's data
// and the person's role in it. Switching project reloads the page — every tab refetches
// in the new project, nothing of the previous one stays on screen.

const KEY = "sokkan_project";
export const HEADER = "x-sokkan-project";
const SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;

export function currentProject(): string {
  if (typeof window === "undefined") return "default";
  const q = new URLSearchParams(window.location.search).get("project");
  if (q && SLUG.test(q)) return q;
  try {
    const s = localStorage.getItem(KEY);
    if (s && SLUG.test(s)) return s;
  } catch { /* private mode */ }
  return "default";
}

export function switchProject(slug: string): void {
  if (!SLUG.test(slug)) return;
  try { localStorage.setItem(KEY, slug); } catch { /* private mode */ }
  const url = new URL(window.location.href);
  url.searchParams.set("project", slug);
  // the deep-link targets of another project make no sense here
  for (const k of ["agent", "run", "chat", "incident", "note", "proposal"]) url.searchParams.delete(k);
  window.location.assign(url.toString());
}

let installed = false;

/** Adds the project header to every same-origin /api request (fetch). Idempotent. */
export function installProjectFetch(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  const orig = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    let path = url;
    try { path = new URL(url, window.location.href).pathname; } catch { /* relative */ }
    if (path.startsWith("/api/")) {
      const headers = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined));
      if (!headers.has(HEADER)) headers.set(HEADER, currentProject());
      return orig(input, { ...init, headers });
    }
    return orig(input, init);
  };
}
