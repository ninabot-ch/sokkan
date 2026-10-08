# Multi-user — spec (3.2)

Status: **3.2 in progress.** Lot 1 (data model, "default project" migration, memory recall
scoped by project), lot 2 (scheduler guard, secrets by name by default, 8 h cockpit
sessions) and lot 3 (projects turned on: every route scoped, SSO teams, admin screens,
selector, per-project memory, `shared`, ops team — see "Lot 3: what shipped") are
implemented on branch `v3.2-multiuser`; everything else on this page is the design the next
lots implement. Nick's decisions of 07.10.2026 are folded in (see the end). This page is the contract: when code and page disagree,
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
* **Abstraction** (`backend/forge/`, lot 5 ✅). `forge.Provider`:
  `authorize_url(state, code_challenge, redirect_uri)`, `exchange(code, verifier,
  redirect_uri) → Tokens`, `refresh(refresh_token, redirect_uri) → Tokens`, `revoke(token)`,
  `whoami(token) → Identity`, `access_level(token, identity, repo) → project role | None`,
  `protected_branches(token, repo)`, `git_credentials(token) → (username, password)`, and the
  pure `role_for(level)`. Errors: `ForgeUnavailable` (network, 5xx: keep the cache, never a
  revocation), `ForgeUnauthorized` (401, `invalid_grant`: revoke the link),
  `NotImplementedForge`. **GitLab** (self-hosted or gitlab.com, API v4) is implemented.
  **GitHub** (collaborator permission `read|triage|write|maintain|admin`) and
  **Gitea/Forgejo** (`/repos/{o}/{r}/collaborators/{u}/permission`: `read|write|admin|owner`)
  are skeletons: mapping defined and tested, every network method raises
  `NotImplementedForge` (contract tests: `tests/test_forge_contract.py`). Bitbucket and Azure
  DevOps: not started.
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

### Lot 6: what shipped (feature `revocation`)

* `backend/revocation.py`, state in `$SOKKAN_DATA_DIR/identity.db` (`account_state`,
  `scim_users`, `scim_groups`, `scim_group_members`; the file exists only once someone was
  provisioned or revoked).
* **One effect, four entry points.** `revoke(email)`: account disabled; every cockpit cookie
  issued before now refused (`session.email_from_request` → `cookie_ok`, and
  `auth.instance_user` answers 403 for a disabled account, whatever the auth mode); the
  person's WebSockets (chat panes, terminal) closed with 4401, and a message on a pane opened
  before is refused; agents they own paused, their queued / running runs cancelled, a
  notification sent; their live interactive SDK sessions interrupted then closed; forge tokens
  blanked (`forge_links.revoked_at`; revoked at the forge first when the lot-5 provider offers
  `forge.revoke_link`); `access_cache` purged; SSO team memberships removed; audit
  `user.revoke` with the counts. Entry points: SCIM `active=false` / DELETE, admin
  **Revoke now** (`POST /api/admin/users/{email}/revoke`, not on yourself nor the owner),
  `reinstate` (agents stay paused on purpose).
* **SCIM 2.0** at `/api/scim/v2` (bearer `SOKKAN_SCIM_TOKEN`, constant-time compare; 404 when
  the feature is off or no token is set): Users create / get / filter `userName|externalId eq`
  / PUT / PATCH (`active` as boolean or the string `"False"` Entra sends, path-less value
  objects) / DELETE; Groups create / filter `displayName eq` / PUT / PATCH add-remove members
  (`members[value eq "…"]`) / DELETE; `ServiceProviderConfig`, `ResourceTypes`, `Schemas`.
  A SCIM group is the team `sso:<displayName>` (`SOKKAN_SCIM_GROUP_KEY=externalId` for an
  Entra claim made of object ids); a member removed → `reconcile`. Renaming a group is refused
  (the name carries grants).
* **SSO login**: after the `groups` claim re-sync, `reconcile(email)` closes the person's live
  sessions in projects they no longer reach and pauses the agents they may no longer run; a
  disabled account gets 403 at the callback (`auth.login.refused`).
* **Scheduler**: before a run starts, `owner_may_run` — owner not disabled and `dev`+ in the
  agent's project — else the run is cancelled ("not started: …"), the agent paused, a
  notification sent. (Feature off: the 3.1 behaviour.)
* Tests: `tests/test_revocation.py` (each path on the real middleware; mutations — removing the
  403, the cookie check, the scheduler check, the login reconcile, the session stop or the
  group reconcile — each turn a test red).
* OIDC back-channel logout — shipped in 3.4 « Bridge »: `POST /api/auth/backchannel-logout`
  (`backend/oidc_logout.py`; token checks in `oidc.verify_logout_token`, effect
  `revocation.logout`). The cockpit cookie carries the id_token's `sid`; a logout token naming a
  `sid` ends that IdP session's cookies and WebSockets, and the person's live SDK sessions when
  no other IdP session of theirs is signed in; `sub` alone ends them all. Not a revocation
  (account, agents, forge tokens untouched). Entra ID has no back-channel: front-channel
  `GET /api/auth/frontchannel-logout?sid=` behind `SOKKAN_OIDC_FRONTCHANNEL_LOGOUT=1`, plus SCIM.
  Runbook: OPERATIONS.md § 2.2. Tests: `tests/test_oidc_logout.py` (fake IdP: RSA key + JWKS,
  real callback and middleware; every token check has a refusal case, mutations → red).

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
admin screen (`SOKKAN_OPS_GROUP` as the bootstrap value) gets the infrastructure sub-tabs Operate › Incidents and Operate › Infra —
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
| 6 | Cockpit: memory search, notes list, note body, stats, recall log, runbooks, quarantine, CortHeXis graph / note, Nina's context | **lot 3** — the selected project (+ shared); recall log = the project's own notes |
| 6b | CortHeXis review, proposals, curation (near-duplicate pairs, drift…) | **lot 3** — the review source reads ONE project (`PgSource(project=…)`): no pair across projects; the review itself runs for `default` only for now, other projects get an empty one |
| 7 | File system: Claude Code loads `CLAUDE.md` / `MEMORY.md` / `.mcp.json` of its working directory; `Read`/`Bash` can open any file the API user can read | **lot 3**: a session of another project works in `$SOKKAN_DATA_DIR/projects/<slug>/work` (nothing of `default` is loaded); raw terminals stay in `default`; agent runs may not Write/Edit under `$SOKKAN_DATA_DIR/projects`. **Lot 8** sandbox (per-project uid / container) for a hard boundary against `Read`/`Bash` |
| 8 | Agent deliverables (note `agent-<name>-latest`, quarantine) | **lot 3** — quarantine per project, approval into the agent's project directory |
| 9 | IDF statistics (`lex_df`) are instance-wide | accepted: a word's document frequency, not content |

Lots 1 and 2 kept project creation out of the API; lot 3 exposes it (admin screen) now that
every surface above is scoped.

### Lot 3: what shipped

* **One gate for every request** (`backend/projectgate.py`, called by the auth middleware): a
  route that names an object (session, card, agent, run) is about that object's project; a
  list or a creation is about the selected project (`x-sokkan-project` header, sent by the
  cockpit with every call); tmux / send / preview are `default`; Operate and Infra are for
  the ops team and instance admins. The person's role **in that project** replaces their
  instance role for the request (viewer → viewer, dev → dev, maintainer → admin, admin →
  admin), so every 3.1 check (`require("dev")`, Crew ownership, board rules) applies per
  project. No role there = 404. WebSockets take the role in the session's project.
* **MCP servers** get `SOKKAN_SESSION_PROJECT` (+ `SOKKAN_SESSION_SCOPE` = project,shared):
  memory (read the scope, write the project's directory), board (list / search / every card
  tool / links), agents (the project's agents only, role there).
* **Memory**: names unique per project (migration `0012`), one directory and one indexer
  per project, `shared` recalled with every project, quarantine per project.
* **Teams**: the OIDC `groups` claim (`SOKKAN_OIDC_GROUPS_CLAIM`, default `groups`; add the
  `groups` scope / Entra "groups claim" on the IdP side) replaces the person's SSO teams at
  each login. With `SOKKAN_DEFAULT_ROLE=none`, someone the instance does not list gets in
  only through a project grant.
* **Admin** (Setup › Organization › Projects & teams, `/api/admin/*`): projects, grants to a person or a
  team, ops group, teams seen; an instance admin sees no content until they grant themself
  (`project.grant.self` in the journal). **Selector** in the header (hidden with one project).
* **Not yet per project, fail-closed meanwhile**: the vault (other projects get no secret
  until lot 4), the CortHeXis review / proposals (default only), cost totals (instance-wide;
  the session list is filtered), the journal (instance admins only once there are several
  projects), agent names (still unique per instance: a name collision tells that an agent of
  that name exists elsewhere — low; to fix with lot 4's agents table rebuild).
* **Tests**: `tests/test_project_isolation.py` drives the real middleware with three people
  (dev of default, dev of radio through an SSO team, instance admin without grant): sessions,
  spawn, board (+ MCP), agents (+ MCP, a maintainer included), quarantine, memory routes,
  CortHeXis, usage, journal, Operate, admin, selector, WebSocket, workspace. Each filter was
  removed once and the suite went red (gate, session list, board, agents, memory notes,
  memory search, board MCP, quarantine, CortHeXis graph, usage list, review pairs).

### Lot 4: what shipped

Feature `project_vault_budgets` (registry; beta, on by default in the enterprise edition,
requires `multi_project` and `named_secrets`). **Off, every point below falls back to the
fail-closed lot 3 behaviour** — turning it off never opens data.

* **Vault per project.** `vault.json` = `{"format": 2, "projects": {slug: {NAME: token}},
  "instance": {}}`, migrated in place at the first read (flat 3.1 file → `default`, original
  kept once as `vault.json.v1.bak`). `vault.namespace(project)` is the only gate: `default`
  always, another project only with the feature, `shared` never (a secret every project
  could read would defeat the scope — a secret two projects need is set in both). Session
  env (`agentchat`), run start and redaction (`agents_runtime`, `agents.secrets_for_session`),
  the names a session / an agent may pick (`/api/vault/session`, agent forms, `sokkan-agents`
  MCP) and the admin screen (`/api/vault*`, now project-scoped: the project's admin or
  maintainer) all name the project. Names unique per (project, name).
* **Budgets per project** (`backend/budgets.py`, table `project_budgets` in `projects.db`):
  day and month ceilings in USD or CHF (compared through `SOKKAN_FX_USD_PER_CHF`). Spend =
  `usage.project_spend`: transcripts of the project's sessions (board mapping) and of its
  workspace (`$SOKKAN_DATA_DIR/projects/<slug>/work`; the instance workspace = `default`).
  80 % → one warning per session; 100 % → the session refuses new turns (HITL: a project
  admin raises the ceiling), a new session is told, an agent run ends `budget` before it
  starts. Operate › Costs: the selected project's totals, series, sessions, models + its budget.
* **Agents**: `agents` rebuilt once with `UNIQUE(project, name)` (ids kept, one
  transaction, `agents.db.pre-lot4.bak`); names resolved in the session's / card's project.
  Before each run the owner must still be dev+ in the agent's project, else the run is
  `skipped` and the agent `paused` (journal `agent.run.owner_lost`, owner notified).
* **CortHeXis review per project** (store 3.0 only): own corpus, own history
  (`$DATA/projects/<slug>/corthexis-review.db`), proposals tagged with their project (no
  `project` = `default`), curation sessions spawned in the project; no chain / bench checks
  and no alert outside `default` (instance-level).
* **Journal**: `events.project` (default = the request's project, or the MCP server's
  `SOKKAN_SESSION_PROJECT`); a project admin / maintainer reads their project's journal
  (`GET /api/audit` with the project header); instance admins read everything.
* **Tests**: `tests/test_project_lot4.py` (same three people + carol, maintainer of radio);
  each filter removed once went red (19 mutations).

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

### Lot 8: what shipped (feature `sandbox`)

A session or an agent run of any project except `default` reaches only its project's space:
`$SOKKAN_DATA_DIR/projects/<slug>/work` read-write (its working directory), the rest of
`projects/<slug>` and `projects/shared` read-only, plus the CLI's own files for that working
directory. `backend/sandbox.py`, wired in `agentchat.AgentSession.ensure_started`.

1. **SDK options**: `cwd` = the project workspace (lot 3), `add_dirs=[]`.
2. **PreToolUse hook** (matcher `Read|Glob|Grep|LS|NotebookRead|Write|Edit|MultiEdit|NotebookEdit|Bash`):
   it runs before every allow rule (SAFE_TOOLS, user settings, an agent's `auto_approve`). Paths
   (`file_path`, `notebook_path`, `path`, the static part of a Glob `pattern` / Grep `glob`) are
   made absolute against the workspace, `..` collapsed and symlinks resolved (`realpath`), then
   checked against the roots; a refusal is a `deny` with the reason, logged (stderr + audit
   `sandbox.deny`).
3. **Bash**: mode detected at start (`sandbox.detect`, exposed as `GET /api/features` →
   `sandbox`): `pod` when sessions run in their own pod (`SOKKAN_RUNNER=kubernetes`, the
   session runner — **the reference isolation** on Kubernetes / OpenShift restricted SCC, where
   bubblewrap cannot run), else `bwrap` when bubblewrap is present and a probe run works
   (opportunistic, compose / k3s host), else `hooks-only`.
   * `pod` → Bash runs in the pod unwrapped (the pod is the boundary); the hook still checks
     file tools.
   * `hooks-only` → Bash refused outside `default`.
   * `bwrap` → the hook rewrites the command to `<wrapper> '<command>'`; the wrapper
     (`$DATA/sandbox/sessions/<sid>.sh`, outside every mount) execs `bwrap --unshare-all
     [--share-net if SOKKAN_SANDBOX_NETWORK=1] --die-with-parent --new-session --clearenv`,
     /usr (+ /bin, /lib… links, a short list of /etc files) read-only, `--proc`, `--dev`,
     private `/tmp`, the project memory and `shared` read-only, the workspace read-write,
     `HOME` = workspace, env = PATH/LANG/TERM/TZ/USER (+ the session's vault names, values read
     at run time, never on a command line). A human still approves (`ask`); an agent's
     `Bash(x:*)` rule still approves (`allow`) when the command has no shell operator.
   * The permission callback re-checks / re-wraps the input that will really run (an edited
     approval cannot unwrap it), also in bypass mode.
4. Raw terminal: `default` only (decision 6, unchanged).
5. Tests: `tests/test_sandbox.py` (paths, symlinks, logging, bwrap: other project's file absent,
   shared read-only, no env secret, loopback only; `default` and feature-off unchanged) and
   `tests/test_sandbox_e2e_cli.py` (real CLI: an agent run's Read and `cat` of another project
   fail in both modes; the CLI runs the hook-rewritten command; `default` reads it).
   Mutations (no path check, no symlink resolution, no rewrite by both hook and callback) → red.
* **Limits**: outside `pod` mode, same uid as the API (no per-project uid / container); MCP servers run outside the
  sandbox (they are project-scoped since lot 3); in `bwrap` the shell sees /usr and a few /etc
  files; Docker's default seccomp refuses user namespaces → `hooks-only` in the stock compose
  (see docs/enterprise/OPERATIONS.md § 4.1).

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
* Build › Crew shows "Scheduler stopped: no model credentials configured for this
  instance" (`GET /api/agents` → `scheduler: {running, held, reason}`). The public demo's
  simulator never calls a model and is not held.

### Cockpit session lifetime — lot 2 ✅

8 h instead of 24 h (`SOKKAN_SESSION_TTL_S`, bounded to 5 min … 24 h), counted from the
cookie's issue time with the limit **in force**: a 24 h cookie issued by 3.1 does not outlive
the new limit after the upgrade. A person disabled in the IdP loses the cockpit at the latest
8 h later (immediately with SCIM, lot 6).

## Sessions and pushes with the person's token (lot 5)

* The person links their forge account once (Setup › My account › Linked accounts → "Link GitLab"):
  OAuth 2 authorization code + PKCE, `state` bound to the SOKKAN session. Scopes (GitLab):
  `read_user`, `read_api` (membership and protected branches), `read_repository`,
  `write_repository`. Not `api`: a merge request is opened by `git push -o
  merge_request.create`, which `write_repository` allows.
* Tokens are Fernet-encrypted — the vault's scheme — with their own key `forge.key`
  (separate from `vault.key`, 0600; losing or rotating it = people link again), refreshed
  server-side (GitLab rotates refresh tokens: one refresh at a time per link), never shown,
  never logged, never stored in a session transcript.
* A session gets **git credentials only**: through `GIT_CONFIG_COUNT`/`KEY`/`VALUE`, for each
  forge host of the project, the credential helper list is **reset** (a `store`/`cache`
  helper of the user's git config never receives the token) then
  `backend/forge/git_credential_helper.py` is set; `GIT_TERMINAL_PROMPT=0`. The helper asks
  `POST /api/forge/git-credential` (loopback only, refused through the proxy) with an HMAC
  ticket naming the session and its person (`SOKKAN_FORGE_TICKET`, not a token); the API
  checks the session is live and owned by that person, re-checks their project role, and
  answers the person's current token for that host only. git keeps it in memory for that one
  command; `erase` (git was refused) re-reads the person's access at once. The token is the person's: a session can do at most what the
  person can, for at most the token's life (GitLab: 2 h). Prompt-injection exfiltration of
  that token is the residual risk (documented; mitigations: short life, minimal scopes,
  the sandbox of lot 8, egress filtering).
* **Where the helper asks** (3.2.0, `forge.gitcred` transports, chosen by `agentchat` per
  session):

  | Session runs… | Channel | Who is asking (fixed by the api) |
  |---|---|---|
  | in the api (local runner) | `POST /api/forge/git-credential`, loopback only | ticket + live session |
  | in a session container / pod (docker / kubernetes runner) | the runner's MCP relay (`{"op": "git-credential"}` on `:8098`), helper `sokkan-git-credential` of the session image | the relay token's session — the ticket must name the same one |
  | Bash inside bubblewrap (lot 8) | a per-session Unix socket of the api (0600, private 0700 directory) bound at `/run/sokkan/forge.sock`, helper bound read-only at `/run/sokkan/git-credential-helper` | the socket's session — the ticket must name the same one |

  Without `SOKKAN_SANDBOX_NETWORK=1` the sandbox has no network: the same socket also
  carries git, through a forwarder the wrapper starts on the sandbox's private loopback
  (`127.0.0.1:47391`, `http.<forge>.proxy`), and tunnels only to the forge hosts of the
  session's project (`CONNECT` or absolute-form `http://`; anything else → 403). Nothing
  else leaves the sandbox. A ticket replayed on another session's channel, or after its
  session ended, gets nothing. Proof: `tests/test_forge_push_runners.py`.
* No forge account linked → the session works on a clone that cannot push, and says so.
* An agent run pushes with its **owner's** token; if the owner's link is revoked or their
  access dropped, the run is skipped.

### Lot 5: what shipped

* `backend/forge/`: `Provider` interface, GitLab implementation, GitHub / Gitea-Forgejo
  skeletons; `links` (configuration, `forge.key`, OAuth transactions bound to the person and
  the browser, refresh with rotation); `access` (resolution, cache, repositories);
  `gitcred` + `git_credential_helper.py`; `routes` (`/api/forge/status|links|refresh`,
  `/api/forge/gitlab/link|callback`, `DELETE /api/forge/links/gitlab`,
  `/api/forge/projects/<slug>/protected-branches`, `/api/admin/projects/<slug>/repos`,
  `POST /api/forge/git-credential`). Operator guide: `docs/enterprise/OPERATIONS.md` § 2b.
* `projects.effective_role` reads the forge (with the person's token) when a forge project has
  no fresh cache row; `explain` shows the forge source.
* Someone whose only access is through GitLab can log in before linking: only
  `/api/forge/*`, `/api/me`, `/api/projects`, `/api/features` answer them.
* Not in lot 5: the 30-min sweep for people with a live session (a decision is re-read at the
  first request after it expires — a push is decided by GitLab anyway); an agent run of a
  revoked owner is not skipped yet (its push fails); `viewer` still cannot open a session
  (read-only sessions for Reporter remain to do); live sessions are closed on unlink, not on
  a 401 found later (their credential requests are refused from then on).
* Proof: `tests/test_forge_contract.py`, `tests/test_forge_api.py`, `tests/test_forge_push.py`
  — a fake GitLab (`tests/fake_gitlab.py`: OAuth with PKCE and refresh rotation, API v4,
  real `git http-backend` with a protected `main`) drives real `git push`: Reporter refused,
  Developer pushes a branch with `-o merge_request.create` (the option reaches GitLab) and is
  refused on `main`, Maintainer pushes `main`; no linked account → immediate failure; the
  token is never in the session env, git's output, any file, the logs or the journal.

## Migration from a single-project instance

Automatic at the first start of 3.2, idempotent, nothing deleted or moved:

1. `projects.db` created with project `default` (access source `instance`, name = the
   organisation name).
2. `board.db`, `agents.db`: `project` column, default `'default'` → every session, card and
   agent is in `default`.
3. Memory store: migration `0011` → every note in `default`; the indexer keeps indexing
   `SOKKAN_MEMORY_DIR` as `default`.
4. Vault (lot 4): existing secrets → namespace `default` (`vault.json.v1.bak` kept; a
   rollback to 3.1 puts it back as `vault.json`). `agents` table rebuilt with names unique
   per project (`agents.db.pre-lot4.bak` kept).
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
  person can read, with their role; the choice is in the URL (`/?project=<slug>&plane=…&tab=…`) and
  remembered per person. Every plane shows the selected project's data; a session's pane
  shows its project badge.
* **Admin → Teams & projects** (instance admin): projects (create, archive, access source,
  repositories, resources, budgets), teams (SSO groups seen at login, local teams), grants
  (group or person → role), "who has access to X and why" (the resolution, source by
  source), "Revoke now".
* **Setup › My account › Linked accounts** (lot 5 ✅): link / unlink GitLab (and later others), linked
  identity, scopes, token expiry, state (active / expired / revoked), the role GitLab gives
  in each project, "Refresh my access". Admin → Projects & teams: access source "GitLab
  roles" and the project's repositories.
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
| **3 ✅** | SSO groups → teams at login, ops team, admin screens (projects, grants, "why"), project selector, scoping of every cockpit route (sessions, board, Crew, CortHeXis, Operate links, Nina), per-project workspace + memory directory + `MEMORY.md`, note names unique per project (migration `0012`), the `shared` project, board MCP scope, `projects.create` exposed | **high** (turns multi-project on) | 7 | a second project with two people: none sees the other's sessions, cards, agents, notes (API + UI e2e) |
| **4 ✅** | Vault per project + instance namespace, project budgets and spend reports, agents' owner-role check before each run | medium | 3 | secrets of X never in a session of Y; budget stop per project |
| **5 ✅** | GitLab: link account (OAuth PKCE), `forge.Provider`, access resolution + cache, credential helper, push with the person's token, read-only sessions for Reporter | **high** (external system, tokens) | 6 | against a GitLab CE container: Reporter cannot push, Developer pushes a branch + opens an MR, Maintainer pushes a protected branch |
| **6 ●** | Revocation: SCIM endpoint, "Revoke now", back-channel logout (3.4), audit entries | medium | 3 | SCIM delete → sessions closed, agents paused, tokens gone within a second |
| **7 ✅** | BYOK admin screen (client admin enters their keys) | low | 1.5 | key set, masked, test call, used by sessions |
| **8 ✅** | Optional sandbox per sensitive project (own uid / container for sessions) | high | 5 | a session of X cannot read X' files by `Bash cat` |

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

## Status of `multi_project` (3.4 review) — stays beta

Reviewed for 3.4 « Bridge » (Helm and classification went stable on top of it). The tests
justify it — `tests/test_project_isolation.py` (15 tests through the real middleware with three
people, each filter removed once turns one red), the 29 classification mutations, the Helm
clearance tests — but the field does not yet: on the internal instance (3.2.x → 3.3.0
enterprise, live since 08.10.2026) one person uses it, with one test project besides
`default` and `shared`, for less than a day; the public demo uses it read-only. No two teams
have worked side by side on one instance. **Criterion to go stable**: one deployment with at
least two teams in separate projects (the RTS POC) for two weeks without an isolation finding,
plus the open items of the lots (agent names unique per instance outside lot 4, the
`git credential fill` path from a session). Until then `multi_project` stays **beta**, and the
stable features that need it (Helm, classification) say so in their docs.
