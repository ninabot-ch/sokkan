# Helm — steering a project (SOKKAN 3.3)

Feature `helm` (`SOKKAN_FEATURE_HELM`, beta). Requires `multi_project` and `assistant`.
Default: off in community, on in enterprise. Turning it off hides the Helm tab and every
`/api/helm/*` route (404), refuses hierarchy edits on the board (400) and stops the
periodic job; the columns and tables it added stay, unused.

## The model

```
 project card (manager)          intent · constraints · decisions · links · deadline
   ├── card (engineer)           ◀── context flows DOWN to every session spawned below
   │     └── sub-task                 (pre-seed + memory note `helm-card-<id>`, card:<id>)
   └── card (engineer)
                                 progress flows UP ──▶ computed state of every parent
```

* A card has an optional **parent** (`cards.parent_id`), in the same project, no cycle,
  6 levels at most. `kind` = `task` (default) · `project` (a steering card) · `reframe`
  (born from an approved Helm suggestion). Deleting a card moves its children one level up.
* **Migration**: `ALTER TABLE cards ADD COLUMN` for `parent_id`, `kind`, `intent`,
  `constraints`, `decisions` (JSON list), `baseline_at`; every existing card is a top-level
  `task`. Idempotent, nothing is rewritten. Helm's own tables (`helm_rollup`,
  `helm_suggestions`, `helm_calendars`) live in `board.db`.
* A merge request is a link kind (`mr`, an http(s) URL) next to session / agent / run /
  incident.

## Context flows down

When a session is spawned from a card (`▶ spawn`, `POST /api/board/card/{id}/spawn`) that
has parents, the seed receives — before the memory pre-seed — a block
`=== Context from the parent cards (Helm) ===` with, root first, each ancestor's intent,
constraints, decisions and links, marked `card:<id>`, and the instruction to ask before
departing from a recorded decision. It is deterministic (built by the server, not left to
the model). The same context is written as a **project memory note** `helm-card-<id>`
(metadata `card: "card:<id>"`, `source: helm`) in the card's project directory, rewritten
on every edit of the card's context and at each spawn below it, so every session of the
project can recall it. The note is a copy: the card is the truth. A decision added or
withdrawn is journaled on the card (`decision recorded` / `decision withdrawn`).

## Progress flows up

`helm.rollup(card)` walks the subtree:

| leaf state | when |
|---|---|
| done | column Done |
| blocked | an open Operate incident linked (directly or through a linked agent), or the last linked run failed / timed out / hit its budget / was incomplete |
| waiting | column Review, a linked run waits for a tool approval, a linked agent waits for activation |
| in_progress | column Doing, a linked session working now, a linked run queued/running |
| todo | otherwise |

A parent is **done** when every leaf is, else **blocked** if anything under it is blocked,
else **waiting**, else **in progress** (something moving or already done), else **todo**.
Its own column is ignored as soon as it has children: progress is never declared by hand.
Signals aggregated for the deck: sessions (working), agent runs (active), merge requests,
open incidents. Recomputed on every board event (`board.on_change` → `helm.refresh`), by
the periodic job and on every read; a change of state is journaled on the parent card
(user `helm`, action `progress`, e.g. `In progress → Blocked (3/7 done)`).

## Helm suggests, a manager decides

A job in the API (`SOKKAN_HELM_TICK_S`, default 900 s) and the « Check now » button run
the detectors of a project. Each finding is a **suggestion** (`helm_suggestions`) shown to
the project's managers as a « reframe » card with **Approve** / **Ignore**:

| kind | rule |
|---|---|
| drift | cosine between a child card's text and its parent's intent (embedding of the memory engine, `memory/embeddings.py`) under `SOKKAN_HELM_DRIFT_MIN` (0.25). No embedding engine = detector skipped and said so |
| contradiction | a recorded decision of the parent or an ancestor rules something out ("no Kafka", "pas de microservices", "REST instead of GraphQL", "X is out of scope") and a child card (title, description, last comments) mentions it. Heuristic on purpose: it flags, a human judges |
| scope | children created after the baseline ≥ max(3, 30 % of the baseline). « Accept the new scope » re-baselines |
| unparented | ≥ 3 open top-level cards created in a steered project since its first project card |
| unassigned | open children without an owner |
| slowing | done in 7 days < half the weekly pace of the 3 weeks before, or no event for 7 days with open work |
| racing | ≥ 5 cards created in 7 days and twice as many as done, or agent-run spend in 24 h > 3× the daily average of the week before (and > $1) |
| incident | an open Operate incident linked under the card |

Approve = a `reframe` card under the steering card (or top level), assigned to the
approver, plus a comment on the concerned card; the work itself is never edited.
Ignore = not proposed again for `SOKKAN_HELM_SNOOZE_DAYS` (7). A suggestion whose
condition disappears is marked `resolved`. New suggestions are notified (Operate channel)
by the periodic job. Audit: `helm.suggestion.approve|ignore`, `helm.scope.accept`.

## The Helm view

Tab **Helm**, for the people who steer at least one project: a project's `maintainer` or
`admin`, and the instance admins **in the projects they belong to** (decision of 07.10: an
instance admin sees no project content without being added to it, which is journaled).
`/api/helm/*` is not a project-scoped route for projectgate: every route checks
`helm.can_steer(user, project)` itself and answers 404 otherwise.

* Deck (Crew's grammar): columns To do · In progress · Waiting for approval · Blocked ·
  Done; colour **and** label; the card breathes while sessions or agents work under it
  (`prefers-reduced-motion`: static ring); progress bar, signals, owners, suggestion count.
* Popout with tabs: **Kanban** (the card's children by column — the Board's own
  `BoardColumns` component — with their computed state; « open its board » drills down;
  breadcrumb back up), **Activity** (events of the whole subtree), **Suggestions**
  (Approve / Ignore / Accept the new scope / Check now), **Costs** (sessions' estimated cost
  from their transcripts + agent runs' cost).
* Filters: project, team (a team granted on the project, or whose members own cards),
  person (owner of the card or of a card below it).
* The card dialog of the Board gets a « Helm » section: breadcrumb, kind, move under a
  card, intent, constraints, decisions, cards under it.

## Nina creates a project

« Create a project » in Nina's chat: she interviews (goal, scope, constraints and
decisions already taken, deadline, team) one question at a time, then proposes a
breakdown into cards and ends with a ```` ```sokkan-project ```` block. The cockpit shows
it **editable** (title, goal, scope, constraints, deadline, team, each card's title and
owner, add/remove cards); « Create the project » posts it to `POST /api/helm/projects`
(developer role in the project): the project card (`kind=project`, owner = the person),
its children, the baseline set after them (the agreed scope), the memory note. Nina
itself never creates anything (her boundary since S1).

## Morning brief

Crew → « + New agent » → template **Morning brief** (`backend/agent_templates.py`): every
weekday 07:30 (Zurich), haiku, one tool — `mcp__sokkan-board__morning_brief` (read-only,
auto-approved, also allowed on an alert-triggered agent) — which returns the facts
gathered by SOKKAN for a person (their cards and the cards under their project cards) or a
team: cards that moved since the last working day (Monday: since Friday), blocked cards
and why, approvals waiting, incidents, agents in error, decisions recorded above their
work, Helm suggestions, and today's agenda. Outputs `memory` + `notify`: the brief is a
memory note **in quarantine** (3.1 invariant) and a notification on success.
`GET /api/helm/brief` previews it (one's own: any member; someone else's or a team's: the
managers).

Agenda: interface `helm_calendar.CalendarSource`; implemented: **ICS URL**
(`ICSUrlSource`: https only, private/loopback addresses refused unless
`SOKKAN_HELM_CALENDAR_ALLOW_PRIVATE=1`, 10 s, 2 MB, DAILY/WEEKLY recurrences with BYDAY,
INTERVAL, UNTIL, COUNT, EXDATE). The private ICS address is a credential: it lives in the
vault, referenced **by name** — per person (`PUT /api/helm/calendar {kind: "ics", secret}`)
or for the instance (`SOKKAN_HELM_CALENDAR_ICS`). Microsoft Graph (`GraphSource`) comes with
the `teams` feature (3.4).

## Board MCP (sessions drive the hierarchy)

`create_card(parent_id=…)`, `update_card(parent_id=…)` (0 = top level), `link_card(kind="mr")`,
and two reads: `get_card_tree(card_id)` (parents with their context, children by column,
computed progress, the context block) and `morning_brief(person, team)`.

## Variables

| variable | default | |
|---|---|---|
| `SOKKAN_FEATURE_HELM` | edition | the feature |
| `SOKKAN_HELM_TICK_S` | 900 | period of the job (min 60) |
| `SOKKAN_HELM_DRIFT_MIN` | 0.25 | cosine threshold of the drift detector |
| `SOKKAN_HELM_SNOOZE_DAYS` | 7 | an ignored / approved suggestion is not proposed again |
| `SOKKAN_HELM_CALENDAR_ICS` | — | vault secret NAME holding the team's ICS address |
| `SOKKAN_HELM_CALENDAR_ALLOW_PRIVATE` | 0 | let the calendar fetch reach a private address |

## Limits (3.3)

* The contradiction detector is lexical (negation patterns), the drift detector depends
  on the embedding engine's quality on short card texts; both only propose.
* Costs are estimations (sessions: API price grid of their transcripts; runs: their cost
  basis). Budgets per project come with lot 4.
* A merge request link is checked for its form only until the GitLab lot (5).
