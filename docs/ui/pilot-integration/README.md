# Pilot integration — UI evidence (branch `pilot-integration`, 11.11.2026)

Captures of ONE journey played in a real browser (headless Chromium, 1280 × 800, French) on a
throw-away instance of this branch: local sign-in with named accounts, the appliance engine
(sessions → local router → `qwen3.8-27b` served by vLLM), study mode on, document ingestion on.
Every person, organisation and document is fictional. Measures and what the databases held
afterwards: `docs/enterprise/PILOT-RELEASE-NOTES.md` § 4.

> **Eleven of the thirteen captures of this journey were withdrawn before publication**: the
> study band or the header of the screen showed the internal name given to the study and to the
> test organisation on that instance. They were not retouched and not replayed (the instance is
> gone; the same screens, with a neutral study, are in `passerelle-reelle/` for the journey and
> the study band, and in `docs/ui/simple-profile/` for the home, the approval card and the
> draft). What they showed is kept below as a description; the measures of § 4 stand.

Camille's journey (simple profile) — the two captures kept:

| | |
|---|---|
| `11-livrable-valide.png` | the deliverable, validated |
| `13-reponses-envoyees.png` | the two questions of the study, answered |

Seen on that journey, capture withdrawn:

- home before the consent: the study band asks for the consent before a first work;
- « What is recorded », opened by itself when the first start is refused;
- consent given: the band says the pseudonym (P1);
- the first draft, 46 s after « Lancer »: the approval card, its three sources and their age;
- the draft corrected before allowing (« Autoriser avec mes corrections »).

Review of an ingestion batch (the memory agent read 14 + 8 synthetic documents with the same
model) — all six captures withdrawn; seen on screen:

- simple profile, « Notes à relire »: the batch in figures, flagged notes first;
- a refusal of the API said on the note, in French (this instance had no Postgres store for a
  project's memory);
- the batch refused with a reason;
- the same screen embedded in the technical cockpit, Setup › Ingestion;
- the batch approved: 4 notes in the memory, 2 flagged notes left to decide;
- the same in English.
