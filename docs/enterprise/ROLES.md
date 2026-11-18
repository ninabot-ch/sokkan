# Job kits of SOKKAN Atelier (« Postes »)

4.0. Part of SOKKAN Atelier (feature `simple_profile`, [SIMPLE-PROFILE.md](SIMPLE-PROFILE.md)): no
switch of its own. A person who arrives picks their job and finds, from the first minute, work ready
to start, a team memory to fill in and the guard-rails of that job.

## What a kit holds

Data, not code: one file per job in `backend/roles/*.json`, one brief per type of deliverable in
`backend/roles/briefs.json`, checked by `backend/roles.py` (`python3 backend/roles.py check`).

| Job (`id`) | Templates | Level of its project | Claude | Notes to fill in |
|---|---|---|---|---|
| Executive assistant (`executive-assistant`) | 7 | team | possible | 3 |
| Human resources (`hr`) | 8 | **confidential** | **never** | 4 |
| Finance and accounting (`finance`) | 8 | project | **never** | 4 |
| Sales (`sales`) | 8 | team | possible | 4 |
| Customer support (`customer-support`) | 8 | team | possible | 4 |
| Marketing and communication (`marketing`) | 8 | team | possible | 4 |
| Legal and compliance (`legal`) | 8 | **confidential** | **never** | 3 |
| Project manager (`project-manager`) | 8 | team | possible | 4 |
| Training (`training`) | 8 | team | possible | 4 |

Every job has, in French and English (the person's language; Swiss French-speaking tone, formal
« vous », Swiss formats):

* **task templates** (6-10): what to attach, what the agent produces, the format of the deliverable
  (`docx` = a Markdown file downloaded as Word or PDF, `xlsx` = a Markdown source with `format: xlsx`
  — tables, totals as formulas — that `backend/outputs.py` previews computed and renders as Excel,
  `md` = text), the points a human checks, and the mission given to the agent;
* **starter notes** (3-5): the house charter, tone, scales and templates of the job, written into the
  project's memory as notes « to fill in with your team » — every blank is marked
  « [À compléter : …] » / « [To fill: …] », the note carries `status: to-fill`, and the agent never
  fills a blank by guessing: it lists what is missing under « Points à vérifier »;
* **guard-rails** (2-8): a sentence the person reads, and the rule the agent is given. HR and legal:
  personal or secret data → the job's project is classified *confidential*, so its work never goes to
  Claude (rule D9, by the project's level — 4.0.1: a job itself no longer refuses Claude; personal data
  in any other project goes through the « PII sandwich », [CLAUDE-PRIVACY.md](CLAUDE-PRIVACY.md));
  finance: every figure is quoted from its document and line, every computation shown, never an
  invented figure; support: no customer data outside the reply to that
  customer; marketing: no claim that cannot be proven from the documents; legal: never a legal
  opinion, a preparation for a specialist.

## The deliverable brief

Each template names a type of deliverable (`letter`, `memo`, `report`, `plan`, `table`, `checklist`,
`minutes`, `short`, `procedure`, `review`). Its brief — structure in order, length, how sources are
cited, what is listed « to check » — is added to the template's mission, followed by the job's rules
and the closing « Points à vérifier » checklist (for a table: listed in the message, not in the file).

The mission travels inside the work's first message (`simple.brief`, the text of the turn), which is
what every engine of the harness contract receives (`Harness.send`): the legacy CLI and the kernel get
the same words; nothing depends on an engine's own system prompt.

## Mailbox and calendar: no false promise

A template whose work would naturally end in a mail or an invitation (reply to a letter, incoming
mail digest, meeting agenda, payment reminder, quote follow-up, complaint reply, training
invitation) declares it: `"uses": ["mail"]` / `["mail", "calendar"]` in its JSON. At the start of
the work, `roles.connector_rules()` adds to the mission what the connectors
([CONNECTORS.md](CONNECTORS.md)) really allow this person in this project
(`connectors.service.capabilities`): **off** (feature off, nothing connected, or the project's
content may not reach this model) = the agent prepares the text and says the person sends it,
never « sent »; **read** = a draft in the mailbox / a calendar read, the person sends; **act** = a
draft and a send or event proposed on a card, nothing leaves before the person's approval. The
home shows the same state under the chosen template (« Messagerie et agenda »).

## Choosing a job

* **First sign-in in the Atelier**: ONE choice of job. For a person who has not been welcomed yet
  (and an administrator during the setup), the onboarding asks it ([ONBOARDING.md](ONBOARDING.md):
  job → tour → first work on an example document of that job) and writes it here as the person's
  first job; closing it without a job = « Plus tard ». Otherwise the picker « Quel est votre poste ? »
  opens once (several jobs possible). « Plus tard » stores an empty choice: not asked again, the four
  general templates stay. Not offered on the public demo nor to a viewer.
* **Settings › My jobs**: the same picker, any time.
* **A project per job**: an instance admin (feature `multi_project` on) ticks « Créer un projet pour
  chaque poste choisi »: one project per newly chosen job, named after it, classified at least at the
  job's level, the admin as its admin, the starter notes in its memory, the job remembered as the
  project's own. A project admin can set ONE job up in the current project instead. A person without
  either right is told to ask the administrator.
* **An admin's default job**: Settings › People's jobs (instance admin) — the person's home opens on
  that job's templates; they can add others.

The home lists the templates job by job — the project's own job first, then the person's other jobs,
then the general templates — each with its format; a chosen template shows what to attach, what the
agent produces and what you check. In a job that keeps its data local, the engine choice is not shown
and a sentence says why.

## API

    GET  /api/simple/roles?lang=fr|en           catalog, the caller's jobs, may they create / set up here
    POST /api/simple/roles/{id}/setup           {project: "" | slug, lang} → {project, name, created, notes, level}
    GET  /api/simple/roles/people               instance admin: every account with its jobs
    PUT  /api/me/prefs                          {roles: [...]}  ([] = « later », unknown id = 400)
    PUT  /api/admin/prefs/{email}               {roles: [...]}  an admin's default (journal `roles.set`)
    GET  /api/simple/home                       + roles, project_role, ask_roles; kit templates first
    POST /api/simple/start                      playbook = "<job>.<template>" (Claude: by the project's level and the PII check, never by the job)
    GET  …/deliverables/{name}/download?format=xlsx|docx|pdf|csv   rendered by backend/outputs.py

Journal: `roles.set`, `roles.setup`, `project.create`, `project.level` (a job raises a project's level,
never lowers it).

## Adding or changing a job

Drop a `<id>.json` (same shape as the built-in files) in the folder named by `SOKKAN_ROLES_DIR`; it
is read after the built-in ones (same id = replaces it), re-read when a file changes. An invalid
file is refused with its name and the field (`roles.CatalogError`), the home then shows the general
templates only (logged). Note names must be unique across jobs.

## Tests and real runs

* `tests/test_roles.py` (catalog, i18n, choice, admin default, setup, start, Claude refusal, table
  export), `tests/nav/simple_roles_test.ts` (grouping, local-only, the picker's plan).
* `tests/roles/real_runs.py` plays each fixture of `tests/roles/fixtures/<job>.<template>/` (fictional
  documents, the facts that must appear, the minimum rows of a table) on a real engine and model,
  through the harness contract, as the Atelier starts a work, and grades the deliverable:
  `PARITY_UPSTREAM=http://<model>/v1 python tests/roles/real_runs.py --out <dir>`.
