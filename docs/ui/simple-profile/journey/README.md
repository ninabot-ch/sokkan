# Camille's journey — played in a real Chrome (brosim lane on rog1), 11.11.2026

Persona: instructional designer, never saw a terminal. Task: have a training plan drafted from 3 internal
documents (fictional: a client brief .docx, a module .pptx, a skills framework .pdf) and validate the result.
Disposable instance, simple profile, engine `qwen3.8-27b` (custom mode, `CLAUDE_CODE_EFFORT_LEVEL=medium`).
Scenario: `../specs/camille.journey.yaml`. Times are the agent's wall clock; sign-in is outside the journey.

| run | verdict | first draft visible (approval card) | deliverable validated | engine load | what happened |
|---|---|---|---|---|---|
| baseline 10.11, v3.5.1 | **ABANDONED** | — | — | — | no way to attach a document |
| 1 | SUCCESS (slow) | 6 min 19 s after arriving (5 min 43 s after « Lancer ») | 7 min 03 s | 5 concurrent requests, one of them an orphan of my own earlier test | one sentence of the agent in English |
| 2 | SUCCESS (slow) | 7 min 32 s (7 min 08 s after « Lancer ») | 8 min 04 s | 4 to 8 concurrent requests from other work | a « technical operation » (a `mkdir`) asked for approval at 5 min 30 s; refused, the agent went on |
| 3 | **SUCCESS** | **1 min 46 s** (81 s after « Lancer ») | 2 min 17 s | idle | nothing; 14 actions for a minimum of 9, no hesitation |

Runs 1 and 2 led to three fixes that run 3 has: the language rule repeated after the request, no shell approval in a
simple work, a « one to three minutes » hint during a long write. Every second beyond the first 40 is the engine's:
the person's own part (say what she wants, attach, launch) takes 25 to 40 s, reading the card and the draft 20 to 30 s.
The 6-minute target is met on an idle engine and missed by 20 to 90 s when the same engine serves 4 to 8 other
requests — the pilot's engine sizing decides this, not the screens.
