# Agents ("Crew") — spec

Status: **3.1 "Crew up"**, in development on branch `crew-up`. This page is the contract
the code implements; when they disagree, fix one of them.

## Why

A session is something a human opens and steers. A lot of engineering work is not like
that: *check our dependencies for CVEs every night*, *sort yesterday's error logs*,
*review every PR labelled `needs-review`*, *write the weekly ops report*. Until 3.0 the only
way to get that in SOKKAN was an external cron calling the API — obscure, unowned,
unmonitored.

An **agent** is a named, owned, reusable job description: *which model, what for, what it
must deliver and when it is done, when it runs, what it may touch, which secrets it may
use, how much it may spend, and where its output goes.* Each execution is a **run** — an
ordinary SDK session (same HITL gates, same memory recall, same transcript) started by the
scheduler instead of a human.

Target users: developers, devops, system engineers, DBAs, QA.

## Reuse, not reinvention

| Need | Reused brick |
|---|---|
| Executing a run | `agentchat.AgentSession` (Claude Agent SDK), registered on the board like any session |
| Context at start | deterministic memory recall at spawn (`_memory_preseed`) + per-turn recall hooks |
| Prompt shape | optional playbook (`playbooks.py`) as the mission template |
| Credentials | the instance vault (`vault.py`) — agents reference secret **names** only |
| Spend | per-run budget (SDK `max_budget_usd`) on top of the instance budgets |
| Human gate | the existing permission widgets + delayed HITL ping (`notify.py`) |
| Alerts | `notify.send` (Telegram / webhook) |
| Event trigger | the Operate alert receiver `POST /api/observability/alert` |
| Output | board card (`board.add_card`), memory note (`memory_write`), file, notification |
| Who / audit | IAM (`iam.py` roles) + `audit.log` |
| From a session | embedded MCP server `sokkan-agents`, like `sokkan-board` / `sokkan-memory` |

## Data model (`$SOKKAN_DATA_DIR/agents.db`, SQLite)

### `agents`

| Field | Meaning |
|---|---|
| `id` | integer |
| `name` | unique kebab-case slug (`nightly-cve-audit`) |
| `owner` | email of the human who owns it (IAM identity). Runs act on their behalf. |
| `model` | `""` = instance default, an alias (`haiku`, `sonnet`, `opus`) or a full model id |
| `purpose` | what the agent is for (the mission, 1-3 paragraphs) |
| `deliverable` | what a run must hand back (a report, a PR review, a ticket list…) |
| `done_criteria` | how the agent knows the run is finished; checked by the agent itself (`DELIVERY:` line, see below) |
| `playbook` | optional playbook id used as the mission template |
| `trigger` | `manual` · `once` (`once_at`, epoch) · `cron` (`schedule`, 5-field cron, **Europe/Zurich** unless `timezone` says otherwise) · `event` (`event`: `alert` or `alert:<alertname glob>`) |
| `tools` | tools the agent may use. Anything else is **denied without asking** (no 3 a.m. ping for a tool it should not use). Default: `Read Glob Grep WebFetch WebSearch Bash` |
| `mcp` | embedded MCP servers it gets: `sokkan-memory` (always), `sokkan-board`, `sokkan-observability` |
| `auto_approve` | subset of `tools` run without asking, in Claude Code rule syntax (`Bash(npm audit:*)`, `Write`). Default empty: every mutating call waits for a human |
| `secrets` | vault secret **names** injected as env vars of the run (`$GITHUB_TOKEN`). Never values. |
| `budget_usd` | hard cap per run (0 = instance session budget, else none) |
| `max_minutes` | wall-clock cap per run (default 30); beyond it the run is interrupted → `timeout` |
| `outputs` | where the deliverable goes, any of: `card` (board card in **Review**), `memory` (note `agent-<name>-latest`, overwritten each run), `file` (`$SOKKAN_DATA_DIR/agents/<name>/<run>.md`), `notify` |
| `notify_on` | subset of `failure` (default), `budget`, `timeout` (default), `approval` (default), `success` |
| `status` | lifecycle, below |
| `pending_change` | a change proposed by a session on an approved agent, waiting for a human |
| `created_by` | `user:<email>`, `session:<sid>` or `nina:<email>` |
| `approved_by`, `approved_at` | the human who activated it |
| `next_run_at`, `last_run_at` | scheduler state (UTC epoch) |

### Lifecycle

```
            human form ──────────────┐ (activate now)
                                     ▼
 draft ──submit──▶ pending ──approve──▶ active ◀──resume── paused
   ▲                 │                    │  └──pause──────▶ │
   └────reject───────┘                    └──────archive─────┴──▶ archived
```

* **Created from a session** (MCP `create_agent`) or from Nina → `pending`. It never runs
  until a human approves it in the Crew tab. Approving = owner (dev+) or an admin.
* **Created from the form** by a human → `draft`, or `active` straight away (the human *is*
  the gate).
* **Changed from a session** while `active`/`paused` → stored as `pending_change`; the
  approved version keeps running until a human approves or rejects the change.
* `archived` is terminal for scheduling; history is kept.

### `runs`

| Field | Meaning |
|---|---|
| `id`, `agent_id` | |
| `trigger` | `manual`, `schedule`, `event`, `session` (MCP `run_agent_now`) |
| `scheduled_for` | the occurrence this run is for; `UNIQUE(agent_id, scheduled_for)` → **no double run** of a cron occurrence |
| `status` | `queued` → `running` → `succeeded` · `incomplete` · `failed` · `timeout` · `budget` · `interrupted` · `cancelled` · `skipped` |
| `waiting_approval` | 1 while a tool call of the run waits for a human |
| `session_id` | the SDK session (open it from the run to see everything) |
| `cost_usd`, `tokens_in`, `tokens_out`, `num_turns` | from the SDK `ResultMessage` |
| `deliverable` | final answer of the run, **secret values redacted** |
| `outputs` | where it was filed (`{"card": 12, "memory": "agent-x-latest", "file": "…"}`) |
| `error` | why it failed |
| `context` | event payload (alert labels…) for event runs |
| `requested_by` | user / session / scheduler |

## Scheduler

* One asyncio loop in the API process (`agents_runtime.py`), tick `SOKKAN_AGENTS_TICK_S`
  (15 s). Disabled with `SOKKAN_FEATURE_AGENTS=0`.
* **Cron**: 5-field expressions (`*`, lists, ranges, steps, names), evaluated in wall-clock
  time of the agent's zone (default `Europe/Zurich`). DST: an occurrence in the
  spring-forward gap is skipped; one in the autumn repeated hour runs once.
* **No double run**: a scheduled run is inserted with `INSERT OR IGNORE` on
  `(agent_id, scheduled_for)`; a run is started only by an atomic
  `UPDATE … SET status='running' WHERE id=? AND status='queued'`. At most **one active run
  per agent** (a new occurrence while the previous run is still going is recorded as
  `skipped`). Global concurrency `SOKKAN_AGENTS_MAX_CONCURRENT` (default 2 — a small
  cockpit VM does not hold 4 agent sessions).
* **Restart**: on boot, runs left `running` by a previous process become `interrupted`
  (+ notification); `queued` runs are picked up. Occurrences missed while the API was
  down: **one** catch-up run if the latest missed occurrence is younger than
  `SOKKAN_AGENTS_MISFIRE_S` (default 6 h), the others are not replayed.
* **Watchdog**: a run past `max_minutes` is interrupted → `timeout`.

## A run

1. Agent context → first message: purpose, deliverable, done criteria, the names of the
   secrets available as env vars, the tool policy, the event context if any, and the
   **auto-recalled memory block** (same `_memory_preseed` as a spawn). Optional playbook
   as the mission template.
2. Session options: the agent's `model`; `allowed_tools` = read-only safe tools ∩ `tools`
   + `auto_approve`; a policy in `can_use_tool` denies tools outside `tools`; env = only the
   referenced vault secrets; `max_budget_usd` = the run budget; the `sokkan-agents` MCP is
   read-only inside a run (an agent does not breed agents).
3. A tool call that needs a human: the usual permission widget in the run's session + the
   usual HITL ping; the run shows **waiting approval** in Crew.
4. End: the agent ends its answer with `DELIVERY: done` or `DELIVERY: incomplete — <why>`.
   Missing line = `succeeded` if the SDK result is not an error. Cost, tokens and turns come
   from the SDK result; the deliverable is redacted (every vault value referenced by the
   agent replaced by `[secret:NAME]`) and filed to the configured outputs.
5. Monitoring → `notify.send`: failure, timeout, budget hit, approval waiting (the existing
   ping), success if asked. Everything lands in the audit log (`agent.*`).

## Access control (IAM, before full RBAC)

* Every agent has an `owner`. A session/Nina proposal is owned by the human behind it.
* **viewer**: sees nothing of Crew yet (runs carry deliverables). **dev**: creates and
  manages their own agents, sees their own runs. **admin/owner**: sees and manages all.
* Approve / reject a proposal: the agent's owner (dev+) or an admin.
* Runs execute with the owner's identity for metering (`AgentSession.user`).

## API (`/api/agents`, cookie auth like the rest)

| Method | Path | Role |
|---|---|---|
| GET | `/api/agents` | dev+ (own) / admin (all) |
| POST | `/api/agents` | dev+ — body = agent fields + `activate: bool` |
| GET/PATCH | `/api/agents/{id}` | owner or admin |
| POST | `/api/agents/{id}/approve` · `/reject` · `/pause` · `/resume` · `/archive` · `/run` | owner or admin |
| GET | `/api/agents/{id}/runs` · `/api/agents/runs/{run_id}` | owner or admin |
| POST | `/api/agents/runs/{run_id}/cancel` | owner or admin |
| POST | `/api/agents/proposals` | dev+ — a proposal from Nina's chat (status `pending`) |
| GET | `/api/agents/meta` | dev+ — vault names, models, MCP, playbooks for the form |

## MCP `sokkan-agents` (embedded in every session)

`create_agent`, `update_agent`, `list_agents`, `get_agent`, `run_agent_now`,
`pause_agent`, `resume_agent`, `archive_agent`, `list_runs`, `get_run`.

* Reads (`list_*`, `get_*`) are auto-approved (in `SAFE_TOOLS`).
* Writes go through the session's normal permission gate, then:
  `create_agent` → **pending** proposal; `update_agent` on an approved agent →
  `pending_change`; `resume_agent` only for an agent a human approved before.
* The caller is identified by `SOKKAN_SESSION_ID` / `SOKKAN_SESSION_USER`, set by the API
  in the MCP server's environment for that session.
* Secrets are passed by name; a tool argument that looks like a secret value is refused.

## Out of scope for 3.1

Per-agent RBAC beyond owner/admin, run retries with back-off, multi-step DAGs, agents
calling agents, run on a Magnitude node, a PR-webhook trigger (`event` covers Operate
alerts only; a generic `POST /api/agents/{id}/hook` is the obvious next step).
