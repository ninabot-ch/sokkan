# Journey — sokkan simple profile — Camille gets a training plan from 3 documents and validates it

**Persona**: Camille, instructional designer at a training agency. Never saw a terminal. Uses Word, Outlook, Teams. French speaker.  
**Goal**: Have a training plan drafted from 3 internal documents (a client brief, an existing module, a skills framework) and validate the result.  
**Start**: http://100.124.110.23:3854 · mode write · device desktop 1440x900

## Verdict: SUCCESS (slow)

- Time to first value: **483.6 s** (target 360 s) — success criterion: A training plan built from the 3 documents is saved, previewed and validated by Camille without help; first draft visible in under 6 minutes.
- Actions: **13** vs minimum 9 (efficiency 0.69)
- Hesitations: **0** · confusion points: **1**
- Outcome note: livrable validé ; premier brouillon à 7 min 32 s (modèle partagé, une demande de commande refusée)
- Times are the agent's wall clock (reading + deciding included): compare runs of the same scenario, and read them as relative effort, not as a human's seconds.

## Steps

| step | reached | time (s) / budget | actions / min | hesitations |
|---|---|---|---|---|
| s1 Understand where to start and say what she wants | yes | 25.4 / 60 | 3 / 2 | 0 |
| s2 Attach the 3 documents | yes | 0.4 / 60 | 2 / 2 | 0 |
| s3 Launch and follow what the agent does | yes | 430.9 / 120 ⚠ | 3 / 1 | 0 |
| s4 Understand the approval request and read the draft | yes | 25.8 / 90 | 3 / 2 | 0 |
| s5 Open the deliverable and validate it | yes | 0.4 / 60 | 2 / 2 | 0 |

## Hesitations and confusion points

- **s3 CONFUSION** après 5 min 30 : « L'agent veut effectuer une opération technique » (un mkdir) — je ne sais pas ce que c'est ; le détail technique est illisible pour moi — target `` — capture `shots/simpleui-k4-approval.jpg`

## Timeline

| t (s) | kind | step | target | note |
|---|---|---|---|---|
| 0.0 | start |  | http://100.124.110.23:3854 | sokkan simple profile — Camille gets a training plan from 3 documents and validates it |
| 0.1 | step | s1 |  | accueil après connexion |
| 25.1 | read | s1 | accueil |  |
| 25.2 | click | s1 | modele-Plan-de-formation |  |
| 25.4 | type | s1 | zone-de-saisie |  |
| 25.5 | done | s1 | - |  |
| 25.7 | step | s2 | - |  |
| 25.9 | click | s2 | Joindre-des-documents |  |
| 26.0 | read | s2 | 3-pastilles |  |
| 26.1 | done | s2 | - |  |
| 26.2 | step | s3 | - |  |
| 26.3 | click | s3 | Lancer |  |
| 377.7 | read | s3 | fil | 3 documents lus, puis longue attente (modèle partagé : 4 requêtes concurrentes) |
| 377.8 | confusion | s3 |  | après 5 min 30 : « L'agent veut effectuer une opération technique » (un mkdir) — je ne sais pas ce que c'est ; le détail |
| 377.9 | click | s3 | Refuser | la carte dit que refuser ne casse rien |
| 457.1 | done | s3 |  |  |
| 457.1 | step | s4 |  |  |
| 482.6 | click | s4 | Voir-le-brouillon |  |
| 482.8 | read | s4 | brouillon |  |
| 482.9 | click | s4 | Autoriser |  |
| 483.0 | done | s4 | - |  |
| 483.1 | step | s5 | - |  |
| 483.2 | click | s5 | Ouvrir-l-apercu |  |
| 483.4 | click | s5 | Valider-le-livrable |  |
| 483.5 | done | s5 | - |  |
| 483.6 | success | s5 |  | livrable validé ; premier brouillon à 7 min 32 s (modèle partagé, une demande de commande refusée) |
| 483.7 | end | s5 |  |  |

## Journey critique (fill in — grid: references/heuristics.md, focus on the PATH, not single screens)

- Where did the persona expect the next step to be, and where was it? (wayfinding, labels, information scent)
- Which step cost the most time or actions vs its minimum, and why?
- What did the UI promise that it didn't deliver (demo banner, CTA, empty state)?
- Feedback along the way: did the persona always know it was progressing?
- TTFV verdict and the 1-3 changes that would cut it the most (impact × effort):

