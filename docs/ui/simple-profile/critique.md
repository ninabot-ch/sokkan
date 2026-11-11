# UI critique — SOKKAN cockpit / simple profile — 2026-11-11

## 1. Frame
- **Who**: Camille, instructional designer in a training agency. Never saw a terminal; Word, Outlook, Teams. French speaker.
- **Tasks**: (1) have a training plan drafted from 3 internal documents; (2) understand and answer what the agent
  asks to do; (3) get the document out (preview, Word) and validate it.
- **Device scope**: desktop first (1440, floor 1280), usable at 390. **Must not change**: every screen of the
  technical profile (dark cockpit, Crew and Helm decks).
- **Success signal**: journey verdict + time to the first draft (target: SUCCESS without help, < 6 min).
  Baseline 10.11 (v3.5.1): **ABANDONED** — no way to attach a document anywhere.
- **Run**: `before/` = v3.5.1 (main 6f7fee5) · `after/` = branch `simple-profile` · specs `specs/*.yaml` ·
  widths 1440 / 1280 / 390 · light and dark. Disposable instances on rog1, fictional documents only.

## 2. Scores (0-4, references/heuristics.md) — before → after, for THIS persona
| category | before | after | one line |
|---|---|---|---|
| Navigation | 1 | 4 | 4 planes × 11 sub-tabs in English → 4 entries in her words + her recent works |
| Visual hierarchy | 1 | 4 | empty pane + « 0 window(s) » → one question, one composer, one brass action |
| States | 1 | 3 | empty / loading / error / waiting / validated exist on every list and panel |
| Feedback & status | 1 | 3 | raw tool names → sentences; a long wait is announced; still no progress within a long write |
| Forms & input | 0 | 4 | no upload at all → picker + drag and drop, refusals explained in words |
| Accessibility | 2 | 3 | AA contrast checked by a script in both themes, 40 px targets, names on icon buttons; no keyboard pass yet |
| Density & layout | 2 | 4 | ~79 % of text ≤ 12 px → 12 px floor, 14 px body, 760 px measure |
| Consistency | 3 | 3 | tokens only inside the profile; the sign-in screen is still the dark English one |
| Onboarding & TTV | 0 | 3 | abandoned → first draft 1 min 46 s after arriving (idle engine) |

## 3. Problems found while building (captures in `after/`, journeys in `journey/`)
| # | where | problem → consequence | rule | sev | priority | state |
|---|---|---|---|---|---|---|
| 1 | thread @390 | title squeezed into a 40 px column, one syllable per line | no wrapped label in a header | 4 | 1 | fixed |
| 2 | approval @390 | name and age of a source drawn on top of each other | legibility | 3 | 1 | fixed |
| 3 | journey run 2 | « L'agent veut effectuer une opération technique » for a `mkdir` → she cannot judge | match with the real world | 4 | 1 | fixed (no shell approval in a simple work) |
| 4 | journey run 1 | one sentence of the agent in English in a French thread | consistency | 2 | 2 | fixed (language rule repeated after the request) |
| 5 | drawer, works | engine failure shown as the agent's last sentence (« API Error: Connection refused… ») | error in plain language | 3 | 1 | fixed (notice, raw text folded) |
| 6 | thread | 1 to 5 min with only a spinner while the document is written | visibility of status | 3 | 2 | eased (hint after 15 s); real progress needs streaming of the draft |
| 7 | works list @1440 | cards without surface nor border | affordance | 2 | 2 | fixed |
| 8 | selected recent | muted text 4.3:1 on the tinted background | WCAG 1.4.3 | 2 | 2 | fixed |
| 9 | deliverable panel | preview kept the validated v1 while v2 was saved | status | 3 | 2 | fixed (reloads, « changed since your validation ») |
| 10 | panel head @390 | head clipped by the body | layout | 3 | 1 | fixed |
| 11 | settings @390 | language choice on two rows | layout | 1 | 3 | fixed |
| 12 | draft preview | « Client : … Format : … » lines of the agent merged into one paragraph (Markdown soft breaks) | readability | 1 | 3 | open |

**Smoke findings dismissed**: line-clamp on a work's title and last sentence in the drawer and the thread header
(user content in a list, full value in the thread and in `title`); 401 on `/api/me` and `/api/features` before the
sign-in of each capture (the spec signs in on every page); `scrollable-region-focusable` at 390 on the draft /
deliverable panel (the body has `tabindex=0`; axe reports the table's own scroller inside it).

**Not fixing now**: the sign-in screen (cockpit's, English — SSO on the pilot box); a keyboard-only pass; progress
inside a long write; Markdown soft line breaks; the technical profile's own defects (out of scope by decision).

## 5. Outcome
- Before / after pairs: `compare/` (landing → home, raw thread → plain thread, spawn form → template, Nina).
- Technical profile unchanged: `compare-technical/compare.md` — 0.09 % of pixels on the landing at 1440 (the session
  title and Nina's button, both data), no layout change.
- Journeys (real Chrome, lane on rog1, engine qwen3.8-27b): `journey/run1` SUCCESS (slow), `run2` SUCCESS (slow),
  `run3` **SUCCESS** — see `journey/README.md`.
