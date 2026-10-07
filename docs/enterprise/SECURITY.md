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

## 8. Roadmap — classification and clearances (3.4)

Goal: a note, a card or a document carries a **classification level**, a person carries
**clearances**, and nothing reaches a person above their clearance — including through Nina.

| Rule | Meaning |
|---|---|
| Levels | an ordered scale set by the customer (e.g. public < internal < confidential < restricted) — **TBD with the customer** |
| Clearance | per person, from IdP groups (same mechanism as teams) |
| **The derived inherits the highest level** | a summary, digest, card or deliverable built from several sources is classified at the highest level among them, automatically |
| **Nina acts on behalf of the user** | the assistant (cockpit, Teams) sees exactly what the person in front of it may see — never a service account's view |
| Audited recall | every recall of a classified note is logged (who, which note, which session) |
| Fail-closed | an unclassified source in a classified project takes the project's default level |

Dependencies (registry): `classification` requires `multi_project` and `sso_teams`; `teams`
(Microsoft Teams) requires `classification`. Status: ○ planned 3.4. Design document: **TBD**.

## 9. Out of scope today

Several organisations on one instance (one instance per customer); ACLs below the project
(per card, per note); cross-project agents; LDAPS group sync (OIDC only in 3.2).
