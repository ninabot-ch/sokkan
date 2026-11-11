# Journey — sokkan simple profile — Camille gets a training plan from 3 documents and validates it

**Persona**: Camille, instructional designer at a training agency. Never saw a terminal. Uses Word, Outlook, Teams. French speaker.  
**Goal**: Have a training plan drafted from 3 internal documents (a client brief, an existing module, a skills framework) and validate the result.  
**Start**: http://100.124.110.23:3854 · mode write · device desktop 1440x900

## Verdict: SUCCESS (slow)

- Time to first value: **422.5 s** (target 360 s) — success criterion: A training plan built from the 3 documents is saved, previewed and validated by Camille without help; first draft visible in under 6 minutes.
- Actions: **14** vs minimum 9 (efficiency 0.64)
- Hesitations: **1** · confusion points: **1**
- Outcome note: livrable validé, daté, sans aide
- Times are the agent's wall clock (reading + deciding included): compare runs of the same scenario, and read them as relative effort, not as a human's seconds.

## Steps

| step | reached | time (s) / budget | actions / min | hesitations |
|---|---|---|---|---|
| s1 Understand where to start and say what she wants | yes | 24.5 / 60 | 3 / 2 | 0 |
| s2 Attach the 3 documents | yes | 12.0 / 60 | 2 / 2 | 0 |
| s3 Launch and follow what the agent does | yes | 350.9 / 120 ⚠ | 2 / 1 | 1 |
| s4 Understand the approval request and read the draft | yes | 19.4 / 90 | 4 / 2 | 0 |
| s5 Open the deliverable and validate it | yes | 15.0 / 60 | 3 / 2 | 0 |

## Hesitations and confusion points

- **s3** hesitate: au-delà de 3 minutes annoncées, je me demande si c'est bloqué ; le bandeau « En cours » et le cercle qui tourne me retiennent
- **s4 CONFUSION** une phrase de l'agent est en anglais au milieu du fil français — target `` — capture `shots/simpleui-j4-approval.jpg`

## Timeline

| t (s) | kind | step | target | note |
|---|---|---|---|---|
| 0.0 | start |  | http://100.124.110.23:3854 | sokkan simple profile — Camille gets a training plan from 3 documents and validates it |
| 0.1 | step | s1 |  | arrive sur l'accueil après connexion (SSO en vrai) |
| 24.4 | read | s1 | accueil | titre « Sur quoi travaille-t-on ? » + 4 modèles : je comprends où commencer |
| 24.4 | click | s1 | modèle Plan de formation |  |
| 24.5 | type | s1 | zone de saisie | l'exemple du modèle me dit quoi écrire |
| 24.6 | done | s1 |  |  |
| 24.7 | step | s2 |  |  |
| 36.5 | click | s2 | Joindre des documents | sélecteur de fichiers, 3 fichiers choisis d'un coup |
| 36.6 | read | s2 | 3 pastilles de fichiers | je vois mes 3 documents, chacun retirable |
| 36.7 | done | s2 |  |  |
| 36.8 | step | s3 |  |  |
| 36.9 | click | s3 | Lancer |  |
| 387.5 | read | s3 | fil | « A lu … » x3 puis « L'agent rédige… une à trois minutes » ; attente longue (modèle chargé : 5 requêtes concurrentes) |
| 387.6 | hesitate | s3 |  | au-delà de 3 minutes annoncées, je me demande si c'est bloqué ; le bandeau « En cours » et le cercle qui tourne me retie |
| 387.7 | done | s3 |  |  |
| 387.8 | step | s4 |  |  |
| 406.8 | read | s4 | carte Votre accord est demandé | je comprends : enregistrer un document, 3 sources dont une vieille de 14 mois, rien ne sort |
| 406.9 | confusion | s4 |  | une phrase de l'agent est en anglais au milieu du fil français |
| 407.0 | click | s4 | Voir le brouillon |  |
| 407.1 | read | s4 | brouillon | premier brouillon lisible, sources citées par nom |
| 407.2 | click | s4 | Autoriser (panneau brouillon) |  |
| 407.2 | done | s4 |  |  |
| 407.4 | step | s5 |  |  |
| 422.0 | click | s5 | Ouvrir l'aperçu |  |
| 422.1 | read | s5 | livrable | aperçu + Télécharger en Word + Valider le livrable, avec ce que valider veut dire |
| 422.2 | click | s5 | Valider le livrable |  |
| 422.4 | done | s5 |  |  |
| 422.5 | success | s5 |  | livrable validé, daté, sans aide |
| 422.6 | end | s5 |  |  |

## Journey critique (fill in — grid: references/heuristics.md, focus on the PATH, not single screens)

- Where did the persona expect the next step to be, and where was it? (wayfinding, labels, information scent)
- Which step cost the most time or actions vs its minimum, and why?
- What did the UI promise that it didn't deliver (demo banner, CTA, empty state)?
- Feedback along the way: did the persona always know it was progressing?
- TTFV verdict and the 1-3 changes that would cut it the most (impact × effort):

