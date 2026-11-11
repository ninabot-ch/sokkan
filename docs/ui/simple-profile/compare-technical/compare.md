# Visual compare

before `before-dark` → after `technical-after` · threshold 0.1 · changed if > 0.20%

**6 of 8 captures changed.**

| capture | changed | bbox (x,y,w,h) | size before → after | triptych |
|---|---|---|---|---|
| 02-thread__390__dark.png | **yes** 10.192% | [0, 124, 390, 720] | [390, 844] → [390, 844] | `compare-technical/diff/02-thread__390__dark__diff.png` |
| 02-thread__1440__dark.png | **yes** 5.539% | [12, 100, 1408, 792] | [1440, 900] → [1440, 900] | `compare-technical/diff/02-thread__1440__dark__diff.png` |
| 04-nina__390__dark.png | **yes** 5.217% | [12, 173, 358, 651] | [390, 844] → [390, 844] | `compare-technical/diff/04-nina__390__dark__diff.png` |
| 04-nina__1440__dark.png | **yes** 0.818% | [12, 141, 1408, 739] | [1440, 900] → [1440, 900] | `compare-technical/diff/04-nina__1440__dark__diff.png` |
| 03-new-session__390__dark.png | **yes** 0.551% | [12, 403, 346, 409] | [390, 844] → [390, 844] | `compare-technical/diff/03-new-session__390__dark__diff.png` |
| 01-landing__390__dark.png | **yes** 0.549% | [12, 173, 346, 639] | [390, 844] → [390, 844] | `compare-technical/diff/01-landing__390__dark__diff.png` |
| 03-new-session__1440__dark.png | no 0.091% | [12, 386, 1408, 494] | [1440, 900] → [1440, 900] | `compare-technical/diff/03-new-session__1440__dark__diff.png` |
| 01-landing__1440__dark.png | no 0.091% | [12, 141, 1408, 739] | [1440, 900] → [1440, 900] | `compare-technical/diff/01-landing__1440__dark__diff.png` |

## Smoke findings

resolved **70** · new **10** · still present **42**

### New (regressions?)

- [error] `truncated-text` 01-landing @1440 dark — ellipsis on div.group.flex > button.min-w-0.flex-1 > span.mt-0.5.block (185 px hidden) — "Plan de formation — Formation Excel intermédiaire pour les…"
- [error] `truncated-text` 01-landing @390 dark — ellipsis on div.group.flex > button.min-w-0.flex-1 > span.mt-0.5.block (51 px hidden) — "Plan de formation — Formation Excel intermédiaire pour les…"
- [error] `flow-step` 02-thread @1440 dark — step 4 {"click": "text=Plan de formation Excel"} failed: Locator.click: Timeout 8000ms exceeded.
- [error] `truncated-text` 02-thread @1440 dark — ellipsis on div.group.flex > button.min-w-0.flex-1 > span.mt-0.5.block (185 px hidden) — "Plan de formation — Formation Excel intermédiaire pour les…"
- [error] `flow-step` 02-thread @390 dark — step 4 {"click": "text=Plan de formation Excel"} failed: Locator.click: Timeout 8000ms exceeded.
- [error] `truncated-text` 02-thread @390 dark — ellipsis on div.group.flex > button.min-w-0.flex-1 > span.mt-0.5.block (51 px hidden) — "Plan de formation — Formation Excel intermédiaire pour les…"
- [error] `truncated-text` 03-new-session @1440 dark — ellipsis on div.group.flex > button.min-w-0.flex-1 > span.mt-0.5.block (185 px hidden) — "Plan de formation — Formation Excel intermédiaire pour les…"
- [error] `truncated-text` 03-new-session @390 dark — ellipsis on div.group.flex > button.min-w-0.flex-1 > span.mt-0.5.block (51 px hidden) — "Plan de formation — Formation Excel intermédiaire pour les…"
- [error] `truncated-text` 04-nina @1440 dark — ellipsis on div.group.flex > button.min-w-0.flex-1 > span.mt-0.5.block (185 px hidden) — "Plan de formation — Formation Excel intermédiaire pour les…"
- [error] `truncated-text` 04-nina @390 dark — ellipsis on div.group.flex > button.min-w-0.flex-1 > span.mt-0.5.block (51 px hidden) — "Plan de formation — Formation Excel intermédiaire pour les…"

### Resolved

- [warn] `reduced-motion` 01-landing @1440 dark — 1 infinite animations run despite prefers-reduced-motion
- [warn] `reduced-motion` 01-landing @390 dark — 1 infinite animations run despite prefers-reduced-motion
- [error] `console-error` 01-landing @1440 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/me]
- [error] `console-error` 01-landing @1440 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/features]
- [error] `http` 01-landing @1440 light — GET http://100.124.110.23:3853/api/me -> 401
- [error] `http` 01-landing @1440 light — GET http://100.124.110.23:3853/api/features -> 401
- [warn] `target-size` 01-landing @1440 light — 5 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17
- [warn] `reduced-motion` 01-landing @1440 light — 1 infinite animations run despite prefers-reduced-motion
- [error] `console-error` 01-landing @390 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/me]
- [error] `console-error` 01-landing @390 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/features]
- [error] `http` 01-landing @390 light — GET http://100.124.110.23:3853/api/me -> 401
- [error] `http` 01-landing @390 light — GET http://100.124.110.23:3853/api/features -> 401
- [warn] `target-size` 01-landing @390 light — 1 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21
- [warn] `reduced-motion` 01-landing @390 light — 1 infinite animations run despite prefers-reduced-motion
- [error] `truncated-text` 02-thread @1440 dark — ellipsis on div.my-1.rounded-lg > button.flex.w-full > span.truncate.text-mut (201 px hidden) — "formation Excel intermédiaire assistantes administratives pl"
- [warn] `reduced-motion` 02-thread @1440 dark — 3 infinite animations run despite prefers-reduced-motion
- [error] `truncated-text` 02-thread @390 dark — ellipsis on div.my-1.rounded-lg > button.flex.w-full > span.truncate.text-mut (407 px hidden) — "formation Excel intermédiaire assistantes administratives pl"
- [error] `truncated-text` 02-thread @390 dark — ellipsis on div.my-1.rounded-lg > button.flex.w-full > span.truncate.text-mut (138 px hidden) — "/root/sokkan-simple/scratch/before/workspace/plan_v1.md"
- [warn] `reduced-motion` 02-thread @390 dark — 2 infinite animations run despite prefers-reduced-motion
- [error] `axe:scrollable-region-focusable` 02-thread @390 dark — [serious] Scrollable region must have keyboard access — 1 node(s), e.g. Fix any of the following:   Element should have focusable content
- [error] `console-error` 02-thread @1440 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/me]
- [error] `console-error` 02-thread @1440 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/features]
- [error] `http` 02-thread @1440 light — GET http://100.124.110.23:3853/api/me -> 401
- [error] `http` 02-thread @1440 light — GET http://100.124.110.23:3853/api/features -> 401
- [error] `truncated-text` 02-thread @1440 light — ellipsis on div.my-1.rounded-lg > button.flex.w-full > span.truncate.text-mut (201 px hidden) — "formation Excel intermédiaire assistantes administratives pl"
- [warn] `target-size` 02-thread @1440 light — 12 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21; div.min-h-0.flex-1 > div.group.flex > button.mt-0.5.rounded 21x24; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17
- [warn] `reduced-motion` 02-thread @1440 light — 3 infinite animations run despite prefers-reduced-motion
- [error] `console-error` 02-thread @390 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/me]
- [error] `console-error` 02-thread @390 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/features]
- [error] `http` 02-thread @390 light — GET http://100.124.110.23:3853/api/me -> 401
- [error] `http` 02-thread @390 light — GET http://100.124.110.23:3853/api/features -> 401
- [error] `truncated-text` 02-thread @390 light — ellipsis on div.my-1.rounded-lg > button.flex.w-full > span.truncate.text-mut (407 px hidden) — "formation Excel intermédiaire assistantes administratives pl"
- [error] `truncated-text` 02-thread @390 light — ellipsis on div.my-1.rounded-lg > button.flex.w-full > span.truncate.text-mut (138 px hidden) — "/root/sokkan-simple/scratch/before/workspace/plan_v1.md"
- [warn] `target-size` 02-thread @390 light — 7 targets < 24x24 px (WCAG 2.5.8): main.min-h-0.flex-1 > div.flex.items-center > button.text-[13px].font-medium 70x20; header.flex.items-center > div.ml-auto.flex > button.shrink-0.rounded-md 111x23; header.flex.items-center > div.ml-auto.flex > button.rounded-md.border 66x23; div.min-h-0.flex-1 > div.my-1 > button.text-[11.5px].italic 74x17
- [warn] `reduced-motion` 02-thread @390 light — 2 infinite animations run despite prefers-reduced-motion
- [error] `axe:scrollable-region-focusable` 02-thread @390 light — [serious] Scrollable region must have keyboard access — 1 node(s), e.g. Fix any of the following:   Element should have focusable content
- [warn] `reduced-motion` 03-new-session @1440 dark — 1 infinite animations run despite prefers-reduced-motion
- [warn] `reduced-motion` 03-new-session @390 dark — 1 infinite animations run despite prefers-reduced-motion
- [error] `console-error` 03-new-session @1440 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/me]
- [error] `console-error` 03-new-session @1440 light — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3853/api/features]

### Still present

- [error] `console-error` 01-landing @1440 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/me]
- [error] `console-error` 01-landing @1440 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/features]
- [error] `http` 01-landing @1440 dark — GET http://100.124.110.23:3854/api/me -> 401
- [error] `http` 01-landing @1440 dark — GET http://100.124.110.23:3854/api/features -> 401
- [warn] `target-size` 01-landing @1440 dark — 5 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17
- [error] `console-error` 01-landing @390 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/me]
- [error] `console-error` 01-landing @390 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/features]
- [error] `http` 01-landing @390 dark — GET http://100.124.110.23:3854/api/me -> 401
- [error] `http` 01-landing @390 dark — GET http://100.124.110.23:3854/api/features -> 401
- [warn] `target-size` 01-landing @390 dark — 1 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21
- [error] `console-error` 02-thread @1440 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/me]
- [error] `console-error` 02-thread @1440 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/features]
- [error] `http` 02-thread @1440 dark — GET http://100.124.110.23:3854/api/me -> 401
- [error] `http` 02-thread @1440 dark — GET http://100.124.110.23:3854/api/features -> 401
- [warn] `target-size` 02-thread @1440 dark — 5 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17
- [error] `console-error` 02-thread @390 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/me]
- [error] `console-error` 02-thread @390 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/features]
- [error] `http` 02-thread @390 dark — GET http://100.124.110.23:3854/api/me -> 401
- [error] `http` 02-thread @390 dark — GET http://100.124.110.23:3854/api/features -> 401
- [warn] `target-size` 02-thread @390 dark — 1 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21
- [error] `console-error` 03-new-session @1440 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/me]
- [error] `console-error` 03-new-session @1440 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/features]
- [error] `http` 03-new-session @1440 dark — GET http://100.124.110.23:3854/api/me -> 401
- [error] `http` 03-new-session @1440 dark — GET http://100.124.110.23:3854/api/features -> 401
- [warn] `target-size` 03-new-session @1440 dark — 6 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21; div.space-y-1.5.border-b > label.flex.cursor-pointer > input.accent-slate-500 13x13; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17
- [error] `axe:select-name` 03-new-session @1440 dark — [critical] Select element must have an accessible name — 1 node(s), e.g. Fix any of the following:   Element does not have an implicit (wrapped) <label>
- [error] `console-error` 03-new-session @390 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/me]
- [error] `console-error` 03-new-session @390 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/features]
- [error] `http` 03-new-session @390 dark — GET http://100.124.110.23:3854/api/me -> 401
- [error] `http` 03-new-session @390 dark — GET http://100.124.110.23:3854/api/features -> 401
- [warn] `target-size` 03-new-session @390 dark — 2 targets < 24x24 px (WCAG 2.5.8): aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21; div.space-y-1.5.border-b > label.flex.cursor-pointer > input.accent-slate-500 13x13
- [error] `axe:select-name` 03-new-session @390 dark — [critical] Select element must have an accessible name — 1 node(s), e.g. Fix any of the following:   Element does not have an implicit (wrapped) <label>
- [error] `console-error` 04-nina @1440 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/me]
- [error] `console-error` 04-nina @1440 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/features]
- [error] `http` 04-nina @1440 dark — GET http://100.124.110.23:3854/api/me -> 401
- [error] `http` 04-nina @1440 dark — GET http://100.124.110.23:3854/api/features -> 401
- [warn] `target-size` 04-nina @1440 dark — 6 targets < 24x24 px (WCAG 2.5.8): div.fixed.bottom-20 > div.flex.items-start > button.ui-focus.shrink-0 50x21; aside.flex.w-full > div.flex.items-center > button.ml-auto.rounded 69x21; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17; main.min-h-0.flex-1 > div.hidden.items-center > button.rounded.px-1.5 19x17
- [error] `console-error` 04-nina @390 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/me]
- [error] `console-error` 04-nina @390 dark — Failed to load resource: the server responded with a status of 401 (Unauthorized) [http://100.124.110.23:3854/api/features]
- [error] `http` 04-nina @390 dark — GET http://100.124.110.23:3854/api/me -> 401


> Look at every triptych flagged **yes** before calling the change good or bad: the ratio says *how much* moved, not *whether it is better*.
