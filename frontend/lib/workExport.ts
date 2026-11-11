"use client";
// Feature `work_export` — « Export our work » (backend/work_export_api.py): one zip of the
// notes, the tools and the deliverables the caller may read, for the instance's admins.
// The route 404s while the feature is off; the UI asks `useFeatureOn("work_export")` first.
import type { Lang } from "./studyText";

export const WORK_EXPORT_URL = "/api/admin/work-export";

export interface WorkExportResult { filename: string; projects: number; notIncluded: number }

/** Fetch the archive, hand it to the browser as a download, and say what it holds: the
 *  screen can then tell the person that something happened (a plain link said nothing, and a
 *  refusal opened a page of JSON). Throws the HTTP status (0 = no answer). */
export async function downloadWorkExport(): Promise<WorkExportResult> {
  let r: Response;
  try { r = await fetch(WORK_EXPORT_URL, { cache: "no-store" }); } catch { throw new Error("0"); }
  if (!r.ok) throw new Error(String(r.status));
  const blob = await r.blob();
  const filename = /filename="([^"]+)"/.exec(r.headers.get("content-disposition") || "")?.[1] || "sokkan-work-export.zip";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.style.display = "none";
  document.body.appendChild(a); a.click(); a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  const n = (h: string) => { const v = parseInt(r.headers.get(h) || "", 10); return Number.isFinite(v) && v >= 0 ? v : 0; };
  return { filename, projects: n("x-sokkan-export-projects"), notIncluded: n("x-sokkan-export-projects-not-included") };
}

interface Text { title: string; help: string; button: string; note: string; preparing: string;
  done: (file: string, projects: number) => string; missing: (n: number) => string; failed: string }

export const WORK_EXPORT_TEXT: Record<Lang, Text> = {
  en: {
    title: "Export our work",
    help: "Download what your organisation built here as one zip, readable without SOKKAN: the memory notes (Markdown), "
      + "the tools (agents, project instructions, skills, declared connectors) and the deliverables, with a README and a manifest.",
    button: "Download the archive (.zip)",
    note: "It holds what you may read: the projects you have a role in, notes up to your clearance. No secret is in it — "
      + "the vault and every credential stay here. Each export is written to the journal.",
    preparing: "Preparing the archive…",
    done: (file, projects) => `Archive downloaded: ${file} (${projects} ${projects === 1 ? "project" : "projects"}).`,
    missing: (n) => `${n} ${n === 1 ? "project is" : "projects are"} not in the archive: you have no role in ${n === 1 ? "it" : "them"}. `
      + `To include ${n === 1 ? "it" : "them"}, give yourself a role there (Setup › Projects & teams), then export again.`,
    failed: "The export did not go through and nothing was downloaded. Try again in a moment.",
  },
  fr: {
    title: "Exporter notre travail",
    help: "Téléchargez ce que votre organisation a construit ici en un seul zip, lisible sans SOKKAN : les notes de la mémoire (Markdown), "
      + "les outils (agents, instructions de projet, skills, connecteurs déclarés) et les livrables, avec un README et un manifeste.",
    button: "Télécharger l'archive (.zip)",
    note: "Elle contient ce que vous avez le droit de lire : les projets où vous avez un rôle, les notes jusqu'à votre niveau d'habilitation. "
      + "Aucun secret n'y figure — le coffre et tous les identifiants restent ici. Chaque export est inscrit au journal.",
    preparing: "Préparation de l'archive…",
    done: (file, projects) => `Archive téléchargée : ${file} (${projects} ${projects === 1 ? "projet" : "projets"}).`,
    missing: (n) => (n === 1
      ? "1 projet ne figure pas dans l'archive : vous n'y avez pas de rôle. Pour l'inclure, donnez-vous un rôle dans ce projet (Setup › Projects & teams), puis exportez à nouveau."
      : `${n} projets ne figurent pas dans l'archive : vous n'y avez pas de rôle. Pour les inclure, donnez-vous un rôle dans ces projets (Setup › Projects & teams), puis exportez à nouveau.`),
    failed: "L'export n'a pas abouti et rien n'a été téléchargé. Réessayez dans un instant.",
  },
};
