"use client";
// Feature `work_export` — « Export our work » (backend/work_export_api.py): one zip of the
// notes, the tools and the deliverables the caller may read, for the instance's admins.
// The route 404s while the feature is off; the UI asks `useFeatureOn("work_export")` first.
import type { Lang } from "./studyText";

export const WORK_EXPORT_URL = "/api/admin/work-export";

export const WORK_EXPORT_TEXT: Record<Lang, { title: string; help: string; button: string; note: string }> = {
  en: {
    title: "Export our work",
    help: "Download what your organisation built here as one zip, readable without SOKKAN: the memory notes (Markdown), "
      + "the tools (agents, project instructions, skills, declared connectors) and the deliverables, with a README and a manifest.",
    button: "Download the archive (.zip)",
    note: "It holds what you may read: the projects you have a role in, notes up to your clearance. No secret is in it — "
      + "the vault and every credential stay here. Each export is written to the journal.",
  },
  fr: {
    title: "Exporter notre travail",
    help: "Téléchargez ce que votre organisation a construit ici en un seul zip, lisible sans SOKKAN : les notes de la mémoire (Markdown), "
      + "les outils (agents, instructions de projet, skills, connecteurs déclarés) et les livrables, avec un README et un manifeste.",
    button: "Télécharger l'archive (.zip)",
    note: "Elle contient ce que vous avez le droit de lire : les projets où vous avez un rôle, les notes jusqu'à votre niveau d'habilitation. "
      + "Aucun secret n'y figure — le coffre et tous les identifiants restent ici. Chaque export est inscrit au journal.",
  },
};
