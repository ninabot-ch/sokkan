// SOKKAN Atelier 4.0 — job kits (« Postes »): the pure part of the home and the picker,
// tested by tests/nav/simple_roles_test.ts. Nothing here talks to the server.
import type { Job, TaskTemplate } from "./api";

export interface TemplateGroup { role: Job | null; templates: TaskTemplate[] }

/** The home's templates, one group per job in the order the server gave (the project's own
 *  job first), then the general templates; a job without a template in the list is skipped. */
export function groupTemplates(playbooks: TaskTemplate[], roles: Job[] = []): TemplateGroup[] {
  const out: TemplateGroup[] = [];
  for (const r of roles) {
    const ts = playbooks.filter((p) => p.role === r.id);
    if (ts.length) out.push({ role: r, templates: ts });
  }
  const known = new Set(roles.map((r) => r.id));
  const general = playbooks.filter((p) => !p.role || !known.has(p.role));
  if (general.length) out.push({ role: null, templates: general });
  return out;
}

/** The picker's next selection after a click on a job (several jobs possible). */
export function toggle(selected: string[], id: string): string[] {
  return selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
}

/** What a save of the picker does, in order: the jobs, then one project per job when the
 *  person may create projects and asked for it, else the single job in the current project
 *  when they administer it and asked for it. */
export function setupPlan(selected: string[], opts: { create: boolean; canCreate: boolean;
  here: boolean; canSetupHere: boolean; project: string; already: string[] }): { rid: string; project: string }[] {
  const fresh = selected.filter((r) => !opts.already.includes(r));
  if (opts.create && opts.canCreate) return fresh.map((rid) => ({ rid, project: "" }));
  if (opts.here && opts.canSetupHere && selected.length === 1) return [{ rid: selected[0], project: opts.project }];
  return [];
}
