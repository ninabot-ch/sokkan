# Multi-user — spec (3.2)

Status: **3.2 in progress.** Lot 1 (data model, "default project" migration, memory recall
scoped by project) and lot 2 (scheduler guard, secrets by name by default, 8 h cockpit
sessions) are implemented on branch `v3.2-multiuser`; everything else on this page is the
design the next lots implement. Nick's decisions of 07.10.2026 are folded in (see the end). This page is the contract: when code and page disagree,
fix one of them.

## Why

Until 3.1 a SOKKAN instance is one team on one perimeter: every user sees every session,
card, agent, secret and memory note; roles (`viewer < dev < admin < owner`) are
instance-wide. A large organisation (first case: a public broadcaster with many units —
developers, devops, system engineers, DBAs, QA) needs **several teams on one instance**,
each on its own perimeter, with rights it already manages elsewhere: in its identity
provider and in its forge. SOKKAN must not become a second place where those rights are
typed by hand.

## Principles

1. **Identity and teams come from SSO.** OIDC login; the IdP's `groups` claim gives the
   teams (Entra ID, Authentik, Keycloak…). SOKKAN keeps no password and no team list of its
   own for SSO users.
2. **A project is a perimeter**: one or more repositories + resources (database, cluster,
   host…) + its CortHeXis memory + its Crew agents + its vault secrets + its budgets + its
   board.
3. **Rights come from where they already live.** A project with repositories takes them
   from the **forge**, read with the person's own forge account (GitLab first: Reporter /
   Developer / Maintainer, protected branches, inherited group membership). A project without
   repositories takes them from an **SSO group**.
4. **A session pushes with the person's token**, never a technical account: the forge
   refuses what the person may not do. SOKKAN's own checks are a convenience in front of
   that, not the guarantee.
5. **Everything is partitioned by project**: memory (spawn recall, per-turn recall,
   `memory_search`, quarantine), agents, secrets, budgets, board. **The real leak risk is
   the memory**: a person without access to project X never receives a note of X, by any
   channel. An agent has its owner's rights on its project, nothing more.
6. **Fail-closed.** Unknown project, unknown session, expired forge read, invalid name →
   no access / no recall. Never "everything".
7. **No loss on upgrade.** A 3.1 instance becomes one project, `default`, whose rights are
   the instance roles: nothing moves, nobody gains or loses anything.

## Data model

### `projects.db` (new, `$SOKKAN_DATA_DIR/projects.db`, SQLite WAL) — lot 1

| Table | Purpose | Key columns |
|---|---|---|
| `projects` | the perimeters | `slug` (`[a-z0-9][a-z0-9-]{0,62}`), `name`, `access_source` (`instance` \| `sso_group` \| `forge`), `budget_day_usd`, `budget_month_usd`, `created_by`, `archived_at` |
| `teams` | IdP groups (`sso:<group>`) or local teams (`local:<slug>`) | `id`, `name`, `source`, `external_id` (Entra object id), `synced_at` |
| `team_members` | who is in a team — replaced at each login from the `groups` claim | `team_id`, `email`, `synced_at` |
| `project_grants` | role of a user or a team in a project | `project`, `principal_kind` (`user` \| `team`), `principal`, `role` (`viewer` \| `dev` \| `maintainer` \| `admin`) |
| `project_repos` | the project's repositories | `provider` (`gitlab` \| `github` \| `gitea` \| `bitbucket` \| `azure-devops`), `base_url`, `repo_path`, `external_id`, `default_branch` |
| `project_resources` | non-secret description of a resource; its credentials are **vault names** of the project | `kind`, `name`, `config` (JSON, never a secret), `secret_names` |
| `forge_links` | a person's forge account (lot 5) | `email`, `provider`, `base_url`, `forge_user_id`, `token_enc`, `refresh_enc` (Fernet), `scopes`, `expires_at`, `revoked_at` |
| `access_cache` | resolved role per (person, project), positive **and negative** | `role` (NULL = no access), `source`, `computed_at`, `expires_at` |
| `meta` | `schema_version` | |

### Columns added to the existing stores — lot 1

| Store | Column | Default (existing rows) |
|---|---|---|
| `board.db` `sessions` | `project TEXT NOT NULL` | `'default'` |
| `board.db` `cards` | `project TEXT NOT NULL` | `'default'` |
| `agents.db` `agents` | `project TEXT NOT NULL` | `'default'` (a run inherits it) |
| memory store `notes` (Postgres, migration `0011_note_project.sql`) | `project text NOT NULL` + index | `'default'` |

Lot 1 adds the columns everywhere so the data is in place; only the **session** and the
**note** columns are read in lot 1 (the memory scope). Cards and agents are filtered by
project from lot 3. The vault (`vault.json`) is namespaced in lot 4
(`{"projects": {"<slug>": {NAME: token}}, "instance": {…}}`, migrated in place: existing
secrets → `default`).

### Memory: one directory per project

The `.md` notes stay the source of truth. **One project = one memory directory**:
`default` keeps today's `SOKKAN_MEMORY_DIR` (nothing is moved); a new project gets
`$SOKKAN_DATA_DIR/projects/<slug>/memory/`. The indexer writes `notes.project` from the
directory it read (`IndexConfig.project`, `CORTHEXIS_MEMORY_PROJECT`; lot 1 indexes the
default directory only and never prunes notes of another project). A frontmatter `project:`
field was rejected: a note's place on disk is what a session can `Read`, so the directory
must be the boundary (see "Channels" below), and each directory gets its own `MEMORY.md`.

Note names are **unique per project** (decision of 07.10): with names unique per instance, a
refused `memory_write` ("name already used") would reveal that a note of that name exists in
a project the writer cannot see. Store change in lot 3 (migration `0012`): `notes` unique on
`(project, name)` instead of `name`; `links`, `note_versions` and `recall_log` gain `project`
(existing rows → `default`); `[[wikilinks]]`, quoted names and `memory_get(name)` resolve in the
session's project first, then in `shared` (below). Lot 1's `notes.name UNIQUE` is still in
place and harmless while only `default` exists.

### The `shared` project (3.2)

A project created by the migration of lot 3, **read-only for everyone** who can log in
(conventions, infrastructure runbooks, glossary). Every session's recall scope is
`(its project, "shared")`. Writing to it takes an explicit `maintainer` grant (typically the
instance admins, audited); `memory_write` from a session of another project never writes
there. Agents never write there; their quarantine approvals go to their own project.

## Sources of rights and their resolution

```
effective_role(person, project) =
    project.access_source == instance   → instance role (viewer→viewer, dev→dev, admin|owner→admin)
    project.access_source == sso_group  → best of grants(user) ∪ grants(teams of the person)
    project.access_source == forge      → best of grants (explicit overrides)
                                          ∪ access_cache row if fresh (forge read)
    archived / unknown project          → none
```

* **Instance roles stay** and change meaning: `owner` / `admin` administer the instance
  (IdP mapping, teams, projects, BYOK keys, Operate, audit); `dev` / `viewer` only matter in
  the `default` project (access source `instance`). **An instance admin has no implicit
  access to a project's content** (decision of 07.10): they add themselves to the project,
  which is audited (`project.grant` by and for the same person, flagged in the log).
* **SSO groups.** At each OIDC login the `groups` claim replaces the person's `sso:*`
  memberships (`projects.sync_sso_groups`, lot 3). Entra ID sends group object ids (or
  names with the "cloud-only group names" option, and an overage link above 200 groups → lot 3
  reads `/me/memberOf` through Graph when the claim is overflowed). Authentik sends names.
  An admin maps a group to a project role in the admin screen.
* **Forge (GitLab first, lot 5).** For each repository of the project, the person's access
  is read **with their own OAuth token** (`GET /projects/:id/members/all/:forge_user_id` →
  `access_level`, inheritance from groups included), mapped:

  | GitLab | SOKKAN project role | What the session may do (the forge enforces it) |
  |---|---|---|
  | Guest (10), Reporter (20) | `viewer` | read-only session (below) |
  | Developer (30) | `dev` | branches, push to unprotected branches, merge requests |
  | Maintainer (40) | `maintainer` | + protected branches (as the forge's rules allow), merge |
  | Owner (50) | `admin` | + project settings in SOKKAN |

  A project with several repositories: the role is the **lowest** level over its
  repositories for writing (a Developer on repo A and Reporter on repo B is `viewer` on the
  project; pushes to A still work because the forge decides per push) — decision of 07.10.
  Protected branches are listed (`GET /projects/:id/protected_branches`) only to grey out
  actions in the UI; the push itself is the authority.
* **Abstraction.** `forge.Provider` (lot 5): `authorize_url()`, `exchange(code)`,
  `refresh(link)`, `whoami(link)`, `access_level(link, repo) → project role | None`,
  `protected_branches(link, repo)`, `git_credentials(link) → (username, token)`. GitLab is
  implemented; GitHub (collaborator permission `read|triage|write|maintain|admin`),
  Gitea/Forgejo (`/repos/{o}/{r}/collaborators/{u}/permission`), Bitbucket (repository
  permissions) and Azure DevOps (Graph + Git security namespaces) are mappings of the same
  five methods.
* **Cache.** Positive rows 10 min, negative rows 2 min (`access_cache.expires_at`). Re-read:
  at login, when a session or an agent run of the project starts, on a push/API `403` from
  the forge, from the "Refresh my access" button, and by a sweep every 30 min for people
  with a live session. A forge that cannot be reached → the cached row until it expires,
  then **no access** (fail-closed), visible as such in the UI.

### Revocation (a person leaves)

| Event | Effect | Delay |
|---|---|---|
| Account disabled in the IdP | no new login; the SOKKAN session cookie dies at its TTL — lowered from 24 h to **8 h** and checked against the IdP at each refresh (lot 6) | ≤ 8 h, or immediate with SCIM / back-channel logout |
| SCIM deprovisioning (Entra ID, Authentik) → `DELETE /scim/v2/Users/{id}` (lot 6) | user disabled, sessions closed, agents paused, tokens erased | immediate |
| Removed from the IdP group | membership gone at next login / SCIM group push | next login or immediate (SCIM) |
| Removed from the GitLab project / user blocked | next forge read fails or returns no access → `access_cache` negative, live sessions of that project closed; token refresh fails → `forge_links.revoked_at`, token erased | ≤ 10 min (cache) |
| Admin "Revoke now" (lot 6) | close the person's sessions, pause their agents (an agent never outlives its owner's access: the scheduler checks the owner's role before each run), erase forge tokens, purge `access_cache` | immediate |

## Role × action matrix

Project roles; "inst. admin" = instance `admin`/`owner` acting on the instance, not on a
project's content.

| Action | viewer | dev | maintainer | admin | inst. admin |
|---|---|---|---|---|---|
| See the project, its sessions' transcripts, board | ✓ | ✓ | ✓ | ✓ | — (explicit grant) |
| Open a session | read-only ¹ | ✓ | ✓ | ✓ | — |
| Push / branches / merge requests | — | forge decides | forge decides (+ protected) | forge decides | — |
| Terminal (tmux) session | — | ✓ ² | ✓ ² | ✓ ² | — |
| Board: create, move, comment, close cards | — | ✓ | ✓ | ✓ | — |
| Board: archive cards, edit tags | — | — | ✓ | ✓ | — |
| Agents: read deck / runs | flag ³ | own (+ all with flag) | ✓ | ✓ | — |
| Agents: create / change (→ approval) | — | own | ✓ | ✓ | — |
| Agents: approve (`owner` / `admin` / `four_eyes`) ⁴ | — | own (mode `owner`) | ✓ | ✓ | — |
| Memory: recall, `memory_search` / `memory_get` | ✓ | ✓ | ✓ | ✓ | — |
| Memory: `memory_write`, CortHeXis curation actions | — | ✓ (curation = proposal) | ✓ | ✓ | — |
| Memory: approve quarantine, delete notes | — | — | ✓ | ✓ | — |
| Secrets: names | — | ✓ | ✓ | ✓ | instance secrets |
| Secrets: use in a session (`named`) / an agent | — | ✓ | ✓ | ✓ | — |
| Secrets: set / rotate / delete values | — | — | ✓ | ✓ | instance secrets |
| Budgets: see spend | — | ✓ | ✓ | ✓ | all projects |
| Budgets: set project budgets | — | — | — | ✓ | instance caps |
| Project settings: repos, resources, grants | — | — | — | ✓ | create / archive projects |
| Operate (incidents of the project's agents) | read | ack | resolve | resolve | — |
| Operate (infrastructure, alert routing) | — | — | — | — | ✓ ⁵ |
| Teams, IdP mapping, BYOK keys, audit log | — | — | — | — | ✓ |

¹ Read-only session: tools `Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, memory and board
reads; no `Write`/`Edit`/`Bash`; no secrets; its push would be refused by the forge anyway.
² **Raw terminal sessions only in `default` until the sandbox exists** (decision of 07.10):
a terminal reads the shared memory directory and `.mcp.json` (see "Channels"); other
projects get them only with lot 8. Enforced since lot 1 (`POST /api/spawn` → 400).
³ `SOKKAN_CREW_VIEWER_READONLY`, per project from lot 3.
⁴ `four_eyes`: approver ≠ proposer ≠ owner **and** role ≥ maintainer in the agent's project.
⁵ And the members of the **ops team** (decision of 07.10): an SSO group named in the
admin screen (`SOKKAN_OPS_GROUP` as the bootstrap value) gets the infrastructure Operate tab —
read, ack, resolve, alert routing — without any project content.

## Memory isolation

### Channels through which a note can reach a session

| # | Channel | Status |
|---|---|---|
| 1 | Spawn pre-seed (`_memory_preseed`, agent runs included) | **lot 1** — scoped to the session's project |
| 2 | Per-turn recall (`UserPromptSubmit`) and sub-agent recall (`PreToolUse` Task/Agent), SDK sessions in-process | **lot 1** |
| 3 | Same hooks for terminal sessions: `POST /api/memory/hook` (scope from the session id) and the in-process fallback (`CORTHEXIS_RECALL_PROJECTS`, else `CORTHEXIS_RECALL_REQUIRE_SCOPE=1` → nothing) | **lot 1** |
| 4 | MCP `memory_search` / `memory_get` / `memory_links` / `memory_write` (scope = `SOKKAN_SESSION_PROJECT`, set by the API) | **lot 1** |
| 5 | A quoted note name (the recall forces a note whose name is in the message) | **lot 1** — a name of another project is never forced |
| 6 | Cockpit: `/api/memory/search` (lot 1, `?project=` + access check), notes list, note body, graph, CortHeXis review pairs, recall log, quarantine, runbooks, Nina's context (lot 1: default project) | **lot 3** for the rest |
| 7 | File system: Claude Code loads `MEMORY.md` of the workspace, and `Read`/`Bash` can open any note file the API user can read | **lot 3** per-project workspace + memory directory; **lot 8** sandbox (per-project uid / container) for a hard boundary |
| 8 | Agent deliverables (note `agent-<name>-latest`, quarantine) | **lot 3** — written to the agent's project directory |
| 9 | IDF statistics (`lex_df`) are instance-wide | accepted: a word's document frequency, not content |

Until lot 3 ships, **no second project can be created through the API or the UI**
(`projects.create` is reachable from Python only): every unscoped surface of row 6-8 then
only ever serves the `default` project, so lot 1 opens no hole.

### How the recall filters (lot 1)

1. A session carries a project (`sessions.project`, set at spawn; an agent run takes its
   agent's). `projects.session_scope()` turns it into a **scope** — a tuple with that one
   project. Unknown session → `("default",)` while the instance has one project, `()` (no
   recall) as soon as it has several. Unknown or invalid project → `()`. From lot 3 the
   scope is `(project, "shared")`.
2. The store applies the scope **at every stage** of the search (`Store.search(projects=…)`):
   dense candidates (`WHERE note_id IN (notes of the scope)`, HNSW with
   `hnsw.iterative_scan = relaxed_order` on pgvector ≥ 0.8 so a filtered probe still fills
   its candidates), lexical candidates, and a **final guard** on the joined note row. A
   search without a scope runs the 3.1 SQL unchanged.
3. The recall (`core.recall.Recaller`) filters again what the store returns (a store that
   ignores the scope cannot leak), asks `existing_names(…, projects=scope)` for quoted
   names and re-checks the note's project before forcing it.
4. `memory_get` / `memory_links` answer "not found" for a note outside the scope — the same
   answer as for a note that does not exist. The 2.x SQLite index (migration in progress)
   has no project: it counts as `default`, so a session of another project gets nothing
   from it.
5. Tests: `tests/test_recall_scope.py` (every channel above, stores that honour and that
   ignore the scope), `tests/test_recall_scope_pg.py` (real Postgres: exact, HNSW and
   lexical-only paths, quoted names, recall log, migration 0011, a note moving between
   projects). Both go red when the filters are removed (checked).

## Secrets, budgets, board, agents

* **Secrets.** `SOKKAN_SESSION_SECRETS=named` is the default since lot 2 (announced in the
  3.1 changelog; `all` stays available for single-team installs, set explicitly; an unknown
  value means `named`). Upgrade note: a 3.1 install that did not set the variable gets a
  start-up message; sessions opened before the upgrade get no secret after a restart (pick
  them again, or set `SOKKAN_SESSION_SECRETS=all`). Vault namespaced per project
  + an `instance` namespace (model keys, Operate integrations) that sessions never receive.
  A project resource's credentials are vault names of that project.
* **Budgets.** Per project (day / month, hard stop like the per-session budget) and per
  person; the instance caps stay above. Spend reports by project, person, agent.
* **Board.** One board per project (`cards.project`); `sokkan-board` MCP sees the session's
  project only (same env as the memory server). Cross-project links are refused.
* **Agents.** `agents.project`; the deck shows the selected project; a run's session, recall,
  secrets and board writes are the agent's project's; before each run the scheduler checks
  that the owner still has ≥ dev in the project (else: run skipped, agent paused, owner and
  project admins notified).

### Scheduler guard (two incidents of 07.10.2026) — lot 2 ✅

Twice on 07.10 an instance started on a data directory holding a `queued` run started that
run at boot with the credentials it found: the host's Claude CLI login. Since lot 2:

* **Explicit credentials only** (`llm.unattended_credentials()`): the cockpit's model
  settings (`llm.json`: BYOK, custom gateway), the provisioned included inference, or
  `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` in the API's own environment. A CLI login
  under `CLAUDE_CONFIG_DIR` does **not** count, unless the operator says so with
  `SOKKAN_AGENTS_USE_CLI_LOGIN=1` (an instance that runs on a Claude subscription logged in
  with the CLI — such as our internal instance — must set it when it upgrades).
* **Boot without credentials**: runs left `queued` by a previous process are marked
  `skipped` ("queued before the restart; no model credentials…"); nothing is caught up.
* **Held scheduler**: while there are no credentials, nothing starts (`tick` and, in depth,
  `start_queued`); a schedule that falls due is moved to its next occurrence with **one**
  `skipped` run recorded (visible in History); an Operate alert records a `skipped` run;
  "Run now" (cockpit and `run_agent_now`) is refused with the reason. When credentials
  arrive, the scheduler resumes from the next occurrence: nothing that fell due meanwhile
  is replayed.
* With credentials, 3.1 behaviour is unchanged (queued runs resume, catch-up of one run
  missed less than `SOKKAN_AGENTS_MISFIRE_S` ago).
* The Crew tab shows "Scheduler stopped: no model credentials configured for this
  instance" (`GET /api/agents` → `scheduler: {running, held, reason}`). The public demo's
  simulator never calls a model and is not held.

### Cockpit session lifetime — lot 2 ✅

8 h instead of 24 h (`SOKKAN_SESSION_TTL_S`, bounded to 5 min … 24 h), counted from the
cookie's issue time with the limit **in force**: a 24 h cookie issued by 3.1 does not outlive
the new limit after the upgrade. A person disabled in the IdP loses the cockpit at the latest
8 h later (immediately with SCIM, lot 6).

## Sessions and pushes with the person's token (lot 5)

* The person links their forge account once (Profile → Forge accounts → "Link GitLab"):
  OAuth 2 authorization code + PKCE, `state` bound to the SOKKAN session. Scopes (GitLab):
  `read_user`, `read_api` (membership and protected branches), `read_repository`,
  `write_repository`. Not `api`: a merge request is opened by `git push -o
  merge_request.create`, which `write_repository` allows.
* Tokens are Fernet-encrypted with `forge.key` (separate from `vault.key`, 0600, rotation =
  re-encrypt), refreshed server-side, never shown, never logged, never stored in a session
  transcript.
* A session gets **git credentials only**: `GIT_ASKPASS` / a credential helper that asks
  the API (loopback, session-bound token) for a short-lived access token for the
  configured forge host. The token is the person's: a session can do at most what the
  person can, for at most the token's life (GitLab: 2 h). Prompt-injection exfiltration of
  that token is the residual risk (documented; mitigations: short life, minimal scopes,
  the sandbox of lot 8, egress filtering).
* No forge account linked → the session works on a clone that cannot push, and says so.
* An agent run pushes with its **owner's** token; if the owner's link is revoked or their
  access dropped, the run is skipped.

## Migration from a single-project instance

Automatic at the first start of 3.2, idempotent, nothing deleted or moved:

1. `projects.db` created with project `default` (access source `instance`, name = the
   organisation name).
2. `board.db`, `agents.db`: `project` column, default `'default'` → every session, card and
   agent is in `default`.
3. Memory store: migration `0011` → every note in `default`; the indexer keeps indexing
   `SOKKAN_MEMORY_DIR` as `default`.
4. Vault (lot 4): existing secrets → namespace `default`.
5. Behaviour: identical for projects, roles, recall (scope `("default",)` = every note),
   board and agents. The project selector appears only when a second project exists.
6. Lot 2 changes three defaults on purpose — check them when upgrading:
   * `SOKKAN_SESSION_SECRETS` → `named` (set `all` to keep the 3.1 behaviour);
   * the agent scheduler needs explicit model credentials (cockpit model settings or a key
     in the environment; a CLI login only with `SOKKAN_AGENTS_USE_CLI_LOGIN=1`);
   * cockpit sessions last 8 h (`SOKKAN_SESSION_TTL_S` to change it).

Rollback to 3.1: the added columns and `projects.db` are ignored by 3.1 (every 3.1 insert
names its columns); migration `0011` leaves a column 3.1 does not read.

## UX

* **Project selector** in the header (hidden while there is one project): projects the
  person can read, with their role; the choice is in the URL (`/?project=<slug>&tab=…`) and
  remembered per person. Every tab shows the selected project's data; a session's pane
  shows its project badge.
* **Admin → Teams & projects** (instance admin): projects (create, archive, access source,
  repositories, resources, budgets), teams (SSO groups seen at login, local teams), grants
  (group or person → role), "who has access to X and why" (the resolution, source by
  source), "Revoke now".
* **Profile → Forge accounts**: link / unlink GitLab (and later others), linked identity,
  scopes, last refresh, "Refresh my access".
* **Admin → Model keys (BYOK)**: the client's admin enters their Anthropic key (or gateway
  URL + token) in the cockpit; stored encrypted in the instance vault namespace, shown
  masked, test call, last use; never readable back. **Per instance for the POC**; a
  per-project override comes later (decision of 07.10).
* Crew, Board, CortHeXis: unchanged layouts, scoped to the selected project
  (`feedback-ui`: one card per agent, state visible at a glance, details in a popout).

## Security

* Tokens (forge, BYOK) encrypted at rest (Fernet, own key files, 0600), refreshed and used
  server-side; minimal scopes; erased on unlink, on revocation and on SCIM delete.
* Audit log entries (lot 3-6): `project.create|archive|grant|revoke`, `team.sync`,
  `forge.link|unlink|refresh_failed|access_changed`, `access.denied` (sampled),
  `session.spawn` with project, `memory.scope_violation` (a filter dropping a row the SQL
  should have excluded — must stay at zero), `byok.set|test`, `revoke.now`.
* Every write route checks the project role server-side; the UI only greys out.
* The scope given to an MCP server comes from the API's environment; no tool takes a
  project argument (`memory_search_server.search_scoped` is not an MCP tool).

## Out of 3.2

* Multi-tenancy inside one instance for **different organisations** (one instance per
  client stays the rule).
* Per-project sandbox as the default for every project (lot 8 is optional in 3.2: projects
  marked "sensitive").
* Bitbucket / Azure DevOps / GitHub providers (the interface is ready; GitLab ships).
* Fine-grained ACLs below the project (per card, per note, per folder).
* Cross-project agents; sharing between two given projects (only the `shared` project,
  readable by all, is in 3.2).
* BYOK per project (per instance in 3.2).
* LDAPS group sync (OIDC only in 3.2; LDAPS login keeps instance roles).

## Delivery plan

Ordered so that each lot is testable alone and the risky surface grows last; relative size
in points (1 point ≈ one focused session of work with its tests).

| Lot | Content | Risk | Size | Testable alone by |
|---|---|---|---|---|
| **1 ✅** | Data model (`projects.db`, `project` columns), "default project" migration, memory recall + MCP scoped by project, spawn with `project` (dev+ check), tests | low: dormant (one project) | 3 | unit + Postgres suites; behaviour identical on a 3.1 data dir |
| **2 ✅** | Scheduler guard (explicit credentials, no boot catch-up), `SOKKAN_SESSION_SECRETS=named` default, session cookie TTL 8 h | low | 1.5 | restart an instance with a queued run and no credentials → nothing runs |
| 3 | SSO groups → teams at login, ops team, admin screens (projects, grants, "why"), project selector, scoping of every cockpit route (sessions, board, Crew, CortHeXis, Operate links, Nina), per-project workspace + memory directory + `MEMORY.md`, note names unique per project (migration `0012`), the `shared` project, board MCP scope, `projects.create` exposed | **high** (turns multi-project on) | 7 | a second project with two people: none sees the other's sessions, cards, agents, notes (API + UI e2e) |
| 4 | Vault per project + instance namespace, project budgets and spend reports, agents' owner-role check before each run | medium | 3 | secrets of X never in a session of Y; budget stop per project |
| 5 | GitLab: link account (OAuth PKCE), `forge.Provider`, access resolution + cache, credential helper, push with the person's token, read-only sessions for Reporter | **high** (external system, tokens) | 6 | against a GitLab CE container: Reporter cannot push, Developer pushes a branch + opens an MR, Maintainer pushes a protected branch |
| 6 | Revocation: SCIM endpoint, "Revoke now", back-channel logout, audit entries | medium | 3 | SCIM delete → sessions closed, agents paused, tokens gone within a second |
| 7 | BYOK admin screen (client admin enters their keys) | low | 1.5 | key set, masked, test call, used by sessions |
| 8 | Optional sandbox per sensitive project (own uid / container for sessions) | high | 5 | a session of X cannot read X' files by `Bash cat` |

Lots 1-2 can ship in a 3.2 preview; lots 3-5 are the POC's "multi-user" criterion; 6-7 before
a production rollout at a large client; 8 if their security officer requires it.

## Decisions (Nick, 07.10.2026)

1. **Instance admin and project content**: no access without adding themselves to the
   project, which is logged.
2. **Several repositories in one project**: the project role is the **lowest** forge level
   over its repositories.
3. **Note names unique per project**, not per instance (a collision must not reveal that a
   note exists elsewhere) → migration `0012` in lot 3.
4. **A `shared` project**, read-only for everyone, in 3.2.
5. **Operate (infrastructure)** open to an **ops team** defined by an SSO group, in addition
   to the instance admins.
6. **No raw terminal outside `default`** before the sandbox (lot 8).
7. **BYOK per instance** for the POC; per project later.
8. **GitLab and Entra ID** to be confirmed at the client; the forge abstraction stays.
