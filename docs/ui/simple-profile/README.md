# Simple profile — UI evidence (branch `simple-profile`, 11.11.2026)

- `critique.md` — frame, scores before → after, problems found and their state.
- `before/` — v3.5.1 (main 6f7fee5): landing, raw thread with « Allow Write? », spawn form — 1440 and 390, dark
  (the « light » captures prove the 3.5.1 cockpit ignores the system theme).
- `after/` — the simple profile: 10 screens × 1440 / 1280 / 390 × light / dark (`sheet-desktop.png`,
  `sheet-mobile.png` to see them at once). Captured on a scripted stand-in engine (deterministic states); the real
  engine's screens are in `journey/`.
- `compare/` — before | after | diff for the three moments that decided the abandon (home, approval, new work).
- `compare-technical/` — the technical profile before / after this branch: unchanged.
- `journey/` — three timed runs of Camille's journey on the real engine, with captures.
- `specs/` — the smoke specs and the journey scenario (skill `ui-ux-engineer`).
