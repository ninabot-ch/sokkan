# Pilot integration — UI evidence (branch `pilot-integration`, 11.11.2026)

Captures of ONE journey played in a real browser (headless Chromium, 1280 × 800, French) on a
throw-away instance of this branch: local sign-in with named accounts, the appliance engine
(sessions → local router → `qwen3.8-27b` served by vLLM), study mode on (`heg-eskills-2027`),
document ingestion on. Every person, organisation and document is fictional. Measures and what
the databases held afterwards: `docs/enterprise/PILOT-RELEASE-NOTES.md` § 4.

Camille's journey (simple profile):

| | |
|---|---|
| `01-accueil-avant-accord.png` | home; the study band asks for the consent before a first work |
| `03-ce-qui-est-enregistre.png` | « What is recorded », opened by itself when the first start is refused |
| `04-accueil-apres-accord.png` | consent given: the band says the pseudonym (P1) |
| `06-carte-1-brouillon.png` | the first draft, 46 s after « Lancer »: the approval card, its three sources and their age |
| `08-correction.png` | the draft corrected before allowing (« Autoriser avec mes corrections ») |
| `11-livrable-valide.png` | the deliverable, validated |
| `13-reponses-envoyees.png` | the two questions of the study, answered |

Review of an ingestion batch (the memory agent read 14 + 8 synthetic documents with the same model):

| | |
|---|---|
| `31-lot-chiffres-et-signalees.png` | simple profile, « Notes à relire »: the batch in figures, flagged notes first |
| `33-refus-du-magasin-dit-sur-la-note.png` | a refusal of the API said on the note, in French (this instance had no Postgres store for a project's memory) |
| `36-lot-refuse.png` | the batch refused with a reason |
| `41-setup-ingestion-lot.png` | the same screen embedded in the technical cockpit, Setup › Ingestion |
| `43-setup-ingestion-lot-approuve.png` | the batch approved: 4 notes in the memory, 2 flagged notes left to decide |
| `44-setup-ingestion-english.png` | the same in English |
