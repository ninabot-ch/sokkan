"use client";
// 3.4 « classification » — levels, my clearance in the selected project, admin mapping.
import { useEffect, useState } from "react";

export interface Level { rank: number; id: string; label: string }
export interface ClassificationInfo {
  enabled: boolean;
  scale: Level[];
  default: string;
  project?: string;
  clearance?: string | null;
  can_audit?: boolean;
}
export interface GroupLevel { team_id: string; project: string; level: number; level_id: string; created_by: string }
export interface ClassificationAdmin extends ClassificationInfo {
  groups: GroupLevel[];
  roles: Record<string, number>;
  teams: { id: string; name: string; members: number }[];
  projects: string[];
}

async function call<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method, credentials: "same-origin",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let d = `${r.status}`;
    try { d = (await r.json()).detail || d; } catch { /* not json */ }
    throw new Error(d);
  }
  return r.json() as Promise<T>;
}

export const fetchClassification = () => call<ClassificationInfo>("/api/classification");
export const setNoteLevel = (name: string, level: string, reason = "") =>
  call(`/api/memory/note/${encodeURIComponent(name)}/level`, "POST", { level, reason });
export const setCardLevel = (id: number, level: string, reason = "") =>
  call(`/api/board/card/${id}/level`, "POST", { level, reason });
export const adminClassification = () => call<ClassificationAdmin>("/api/admin/classification");
export const adminSetGroupLevel = (team: string, level: string, project = "*") =>
  call("/api/admin/classification/groups", "PUT", { team, level, project });
export const adminDeleteGroupLevel = (team: string, project = "*") =>
  call(`/api/admin/classification/groups?team=${encodeURIComponent(team)}&project=${encodeURIComponent(project)}`, "DELETE");
export const adminSetRoleLevel = (role: string, level: string) =>
  call("/api/admin/classification/roles", "PUT", { role, level });
export const auditCsvUrl = (days = 30, actor = "", note = "") =>
  `/api/classification/audit?format=csv&days=${days}${actor ? `&actor=${encodeURIComponent(actor)}` : ""}${note ? `&note=${encodeURIComponent(note)}` : ""}`;
export interface AccessRow { at: string; actor: string | null; actor_source?: string; via: string; note_name: string; level: string; session_id?: string | null; query?: string | null }
/** 3.4 — the audited recall, consultable: who obtained which note, through which path. */
export const fetchAudit = (days = 30, actor = "", note = "") =>
  call<{ project: string; entries: AccessRow[]; hidden_above_clearance: number; clearance: string | null }>(
    `/api/classification/audit?days=${days}&limit=500${actor ? `&actor=${encodeURIComponent(actor)}` : ""}${note ? `&note=${encodeURIComponent(note)}` : ""}`);

let cache: Promise<ClassificationInfo> | null = null;

/** The scale and my clearance; `enabled: false` (and no badge anywhere) when the feature is off. */
export function useClassification(): ClassificationInfo | null {
  const [d, setD] = useState<ClassificationInfo | null>(null);
  useEffect(() => {
    cache = cache || fetchClassification().catch(() => ({ enabled: false, scale: [], default: "project" }));
    cache.then(setD);
  }, []);
  return d;
}

/** Label of a level (rank or id) in the customer's grid. */
export function levelLabel(info: ClassificationInfo | null, level: number | string | undefined | null): string {
  if (level === undefined || level === null || !info) return "";
  const l = info.scale.find((x) => x.rank === level || x.id === level);
  return l ? l.label : String(level);
}

export function levelRank(info: ClassificationInfo | null, level: number | string | undefined | null): number {
  if (level === undefined || level === null || !info) return 2;
  const l = info.scale.find((x) => x.rank === level || x.id === level);
  return l ? l.rank : 2;
}
