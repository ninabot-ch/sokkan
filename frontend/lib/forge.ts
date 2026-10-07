// 3.2 lot 5 — linked forge accounts (GitLab) and a forge project's repositories.
// No route ever returns a token: the cockpit only sees who is linked, scopes and dates.

export interface ForgeLink {
  provider: string; base_url: string; forge_user_id: string; forge_username: string; scopes: string;
  expires_at: number | null; linked_at: number; last_refresh_at: number | null; revoked_at: number | null;
  state: "active" | "expired" | "revoked";
}
export interface ForgeProjectAccess {
  slug: string; name: string; role: string | null; computed_at: number | null; expires_at: number | null;
  repos: ForgeRepo[];
}
export interface ForgeRepo { provider: string; base_url: string; repo_path: string }
export interface ForgeStatus {
  enabled: boolean;
  providers: { provider: string; base_url: string; configured: boolean; scopes: string[]; redirect_uri: string }[];
}

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method, cache: "no-store",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

export const forgeStatus = () => call<ForgeStatus>("/api/forge/status");
export const forgeLinks = () => call<{ links: ForgeLink[]; projects: ForgeProjectAccess[] }>("/api/forge/links");
export const forgeUnlink = (provider: string) => call<{ ok: boolean; sessions_closed: number }>(`/api/forge/links/${provider}`, "DELETE");
export const forgeRefresh = () => call<{ projects: Record<string, string | null> }>("/api/forge/refresh", "POST");
/** full-page navigation: the OAuth consent happens on GitLab */
export const forgeLinkUrl = (provider: string) => `/api/forge/${provider}/link`;

export const adminRepos = (slug: string) => call<{ repos: ForgeRepo[] }>(`/api/admin/projects/${slug}/repos`);
export const adminAddRepo = (slug: string, repo_path: string, base_url = "") =>
  call<{ repos: ForgeRepo[] }>(`/api/admin/projects/${slug}/repos`, "POST", { provider: "gitlab", repo_path, base_url });
export const adminRemoveRepo = (slug: string, r: ForgeRepo) =>
  call<{ repos: ForgeRepo[] }>(`/api/admin/projects/${slug}/repos`, "DELETE", r);
