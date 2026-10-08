// 3.2.2 — the plane a person was on last, kept per user by the API (/api/me/nav): an admin
// lands there next time, on any browser.
export async function navLast(): Promise<string | null> {
  try {
    const r = await fetch("/api/me/nav", { cache: "no-store" });
    if (!r.ok) return null;
    return ((await r.json()) as { last_plane?: string | null }).last_plane ?? null;
  } catch { return null; }
}

export function navRemember(plane: string): void {
  fetch("/api/me/nav", {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ last_plane: plane }),
  }).catch(() => {});
}
