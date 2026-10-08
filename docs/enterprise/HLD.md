# SOKKAN Enterprise — high-level design

Status legend used on every page: **● shipped** (released, 3.1.x) · **◐ in progress**
(implemented on branch `v3.2-multiuser`, unreleased) · **○ planned** (later 3.2 lots, 3.3
"Helm", 3.4 "Teams and decisions"). The contractual specs are
[MULTIUSER.md](../MULTIUSER.md) and [AGENTS.md](../AGENTS.md); when this page and a spec
disagree, the spec wins.

<p align="center"><img src="hld.svg" width="100%" alt="High-level design diagram"/></p>

## 1. Principles

1. **One app.** Community and Enterprise are the same code; the edition only changes
   defaults ([feature registry](FEATURES.md)).
2. **A project is the perimeter**: repositories + resources + CortHeXis memory + agents +
   secrets + budgets + board. Everything is partitioned by project. ◐
3. **Rights come from where they already live**: teams from the IdP (`groups` claim), code
   rights from the forge, read with the person's own token. SOKKAN does not become a
   second place where rights are typed by hand. ◐ (SSO) / ○ (forge)
4. **Fail-closed.** Unknown project, unknown session, expired forge read → no access, no
   recall. Never "everything". ◐
5. **A human go before anything changes**: mutating tool calls, agent creation and changes,
   memory written by agents, deploys. ●
6. **No telemetry.** Nothing leaves the instance except what the operator configures
   (model calls, optional update check). ●

## 2. Components

### 2.1 People and identity

| Item | Status | Notes |
|---|---|---|
| OIDC single sign-on (`SOKKAN_AUTH_MODE=oidc`) | ● | any OIDC IdP; LDAPS login keeps instance roles |
| Unknown emails get no instance role (`SOKKAN_DEFAULT_ROLE=none`) | ● | enterprise default since 3.4.1 (community: `viewer`); access only through project grants / SSO teams |
| `groups` claim → teams `sso:<group>` at each login | ◐ | claim name `SOKKAN_OIDC_GROUPS_CLAIM` (default `groups`) |
| Role per project (`viewer` < `dev` < `maintainer` < `admin`) | ◐ | instance admins have **no implicit access** to project content |
| Ops team = one SSO group (Operate infrastructure) | ◐ | bootstrap `SOKKAN_OPS_GROUP`, then the admin screen |
| SCIM deprovisioning, "Revoke now", back-channel logout | ◐ | 3.2 lot 6; OIDC back-channel logout 3.4 (Entra ID: front-channel + SCIM) |

Profiles: developers, DevOps / system engineers, DBAs, QA, managers, instance admins, ops team.
They work from the **cockpit** (browser) ●, **VS Code or the terminal** with Claude Code
pointed at the gateway (`ANTHROPIC_BASE_URL`) ●, or the `sokkan` CLI ●.

### 2.2 The cockpit (control plane)

Since 3.2.2 the cockpit is navigated by **planes**: one bar of four planes, each with its
sub-tabs. A sub-tab absent for a person (feature off, role too low) is not shown; a plane
with no visible sub-tab is not shown. Landing plane by role: manager / maintainer →
Control, developer → Build, ops team → Operate, instance admin → the last plane they used
(kept per user, `GET/PUT /api/me/nav`). Deep links: `/?plane=build&tab=crew`; every older
`/?tab=<name>` still opens the right place (table in `frontend/lib/planes.ts`, tested).
Keyboard: `g` then `c` / `b` / `o` / `s`, `1`–`9` for the sub-tabs.

| Plane › sub-tab | Role | Status |
|---|---|---|
| Control › Helm | manager view: hierarchical cards, progress that rolls up | ● 3.3 (feature `helm`, people who steer a project) |
| Control › Board | kanban per project; cards spawn pre-seeded sessions; driven from sessions by MCP | ● |
| Control › CortHeXis | memory: notes, graph, recall playground, quarantine review | ● ; per project ● |
| Build › Sessions | parallel Claude Code sessions (Agent SDK), native approval widgets | ● |
| Build › Crew | agents: one card each, deck idle · armed · running · error | ● |
| Build › Preview | the change running, for validation; share read / read-write for review | ● (project `default`) |
| Operate › Incidents | observability, alerts → incidents with a diagnosis session, runbooks | ● (ops team + instance admins) |
| Operate › Infra | topology, managed fleet | ● (ops team + instance admins) |
| Operate › Costs | usage and budgets per session / day / agent run / project | ● |
| Operate › Journal | audit trail | ● |
| Setup › Organization | name, budgets, members, projects & teams, classification, Teams, features | ● |
| Setup › Engines | « Connect your AI » and the instance model keys on one page (governed: allowed engines + each provider's instance key for the admin) | ● |
| Setup › Magnitude | pair machines, benchmark models, serve one, route sessions to it | ● |
| Setup › Secrets · My account · Notifications | vault (names only), identity and linked accounts, alert channels | ● |
| Nina | built-in assistant, panel on every plane | ● |

**Project gate** ◐ — a middleware decides the project of every request (the project of the
*object* for sessions, cards, agents, runs; otherwise the `x-sokkan-project` header) and
replaces the role by the role in that project. No role → `404`. MCP servers get their scope
from the API's environment (`SOKKAN_SESSION_PROJECT`), never from a tool argument.

**Feature registry** ◐ — [`backend/features.py`](../../backend/features.py), generated
table in [FEATURES.md](FEATURES.md).

### 2.3 Sessions and agent runs (execution)

* A session is a Claude Code process (Agent SDK) in the `api` container, non-root, against
  the project's workspace. Mutating tools wait for a click. ●
* Secrets are injected **by name** (`SOKKAN_SESSION_SECRETS=named`, default from 3.2 ◐) and
  redacted from live events, replays and deliverables in raw, base64, URL-encoded and hex
  forms. ●
* Budget per session, per day, per agent run; for non-Claude models SOKKAN computes the cost
  itself, with a token ceiling per run (`SOKKAN_AGENTS_MAX_TOKENS_PER_RUN`). ●
* Agents (Crew): draft → pending → active ⇄ paused → archived; approval `owner` | `admin` |
  `four_eyes` (`SOKKAN_AGENTS_APPROVAL`); the scheduler only runs with **explicit**
  credentials (lot 2). ●/◐
* A session of a project other than `default` works in its own workspace
  `$SOKKAN_DATA_DIR/projects/<slug>/work`; raw terminal sessions only in `default` until the
  sandbox. ◐
* Sandbox per sensitive project (own uid / container). ○ 3.2 lot 8

**Execution layer — session pods** ◐ *in progress* (feature `kubernetes_runner`,
[KUBERNETES.md](KUBERNETES.md)). Where the CLI of a session or agent run executes is a
runner: `local` (in the api, default ●), `docker` (one container per session on the compose
host ◐) or `kubernetes` (one Pod per session through the K8s API, Helm chart ◐). A session
container is non-root, read-only, capability-free, CPU/memory limited, mounts only its
project's workspace and transcripts, and reaches only the api's MCP relay (identity set by the
api) and an allowlist egress gateway. The SDK keeps driving it (permissions, hooks, budgets);
a supervisor in the container survives an api restart and the api reattaches. Validated: unit
and docker tests with the real CLI; the chart on a real k3s cluster (real images, a session
pod started from the api, api restart → reattach, cleanup) and under the enforced `restricted`
Pod Security Standard — SKS and OpenShift clusters not yet.

### 2.4 Embedded MCP servers

`sokkan-memory` (search, get, links, write) ● · `sokkan-board` (search, read, create, edit,
move, comment, link, close cards — every action signed with the session) ●/◐ ·
`sokkan-agents` (propose, list, run, pause agents; read-only inside a run) ● · observability
(query metrics and logs, build dashboards) ● · preview ●. All scoped to the session's
project ◐.

### 2.5 Inference — tiered gateway (operated service)

SOKKAN Inference speaks the **Anthropic Messages API**: Claude Code and the Agent SDK use it
through `ANTHROPIC_BASE_URL` with a tenant token.

| Stage | Status |
|---|---|
| Tenant authentication, prepaid balance, refusal at zero | ● |
| Scrubber before any upstream call: secrets → request blocked, PII → masked (irreversible) | ● |
| Tiers **Ship** (fast open model) → **Deep** (large open model); escalation on keywords, on the `<<ESCALATE>>` sentinel, or on upstream failure; no sticky routing | ● |
| Client tier profiles: N tiers per customer, top tier **Claude with the customer's own key** (BYOK, encrypted, passthrough, not billed, `cache_control` preserved) | ◐ |
| Escalation held for one human turn, then decided again | ◐ |
| Upstream prompt cache billed at the cache rate | ◐ |
| "Avoided Claude cost" report (day, month, user, tier; JSON, CSV, Prometheus) — net of the escalation overhead | ◐ |
| Router and Inference merged into one engine with two doors (OpenAI + Anthropic) | ○ decision pending |

### 2.6 Compute

| Where models run | Status |
|---|---|
| EU GPU endpoints — the current Ship / Deep upstreams | ● |
| Dedicated Swiss GPUs (Exoscale, Geneva) — primary capacity for sustained enterprise load | ○ |
| Magnitude nodes — the customer's own GPUs, multi-node registry, one GPU per node | ● |
| A small 10–20B model on a customer VM (memory engine, light tasks) | ○ |
| Local embeddings for CortHeXis (CPU, or GPU profile) | ● |

### 2.7 CortHeXis — team memory

Postgres + pgvector, a local embedding service (optional reranker) ●. One memory directory, indexer and
note namespace per project, plus the read-only `shared` project; recall filtered at every
step (dense, lexical, final guard) and names cited by a note filtered too ◐. Notes written by
agent runs go to a **quarantine** outside the indexed directory and reach the memory only
after a human approves ●.

### 2.8 Integrations

| Integration | Status |
|---|---|
| GitLab: OAuth link per person, access level → project role, push and merge requests **with the person's token** (credential helper), read-only session for Reporter | ○ 3.2 lot 5 |
| Other forges (GitHub, Gitea/Forgejo, Bitbucket, Azure DevOps) — same `forge.Provider` interface | ○ |
| Microsoft Teams: @Nina, HITL approval cards, decisions captured from a thread, calendar / presence / channels via Graph | ○ 3.4 |
| Observability: Prometheus, Grafana, Loki; alert webhook → incident (alert payload framed as untrusted data) | ● |
| HITL push: Telegram / webhook | ● |

### 2.9 Audit

Journal of every action (spawn, move, delete, `agent.*`, approvals, overrides) ●;
`project.create|archive|grant|revoke`, `team.sync`, `project.grant.self`,
`memory.scope_violation` (must stay at zero) ◐; forge, BYOK and revocation events ○. The
journal records actions, not conversation content.

## 3. Deployment views

| View | Content |
|---|---|
| Kubernetes (◐ in progress) | Helm chart `deploy/helm/sokkan`: `api` (1 replica), `web`, egress gateway, one Pod per session / run; Postgres = DBaaS; optional embeddings and vLLM on GPU nodes; overlays SKS and OpenShift (restricted SCC) |
| Single VM (POC) | `docker compose`: `web`, `api`, `db` (Postgres), `corthexis-embed`; optional profiles `rerank` (`corthexis-rerank`) and `edge` (Caddy, TLS); volumes `sokkan-data`, `sokkan-pg`, `corthexis-models` |
| Three VMs (POC target) | cockpit · CortHeXis memory engine · small LLM — **TBD**: split topology not yet documented as a supported layout |
| Managed (SOKKAN Cloud) | dedicated VM + private network per customer in Geneva |
| Inference | operated gateway; customer GPUs via Magnitude |

## 4. Open decisions

* Router / Inference merge (one engine, two doors) — before the enterprise offer.
* GitLab and Entra ID to be confirmed at the customer (abstraction kept).
* BYOK per instance in 3.2, per project later.
* Supported multi-VM layout (§3) — TBD.
