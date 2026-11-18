// 4.0 connectors (feature `connectors`, backend/connectors/api.py): the person's connections and
// the approval cards. Server-side gated: every route 404s when the feature is off.
import { ApiError } from "./api";

export type Provider = "graph" | "imap" | "caldav" | "webdav" | "folder" | "ics" | "demo";
export interface FamilyEntry { provider: Provider; enabled: boolean; connected: boolean; login: string }
export interface ConnectorsMe {
  families: Record<"mail" | "calendar" | "files", FamilyEntry[]>;
  content_allowed: boolean; reason: string; zone: string; allow_send: boolean; pending_actions: number;
}
export interface ConnectorsSettings {
  providers: Record<string, Record<string, string | number | boolean>>;
  allow_send: boolean; graph_client_secret_set: boolean;
  scopes: Record<string, { shared_mailboxes: string[]; sites: string[]; folders: string[] }>;
}
export interface ConnectorsOverview { me: ConnectorsMe; is_admin: boolean; settings?: ConnectorsSettings;
  accounts: { provider: Provider; login: string; consented_at: number }[] }
export interface ConnectorAction {
  id: string; kind: "mail.send" | "calendar.create" | "files.write"; provider: string; title: string;
  session_id?: string; project?: string;
  status: string; created_at: number; expires_at: number; result: string;
  preview: Record<string, unknown>;
}

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, { method, cache: "no-store",
    headers: body ? { "content-type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let code = "", message = `${r.status}`;
    try {
      const d = (await r.json()).detail;
      if (d && typeof d === "object") { code = d.code || ""; message = d.message || message; } else if (typeof d === "string") message = d;
    } catch { /* not JSON */ }
    throw new ApiError(r.status, code, message);
  }
  return r.json();
}

export const fetchConnectors = () => call<ConnectorsOverview>("/api/connectors");
export const saveConnectorsSettings = (body: Partial<ConnectorsSettings> & { graph_client_secret?: string }) =>
  call<ConnectorsSettings>("/api/connectors/settings", "PUT", body);
export const connectLogin = (provider: Provider, login: string, password: string) =>
  call<{ provider: string; login: string }>(`/api/connectors/${provider}/connect`, "POST", { login, password });
export const startGraph = () => call<{ url: string }>("/api/connectors/graph/start");
export const revokeConnector = (provider: Provider) => call<{ revoked: boolean }>(`/api/connectors/${provider}`, "DELETE");
export const fetchActions = (status = "pending") =>
  call<{ actions: ConnectorAction[] }>(`/api/connectors/actions?status=${encodeURIComponent(status)}`);
export const decideAction = (id: string, approve: boolean, reason = "") =>
  call<ConnectorAction>(`/api/connectors/actions/${encodeURIComponent(id)}/${approve ? "approve" : "reject"}`, "POST",
    approve ? undefined : { reason });
