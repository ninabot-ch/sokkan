# SOKKAN Enterprise

> **The same open-source SOKKAN, with enterprise features you switch on one by one.**
> One codebase, one release train, one changelog — no "enterprise fork" that drifts.

<p align="center"><img src="hld.svg" width="960" alt="SOKKAN Enterprise high-level design: people and SSO, the cockpit, sessions and agents, embedded MCP servers, the tiered inference gateway, compute, team memory, GitLab, Teams, observability and audit"/></p>

## What it is

SOKKAN is a self-hosted cockpit for running Claude Code sessions and agents on your code and
your production, with your project memory injected into every session and a human go before
anything changes. **SOKKAN Enterprise** is that cockpit configured for an organisation:
several teams on one instance, each on its own perimeter, with rights that come from your
identity provider and your forge, and with the controls a security officer asks for.

## Who it is for

| Profile | What they get |
|---|---|
| Developers, DevOps, system engineers, DBAs, QA | parallel sessions from the cockpit, VS Code or the terminal; a kanban they drive from the sessions; agents for the recurring work; a preview to validate a change |
| Ops team | the Operate tab: alerts become incidents with a diagnosis session already started, runbooks, observability their sessions can read and write |
| Managers | project cards that split into engineer cards and report progress back (Helm, 3.3) |
| Security officers, admins | SSO, roles per project, memory isolated per project, secrets by name, four-eyes approval, audit trail, and a feature registry that says what is on |

## What sets it apart

1. **A cockpit, not another chat.** Board, Sessions, Crew (agents), Preview, CortHeXis
   (memory), Operate, Costs and Magnitude in one app, around the developer's existing tools:
   Claude Code in VS Code or the terminal keeps working; SOKKAN plugs in underneath
   (`ANTHROPIC_BASE_URL` + MCP servers), it does not replace it.
2. **Governed agents.** An agent is a card: model, purpose, expected deliverable, schedule
   or trigger, allowed tools, secrets by name, budget per run. A session can only *propose*
   an agent; a human approves (optionally a second human — four-eyes).
3. **Team memory, partitioned.** CortHeXis gives every session the project's knowledge at
   spawn. One memory per project plus a read-only `shared` project; notes written by agents
   wait in quarantine for a human review. A person without access to a project never
   receives one of its notes, by any channel.
4. **Sovereign, tiered inference.** A gateway that starts on a fast open model, escalates to a
   larger one when the task needs it, and to Claude with **your own key** as the top tier. Prompts
   are scrubbed (secrets blocked, PII masked) before they leave; a report shows the Claude cost
   avoided. GPUs in Switzerland first (Exoscale, Geneva), EU endpoints as fallback, or your
   own machines through Magnitude.
5. **Steering.** Costs and budgets per session, per agent run and (3.2) per project; an audit
   trail of every action; a manager view in 3.3.

## One app, features switched on one by one

The enterprise features live in the open-source code. Each one is declared once in a
registry — its switch (`SOKKAN_FEATURE_<ID>`), its default per edition (`SOKKAN_EDITION` =
`community` | `enterprise`), what it requires and what it conflicts with. A feature whose
requirements are not met is turned off with the reason; it is never half on. The cockpit
shows the effective state in **Profile → Features** (`GET /api/features`).

* Registry (source of truth): [`backend/features.py`](../../backend/features.py)
* Generated table of every feature, its variables, status and dependencies: [`FEATURES.md`](FEATURES.md)
* Recommended activation order: [OPERATIONS.md § Enable features one by one](OPERATIONS.md#4-enable-features-one-by-one)

## Status (October 2026)

| Release | Content | Status |
|---|---|---|
| 3.1.x "Crew up" | agents (Crew), four-eyes approval, memory quarantine, alert = untrusted input, secret redaction, cost basis for non-Claude models | **shipped** |
| 3.2 "multi-user" | projects, SSO teams, roles per project, memory per project, `shared`, ops team, secrets by name by default, 8 h cockpit sessions (lots 1-3 done on the branch); vault and budgets per project, GitLab, revocation, BYOK screen, sandbox, shared preview for review (next lots) | **in progress** — branch `v3.2-multiuser`, unreleased |
| 3.3 "Helm" | manager cards → engineer kanban, progress that rolls up, direction view, Nina decomposes and suggests, morning brief | **planned** |
| 3.4 "Teams and decisions" | note classification and clearances, Nina in Microsoft Teams, HITL approval cards, decisions captured from a thread | **planned** |

The tiered inference gateway (client tier profiles with a Claude BYOK tier, avoided-cost
report) is an operated SOKKAN Cloud service, not part of this repository; its client side is
the standard `ANTHROPIC_BASE_URL` setting.

## Documents

| Document | For |
|---|---|
| [HLD.md](HLD.md) | architecture, component by component, with status |
| [METHOD.md](METHOD.md) | how a team works with SOKKAN, from the manager's card to production |
| [PIPELINE.md](PIPELINE.md) | the path of one model call; the release pipeline |
| [OPERATIONS.md](OPERATIONS.md) | runbook: install, SSO, features, backup, upgrade, monitoring, incidents, secrets, go-live checklist |
| [SECURITY.md](SECURITY.md) | security model and the classification roadmap |
| [hld-overview.html](hld-overview.html) · [PNG](hld-overview.png) · [PDF](hld-overview.pdf) | one-page overview for decision-makers (French) |
| [../MULTIUSER.md](../MULTIUSER.md), [../AGENTS.md](../AGENTS.md), [../OPERATE.md](../OPERATE.md), [../UPGRADE.md](../UPGRADE.md), [../RELEASING.md](../RELEASING.md) | the contractual specs these pages summarise |

Diagrams are generated by [`src/diagrams.py`](src/diagrams.py) (SVG, light and dark).
