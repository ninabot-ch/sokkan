"use client";
// Feature `study_mode` — every sentence of the banner and of Setup › Study, in French and in
// English. The cockpit has no translation layer yet: this file is self-contained (language =
// the person's choice kept per browser, else the browser's language) and can be folded into
// one when it exists.
//
// THE CONSENT TEXT LIVES HERE (`recorded`, `consent`). Changing what it promises = bump
// CONSENT_VERSION in backend/study.py: everyone is then asked again.
import { useEffect, useState } from "react";

export type Lang = "fr" | "en";
const KEY = "sokkan_lang";

export function useLang(): [Lang, (l: Lang) => void] {
  const [lang, setLang] = useState<Lang>("en");
  useEffect(() => {
    let l: string | null = null;
    try { l = localStorage.getItem(KEY); } catch { /* private mode */ }
    if (l !== "fr" && l !== "en") l = /^fr\b/i.test(navigator.language || "") ? "fr" : "en";
    setLang(l as Lang);
  }, []);
  const set = (l: Lang) => {
    try { localStorage.setItem(KEY, l); } catch { /* private mode */ }
    setLang(l);
  };
  return [lang, set];
}

const fr = {
  title: "Étude",
  banner_on: "Étude {study} en cours",
  banner_until: "jusqu'au {date}",
  banner_as: "vos actions sont enregistrées sous le pseudonyme {p}",
  banner_notice: "La période gratuite se termine dans {n} jour(s)",
  usage_mine: "Votre utilisation du jour",
  usage_mine_deep: "Vos demandes approfondies aujourd'hui",
  banner_ask: "Avant votre première session : lisez ce qui est enregistré et donnez ou refusez votre accord.",
  banner_link: "Ce qui est enregistré",
  banner_answer_one: "1 tâche attend vos deux réponses",
  banner_answer_many: "{n} tâches attendent vos deux réponses",
  banner_decide: "Lire et décider",
  status_title: "L'étude sur cette instance",
  status_off: "Aucune étude n'est active sur cette instance.",
  status_expired: "L'étude {study} est terminée depuis le {date}.",
  status_active: "Étude {study}",
  status_until: "Fin le {date}",
  status_days: "{n} jours restants",
  status_source_gateway: "État lu depuis la passerelle d'inférence",
  status_source_local: "Étude déclarée sur cette instance",
  status_stale: "La passerelle ne répond pas : dernier état connu.",
  status_you: "Vous êtes {p}",
  usage_day: "Utilisation du jour",
  usage_month: "Utilisation du mois",
  usage_deep: "Paliers approfondis : {n} par personne et par jour",
  usage_none: "pas de plafond",
  recorded_title: "Ce qui est enregistré",
  recorded_lead: "Pendant l'étude, SOKKAN garde une trace de la façon dont vous travaillez avec les agents. Voici exactement quoi, et ce que la chercheuse ou le chercheur reçoit.",
  recorded_always_title: "Toujours, sous votre pseudonyme",
  recorded_always: [
    "Le scénario choisi (S1 à S6) et le temps de référence que vous indiquez.",
    "La durée de chaque demande à l'agent, et quel modèle y a répondu.",
    "Chaque validation demandée par l'agent : autorisée, refusée ou corrigée, et le temps que vous avez mis à décider.",
    "Les documents de la mémoire que l'agent a consultés, et leur ancienneté.",
    "Vos deux réponses en fin de tâche.",
  ],
  recorded_consent_title: "Seulement si vous cochez le scénario",
  recorded_consent: [
    "Le texte de vos échanges avec l'agent pour ce scénario : vos demandes, ses réponses, ce qu'il a proposé d'écrire ou de lancer.",
    "Les mots de passe et les clés y sont masqués, les adresses e-mail remplacées.",
  ],
  recorded_never_title: "Jamais",
  recorded_never: [
    "Votre nom et votre adresse e-mail : la table qui relie une personne à son pseudonyme reste sur cette machine et n'est jamais exportée.",
    "Le texte de vos échanges pour un scénario que vous n'avez pas coché : seule sa longueur est notée.",
    "Le texte des fichiers que l'agent lit et le résultat brut de ses outils : seule leur taille est notée, même pour un scénario coché.",
  ],
  recorded_who: "Qui voit quoi : les administrateurs de cette instance voient la table des pseudonymes. Le chercheur ne reçoit que le paquet d'export. Le service d'inférence reçoit votre pseudonyme, pas votre adresse.",
  consent_title: "Votre accord",
  consent_none: "Vous n'avez pas encore répondu.",
  consent_given: "Vous avez donné votre accord le {date}.",
  consent_withdrawn: "Vous avez retiré votre accord le {date}.",
  consent_outdated: "Le texte a changé depuis votre accord : merci de le relire.",
  consent_required: "Tant que l'étude est active, l'usage gratuit de SOKKAN suppose cet accord : sans lui, vos sessions ne démarrent pas.",
  consent_content_label: "J'accepte aussi que le texte de mes échanges soit transmis pour ces scénarios (facultatif) :",
  consent_give: "Je donne mon accord",
  consent_update: "Enregistrer mes choix",
  consent_withdraw: "Retirer mon accord",
  consent_withdraw_confirm: "Retirer votre accord ? Plus rien n'est enregistré pour l'étude, et rien de ce qui a été collecté jusqu'ici n'est transmis.",
  consent_withdraw_note: "Vous pouvez retirer votre accord à tout moment : plus rien n'est collecté, et rien de ce qui l'a été n'est transmis.",
  tasks_title: "Mes tâches de l'étude",
  tasks_none: "Aucune tâche pour l'instant : elles apparaissent ici dès votre première demande à un agent.",
  tasks_scenario: "Scénario",
  tasks_pick: "à choisir",
  tasks_reference: "Temps sans l'agent (min)",
  tasks_reference_help: "Le temps que cette tâche vous prend d'habitude, sans agent.",
  tasks_requests: "{n} demande(s)",
  tasks_untitled: "Sans titre",
  fb_title: "La tâche est terminée ? Deux questions.",
  fb_useful: "L'agent vous a-t-il été utile ?",
  fb_useful_low: "pas du tout",
  fb_useful_high: "beaucoup",
  fb_verify: "Combien de minutes avez-vous passées à vérifier son travail ?",
  fb_send: "Envoyer",
  fb_done: "Répondu : utilité {u}/5, {m} min de vérification",
  fb_edit: "modifier",
  admin_title: "Administration de l'étude",
  admin_single: "La connexion locale ne donne qu'une identité : tous les volontaires partageraient un seul pseudonyme. Donnez un compte à chaque personne avant de commencer.",
  admin_people: "Table de correspondance (reste sur cette instance)",
  admin_people_none: "Personne n'a encore ouvert l'étude.",
  admin_col_pseudo: "Pseudonyme",
  admin_col_person: "Personne",
  admin_col_consent: "Accord",
  admin_col_content: "Contenu partagé",
  admin_consent_given: "donné",
  admin_consent_withdrawn: "retiré",
  admin_consent_none: "pas de réponse",
  admin_export: "Paquet d'export pour le chercheur",
  admin_export_help: "Métadonnées seules par défaut ; le contenu des échanges uniquement pour les scénarios cochés par chaque personne. Aucun nom, aucune adresse.",
  admin_export_zip: "Télécharger le paquet (.zip)",
  admin_export_json: "Voir en JSON",
  admin_tokens: "Jetons d'export en lecture seule",
  admin_tokens_help: "Un jeton ouvre le paquet d'export et rien d'autre. Il s'affiche une seule fois.",
  admin_token_label: "Pour qui (ex. chercheuse, université)",
  admin_token_days: "Validité (jours)",
  admin_token_create: "Créer un jeton",
  admin_token_once: "Copiez ce jeton maintenant, il ne sera plus affiché :",
  admin_token_revoke: "révoquer",
  admin_token_revoked: "révoqué",
  admin_token_none: "Aucun jeton.",
  admin_token_uses: "{n} utilisation(s)",
  admin_curl: "Commande pour le chercheur",
  admin_journal: "Journal des validations (toute l'instance)",
  admin_journal_help: "Chaque approbation demandée et chaque décision, avec le délai et l'origine. Avec les adresses : réservé aux administrateurs.",
  admin_journal_project: "Projet",
  admin_journal_person: "Personne (adresse)",
  admin_journal_since: "Du",
  admin_journal_until: "Au",
  admin_refresh: "Relire l'état",
  admin_exports_last: "Dernier export : {date} ({n} sessions)",
  err: "Action impossible : {e}",
};

const en: typeof fr = {
  title: "Study",
  banner_on: "Study {study} in progress",
  banner_until: "until {date}",
  banner_as: "your actions are recorded under the pseudonym {p}",
  banner_notice: "The free period ends in {n} day(s)",
  usage_mine: "Your use today",
  usage_mine_deep: "Your deep requests today",
  banner_ask: "Before your first session: read what is recorded and give or refuse your consent.",
  banner_link: "What is recorded",
  banner_answer_one: "1 task is waiting for your two answers",
  banner_answer_many: "{n} tasks are waiting for your two answers",
  banner_decide: "Read and decide",
  status_title: "The study on this instance",
  status_off: "No study is active on this instance.",
  status_expired: "The study {study} ended on {date}.",
  status_active: "Study {study}",
  status_until: "Ends on {date}",
  status_days: "{n} days left",
  status_source_gateway: "State read from the inference gateway",
  status_source_local: "Study declared on this instance",
  status_stale: "The gateway does not answer: last known state.",
  status_you: "You are {p}",
  usage_day: "Use today",
  usage_month: "Use this month",
  usage_deep: "Deep tier: {n} per person per day",
  usage_none: "no ceiling",
  recorded_title: "What is recorded",
  recorded_lead: "During the study, SOKKAN keeps a trace of how you work with the agents. Here is exactly what, and what the researcher receives.",
  recorded_always_title: "Always, under your pseudonym",
  recorded_always: [
    "The scenario you chose (S1 to S6) and the reference time you enter.",
    "How long each request to the agent took, and which model answered it.",
    "Every approval the agent asked for: allowed, refused or corrected, and how long you took to decide.",
    "The memory documents the agent consulted, and how old they were.",
    "Your two answers at the end of a task.",
  ],
  recorded_consent_title: "Only if you tick the scenario",
  recorded_consent: [
    "The text of your exchanges with the agent for that scenario: your requests, its answers, what it proposed to write or to run.",
    "Passwords and keys are masked in it, e-mail addresses replaced.",
  ],
  recorded_never_title: "Never",
  recorded_never: [
    "Your name and your e-mail address: the table that links a person to their pseudonym stays on this machine and is never exported.",
    "The text of your exchanges for a scenario you did not tick: only its length is noted.",
    "The text of the files the agent reads and the raw output of its tools: only their size is noted, even for a ticked scenario.",
  ],
  recorded_who: "Who sees what: the administrators of this instance see the pseudonym table. The researcher only receives the export package. The inference service receives your pseudonym, not your address.",
  consent_title: "Your consent",
  consent_none: "You have not answered yet.",
  consent_given: "You gave your consent on {date}.",
  consent_withdrawn: "You withdrew your consent on {date}.",
  consent_outdated: "The text has changed since you consented: please read it again.",
  consent_required: "While the study is active, the free use of SOKKAN requires this consent: without it your sessions do not start.",
  consent_content_label: "I also agree that the text of my exchanges is shared for these scenarios (optional):",
  consent_give: "I give my consent",
  consent_update: "Save my choices",
  consent_withdraw: "Withdraw my consent",
  consent_withdraw_confirm: "Withdraw your consent? Nothing more is recorded for the study, and nothing collected so far is shared.",
  consent_withdraw_note: "You can withdraw at any time: nothing more is collected, and nothing collected so far is shared.",
  tasks_title: "My study tasks",
  tasks_none: "No task yet: they appear here from your first request to an agent.",
  tasks_scenario: "Scenario",
  tasks_pick: "choose",
  tasks_reference: "Time without the agent (min)",
  tasks_reference_help: "How long this task usually takes you, without an agent.",
  tasks_requests: "{n} request(s)",
  tasks_untitled: "Untitled",
  fb_title: "Task done? Two questions.",
  fb_useful: "Was the agent useful to you?",
  fb_useful_low: "not at all",
  fb_useful_high: "very",
  fb_verify: "How many minutes did you spend checking its work?",
  fb_send: "Send",
  fb_done: "Answered: usefulness {u}/5, {m} min of checking",
  fb_edit: "change",
  admin_title: "Study administration",
  admin_single: "Local sign-in is a single identity: every volunteer would share one pseudonym. Give each person their own account before starting.",
  admin_people: "Correspondence table (stays on this instance)",
  admin_people_none: "Nobody has opened the study yet.",
  admin_col_pseudo: "Pseudonym",
  admin_col_person: "Person",
  admin_col_consent: "Consent",
  admin_col_content: "Content shared",
  admin_consent_given: "given",
  admin_consent_withdrawn: "withdrawn",
  admin_consent_none: "no answer",
  admin_export: "Export package for the researcher",
  admin_export_help: "Metadata only by default; the content of the exchanges only for the scenarios each person ticked. No name, no address.",
  admin_export_zip: "Download the package (.zip)",
  admin_export_json: "View as JSON",
  admin_tokens: "Read-only export tokens",
  admin_tokens_help: "A token opens the export package and nothing else. It is shown once.",
  admin_token_label: "For whom (e.g. researcher, university)",
  admin_token_days: "Valid for (days)",
  admin_token_create: "Create a token",
  admin_token_once: "Copy this token now, it will not be shown again:",
  admin_token_revoke: "revoke",
  admin_token_revoked: "revoked",
  admin_token_none: "No token.",
  admin_token_uses: "{n} use(s)",
  admin_curl: "Command for the researcher",
  admin_journal: "Validations journal (whole instance)",
  admin_journal_help: "Every approval asked and every decision, with the delay and the origin. With addresses: for administrators only.",
  admin_journal_project: "Project",
  admin_journal_person: "Person (address)",
  admin_journal_since: "From",
  admin_journal_until: "To",
  admin_refresh: "Read the state again",
  admin_exports_last: "Last export: {date} ({n} sessions)",
  err: "Could not do that: {e}",
};

export type StudyText = typeof fr;
export const TEXT: Record<Lang, StudyText> = { fr, en };

/** Fill `{name}` placeholders. */
export function fill(s: string, vars: Record<string, string | number>): string {
  return s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? ""));
}

export function fmtDate(v: string | number | null | undefined, lang: Lang): string {
  if (v === null || v === undefined || v === "") return "—";
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  // an ISO instant (the study's end, « 2027-04-30T23:59:59Z ») is a DATE of the protocol: shown
  // as written, not shifted to the next day by the reader's time zone; an epoch is a moment
  return d.toLocaleDateString(lang === "fr" ? "fr-CH" : "en-GB",
    { day: "2-digit", month: "2-digit", year: "numeric", ...(typeof v === "string" ? { timeZone: "UTC" } : {}) });
}

export function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : `${n}`;
}
