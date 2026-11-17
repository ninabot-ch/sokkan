# Getting started and everyday administration (4.0)

From « installed » to « the team's first validated deliverable » without opening `.env`, and the
administrator's everyday screens — all in the Atelier (simple profile). Code:
`backend/onboarding.py` (routes, store), `backend/onboarding_samples.py` (starter positions and
their example documents), `frontend/components/simple/onboarding/`, sentences in
`frontend/lib/simple/dictOnboarding.ts`.

No feature switch and no new variable: the screens read the features they rely on
(`multi_project`, `documents`, `project_vault_budgets`) and say in words when one is off.

## The administrator's first launch

An **instance** administrator (IAM role `admin` or `owner`) who opens the Atelier while the setup
is not finished lands on it; the drawer then shows « Getting started · n/6 » under
*Administration*, the home page a « Resume » banner. Six steps, each one skippable, kept on the
server (`$SOKKAN_DATA_DIR/onboarding.json`) — closing the browser and coming back resumes at the
first step still to do:

| Step | What it does | Where it is written |
|---|---|---|
| Organisation | name, team language (offered to the people invited) | `instance.json`, `onboarding.json` |
| Model | this machine's model (when `SOKKAN_SHIP_URL` is set) / SOKKAN Inference (token) / the client's own key (Anthropic, or any compatible address + model). **A one-token test runs first; nothing is saved unless it answers.** | `llm.json` (sealed) |
| Projects | name + classification in plain words, with « Claude possible » / « never sent outside » per level; the administrator is added to the project | `projects.db` |
| People | name, e-mail, projects (role `dev`), administrator or not; local auth: an access code shown once **or** an invitation link (7 days, single use); SSO: the grants only | `iam.db`, `projects.db`, `onboarding.json` (hash of the link only) |
| Documents | drop files into a chosen project | documents store |
| First work | pick a position → a first ask + an example document of that trade, started in the chosen project | session + documents |

An instance that already has works when an administrator first opens the Atelier (an upgrade) is
marked as set up (`preexisting`) and never sent through it. *Getting started › Run again* reopens
it on purpose.

The time from the first opening of the setup to the first `deliverable.validate` of the journal is
shown at the end (`GET /api/onboarding` → `ttfv.seconds`).

## A person's first sign-in

Once per person (`welcomed`): their **position** (a list from `GET /api/onboarding/roles`), a
four-card **tour**, then a **first work** of their trade on an example document added to their
project (once per person and project). Everything can be skipped (« Later »).

The positions come from the catalogue of positions when it is installed: a module
`roles_catalog` with `catalog(lang) → [{id, label, description, …}]` and, optionally,
`sample(role_id, lang) → {ask, doc: (name, text)}`. Without it, the seven starter positions of
`onboarding_samples.py` (management, administration & HR, accounting & finance, sales & customer
care, communication & marketing, training & teaching, another position). The chosen position is
kept per person (`onboarding.json`, shown in Team).

## Invitations (local auth)

`POST /api/team` with `via: "link"` creates the account and returns
`<SOKKAN_PUBLIC_URL>/?invite=<token>`. Opening it shows « You are invited » → *Enter SOKKAN*
(`POST /api/auth/invite`): the person is signed in **and** shown their personal access code once,
to keep for the next visits. The link works once and for 7 days; a used or expired link says so
and the code form stays available. Only a SHA-256 of the token is stored. A new link (Team ›
*New link*) replaces the previous one; deactivating the person drops it.

## Administration (Atelier › Administration)

* **Team** — everyone once (IAM users, people with a code, people with a project grant, pending
  invitations): name, e-mail, position, projects and roles, how they sign in, last sign-in.
  Add, edit projects (*Read · Contribute · Lead · Administer*), make administrator, *New code*,
  *New link*, *Deactivate* (immediate: open pages and running works are cut, nothing is
  deleted — `revocation.revoke`) and *Reactivate*. The owner (`SOKKAN_OWNER_EMAIL`) and yourself
  cannot be deactivated.
* **Usage** — per person (owner of the session) and per project, 7 / 30 / 90 days, the Costs
  estimate (public price list, not an invoice; a local model costs no tokens). With
  `project_vault_budgets` on: each project's daily / monthly ceiling, its gauge, and an alert at
  80 % (warning) and 100 % (new exchanges of the project stop). Off: the organisation's daily
  ceiling (`instance.json` `budget_day_usd`) with the same alerts.
* **Validations** — the journal of human validations (`GET /api/hitl/journal`): decision, what
  was validated, who decided, whose work, project, when; filters and CSV export.

## Errors in words

The Atelier says in its own sentence (and keeps the technical line behind « Details »):
`model_quota` (402, credit used up), `model_rate` (429), `model_unreachable` (no answer,
connection refused), `model_auth` (401/403, key refused), `sandbox_unavailable` (the 4.0 engine
cannot open the work's sandbox). Mapping in `backend/plain.py` (`_human_error`), sentences in
`frontend/lib/simple/dict.ts` (`no.*`).

## API

| Route | Who | |
|---|---|---|
| `GET /api/onboarding` | signed in | setup state (admins), own progress, auth mode, model state, counts, ttfv |
| `PUT /api/onboarding/setup` | instance admin | `{step, state: done\|skipped\|todo}` · `{finish}` · `{reopen}` |
| `PUT /api/onboarding/org` | instance admin | `{org_name, lang}` |
| `POST /api/onboarding/model/test` · `POST /api/onboarding/model` | instance admin | `{kind: local\|sokkan\|key\|custom, key, base_url, model}` — the save tests first |
| `GET/POST /api/onboarding/projects` | instance admin | `{name, level}` |
| `GET /api/onboarding/roles` · `PUT /api/onboarding/me` · `POST /api/onboarding/sample` | signed in | positions, own progress, example document |
| `GET/POST /api/team` · `PATCH /api/team/{email}` · `POST /api/team/{email}/code\|disable\|enable` | instance admin | |
| `GET /api/team/usage` · `PUT /api/team/budget` | instance admin | |
| `POST /api/auth/invite` | anyone with a live link | throttled (10 failures / min / IP) |

Every change is journaled (`onboarding.*`, `instance.rename`, `llm.test`, `llm.config`,
`project.create`, `iam.invite`, `iam.access_code.create`, `user.revoke`, `auth.login` `invite`…).
