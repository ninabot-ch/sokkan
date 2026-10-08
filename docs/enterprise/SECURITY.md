# Security model — SOKKAN Enterprise

Vulnerabilities: security@ninabot.ch (see [../../SECURITY.md](../../SECURITY.md)). Status
markers: ● shipped · ◐ in progress (3.2 branch) · ○ planned.

## 1. Trust boundaries

| Boundary | Enforcement |
|---|---|
| Browser ↔ cockpit | OIDC login, signed cookie (8 h ◐), WebSocket `Origin` check, no CORS layer (the browser only talks to the web origin) ● |
| Person ↔ project | **project gate**: every route, MCP server and WebSocket resolves the project of the object and uses the role in that project; no role → 404 (existence not revealed) ◐ |
| Session ↔ host | sessions run tools in the `api` container as a non-root user, against the mounted workspace; mutating tools need a click ● ; per-project sandbox (own uid / container) ○ lot 8 |
| Instance ↔ model provider | the scrubber at the gateway (secrets blocked, PII masked) ● ; data location of each tier agreed per customer |
| Instance ↔ forge | push with the person's own token: the forge is the authority ○ lot 5 |

## 2. Roles per project

`viewer < dev < maintainer < admin` per project, from an SSO group or (lot 5) the forge access
level; with several repositories the **lowest** level applies. Instance `admin` / `owner`
administer teams, IdP mapping, projects, BYOK and audit, but **see no project content**
without granting themselves a role, which is logged. Full matrix:
[MULTIUSER.md § Role × action matrix](../MULTIUSER.md#role--action-matrix). ◐

## 3. Memory isolation (the main leak risk)

* One memory directory, indexer and note namespace per project (note names unique per
  project, so a name collision reveals nothing), plus `shared`, read-only for everyone. ◐
* Recall is filtered at every step: dense search (HNSW iterative scan), lexical search, final
  guard; the names a note cites are filtered too; `memory_get` / `memory_links` answer "not
  found" outside the scope. ◐
* Fail-closed: an unknown session or project gets no recall. ◐
* Notes written by agent runs go to a **quarantine** outside the indexed directory, with
  provenance; a maintainer approves or rejects. Write/Edit to memory directories is refused
  inside a run. ● Limit: an auto-approved `Bash` rule could still write a file.
* `memory.scope_violation` audit entry when a filter drops a row the query should have
  excluded — must stay at zero. ◐
* Tests: a two-project, three-person isolation suite runs against the real middleware; each
  filter removed once turns it red (mutation check). ◐

## 4. Secrets

* Vault encrypted at rest (Fernet, instance key, files 0600) ●; per-project namespaces + an
  `instance` namespace sessions never receive ○ lot 4.
* **Named secrets**: a session or agent receives only the secrets named for it
  (`SOKKAN_SESSION_SECRETS=named`, default from 3.2) ◐; agents reference names only, a value
  that looks like a key is refused ●.
* Redaction of secret values — raw, base64 (std / URL, three alignments), URL-encoded, hex,
  fragments ≥ 12 characters of a secret ≥ 16 — in live events, replays, History, deliverables ●.
* Known limits: the Claude CLI's JSONL transcript on disk keeps values; a rotated secret is
  masked with its current value only.
* The scrubber at the gateway blocks a request that carries a secret before it leaves ●.

## 5. Alerts are untrusted input

An alert payload enters a prompt only inside an `<untrusted-data kind="alert" id="<nonce>">`
frame with an instruction not to follow it ●. An agent triggered by alerts cannot auto-approve
writes (Write / Edit / Bash / non-read MCP tools): refused at activation, edit and approval,
and stripped at run time from agents armed earlier; an admin override is stored with who,
when and which rules, and audited (`agent.alert_write_override`) ●.

## 6. Double control

* `SOKKAN_AGENTS_APPROVAL=four_eyes`: the approver of an agent's creation or change differs
  from the proposer **and** the owner; with projects, the approver is maintainer+ in that
  project ●/◐.
* Every mutating tool call in a session waits for the person driving it ●.
* A session can only *propose* an agent ●.
* Before each run the scheduler checks that the owner still has ≥ dev in the project ○ lot 4.
* The scheduler runs only with explicit model credentials ◐ (lot 2).

## 7. Audit

Journal of actions (not conversation content): spawns, moves, deletions, `agent.*`, approvals,
overrides ●; `project.create|archive|grant|revoke`, `project.grant.self`, `team.sync`,
`memory.scope_violation` ◐; `forge.*`, `byok.*`, `revoke.now` ○. With several projects the
journal is for instance admins only ◐.

## 8. Classification and clearances (3.4) ◐

A note, a decision, a card or an agent deliverable carries a **level**; a person carries a
**clearance** per project; nothing reaches a person above their clearance — including
through Nina, an agent or Teams. Feature `classification` (requires `multi_project` and
`sso_teams`; on by default in the enterprise edition). Code: `memory/core/levels.py`,
`memory/core/scope.py`, `backend/classification.py`, `backend/classification_api.py`.

### Levels

`public < team < project < confidential < restricted` — five fixed ids (API, frontmatter,
database rank 0-4), relabelled to the customer's grid with `SOKKAN_CLASSIFICATION_LABELS`
(five labels in that order, e.g. `Public,Interne,Projet,Confidentiel,Secret`).

| Object | Where the level lives | Default |
|---|---|---|
| Note / decision | frontmatter `classification: <id or label>` → `notes.level` (migration `0013`) | `project` |
| Card | `cards.level` | `project` (selector at creation) |
| Agent deliverable | quarantined note (`classification:` + provenance), run card | highest level its run obtained |
| Session / run | computed: highest level of the notes it obtained (`note_access`) | — |

Fail-closed: a value that is set but not understood is `restricted`; a note without the key
is `project`; the migration puts every existing note at `project` (nothing changes on screen).

### Clearance

clearance(person, project) = max( level of their **project role** (default: every role reads
up to `project`; `SOKKAN_CLEARANCE_ROLES=viewer=team,…` or Setup › Organization › Classification), level
mapped to any of their **SSO groups** for that project or for every project
(`clearance_groups`, Setup › Organization › Classification, `PUT /api/admin/classification/groups`) ).
No role in the project = no clearance (the project does not exist for them).

A **scope** is `(project@clearance, shared@clearance)`. The memory engine applies it at every
stage of every search (dense HNSW, lexical, final guard — SQL), in `resolve_note`,
`existing_names`, `list_notes`, `recall_log`, and re-checks in Python (`scope.filter_hits`,
`scope.visible`): a store that ignored the scope still could not leak. An entry that lost
its clearance on the way (`radio` instead of `radio@3`) reads up to `project` only — a
lost clearance only narrows.

### Who acts for whom — there is no service view

| Surface | Whose clearance | Check |
|---|---|---|
| Spawn pre-seed, per-turn and sub-agent recall | the session's **owner** (`sessions.owner`) | `classification.session_scope` |
| MCP `memory_search` / `memory_get` / `memory_links` | the owner (`SOKKAN_SESSION_SCOPE` with clearances, set by the API) | `memory_search_server._scope` |
| MCP board (`list_board`, `search_cards`, `get_card`…) | the owner | `board_mcp._cap` |
| Agent run | the agent's **owner** | `agents_runtime` → `owner` |
| Cockpit (memory, CortHeXis graph/note/review/proposals, board, quarantine, sessions, runs) | the person logged in | `projectgate` → `pu["clearance"]` |
| Nina (cockpit) | the person who asks | `assistant._memory_scope` |
| Nina (Teams), approvals, decisions | the Teams user linked to their SSO account | `backend/teams/` |

Object routes (`/api/sessions/{id}`, `/api/board/card/{id}`, `/api/agents/runs/{id}`, the
session WebSocket) answer **404** when the object is above the person's clearance — the same
answer as for an object that does not exist. Run lists keep the row and withhold the
deliverable (`classified: <level>`).

### The derived inherits the highest level

* A note written by a session (`memory_write`) = max(requested, highest level the session
  obtained); a floor is recorded at once (`note_level_floor`).
* An agent deliverable (quarantined note, run card) = highest level its run obtained; at
  approval the floor is recorded.
* A card created from a session (board MCP) inherits likewise.
* A Nina answer reports `level` = highest level of the notes it was built from.
* A decision captured in Teams = max(level of the channel's project default, the thread).

**Never lowered by an edit**: an upsert of the index keeps `greatest(file level, stored
level, floor)`. Lowering = `POST /api/memory/note/{name}/level` or
`/api/board/card/{id}/level` by a **maintainer/admin of the project cleared for the current
level, with a reason** — journaled (`classification.note.lower`, `classification.card.lower`).
Raising: any dev cleared for the current level.

### Audited recall

Every note handed out is a row of `note_access` (Postgres): `at, via, actor, session_id,
project, note_name, level, query`. `via` = `spawn` | `prompt` | `subagent` | `mcp` |
`cockpit` | `nina` | `teams` | `brief`. Rows of sessions name their actor through the
session's owner. `GET /api/classification/audit` (project **admins**; `?format=csv` exports;
filters actor, note, session, days) — entries about notes above the reader's own clearance
are left out; each read is journaled (`classification.audit.read`).

### Off

`classification` off: every scope is the 3.2 one, which reads up to `project` — a note or a
card classified above it stays out of reach of everyone (turning the feature off never opens
data); badges and selectors are hidden.

### Limits (3.4)

* Not a hard boundary against `Read`/`Bash` in a session: a classified note is a file in the
  project's memory directory (channel 7 of MULTIUSER.md) — lot 8 sandbox.
* A session's scope is computed at its start (owner's clearance then); a clearance removed
  later applies to new sessions (revocation, lot 6, closes the running ones).
* The CortHeXis Telegram digest is instance-level: keep it off on classified instances.
* No per-paragraph classification; the level is per object.
* Session transcripts (`*.jsonl`) are files of the instance: protected by the gate (404), not
  encrypted per level.

## 9. Out of scope today

Several organisations on one instance (one instance per customer); ACLs below the project
(per card, per note); cross-project agents; LDAPS group sync (OIDC only in 3.2).
