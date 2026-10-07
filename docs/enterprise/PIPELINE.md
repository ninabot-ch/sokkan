# Pipelines

<p align="center"><img src="pipeline.svg" width="100%" alt="A: request pipeline from the prompt to the audit journal; B: release pipeline from the branch to the fleet"/></p>

## A. One model call — from the prompt to the audit trail

| # | Stage | Where | What it guarantees | Status |
|---|---|---|---|---|
| 1 | **Prompt** | cockpit, VS Code / terminal (Claude Code with `ANTHROPIC_BASE_URL`), `sokkan` CLI, or an agent run | the session belongs to one project; its MCP servers get that scope from the API's environment | ● (scope ◐) |
| 2 | **Session policy** | SOKKAN `api` | allowed tools (agents: `tools`, `auto_approve`, `disallowed_tools`); every other mutating call waits for a human; secrets injected **by name**; budget per session / run. An alert that triggers an agent is framed as `<untrusted-data>` and cannot auto-approve writes | ● |
| 3 | **Scrubber** | gateway, before any upstream call (prompt, tool results, system blocks, vision relay) | secrets (API keys, tokens, private keys, credentials) → the request is **blocked**; PII (IBAN, e-mail, AHV/AVS, card numbers) → **masked**, not reversible | ● |
| 4 | **Balance / budget gate** | gateway | prepaid balance; with a tier profile the refusal (`402`) is decided when a **billed** tier would be called — a passthrough Claude tier (customer's key) is never blocked, and a request is never silently moved to the customer's Anthropic bill | ● / ◐ |
| 5 | **Tier chain** | gateway | start tier from the keywords of the last human message; a tier may hand over with the `<<ESCALATE>>` sentinel before its first byte; an upstream failure goes to the next upstream, then the next tier; the escalated tier holds for the rest of that human turn (`escalation_scope: human_turn`), then the next message decides again; **no sticky routing**; ceiling `max_tier` | ● Ship → Deep · ◐ Claude (BYOK), per-turn hold |
| 6 | **Metering** | gateway | tokens in / cached / out per tenant, user, tier; cache priced at the cache rate when the upstream reports it; "avoided Claude cost" = public price of the reference model − escalation overhead − SOKKAN billed (report per day / month / user / tier, CSV, Prometheus) | ● metering · ◐ cache rate, report |
| 7 | **Response** | SOKKAN `api` | secret values redacted (raw, base64, URL-encoded, hex, long fragments) in live events, replays, History and deliverables | ● |
| 8 | **Memory quarantine** | SOKKAN `api` | notes written by agent runs land outside the indexed directory, with provenance; a maintainer approves (→ memory, `approved_by`) or rejects (→ archive) | ● |
| 9 | **Audit** | journal | who did what, in which project, approved by whom; actions, not conversation content | ● (project events ◐) |

Known limits (stated, not hidden): the Claude CLI's own JSONL transcript on disk keeps secret
values in clear; a secret rotated later is masked with its *current* value; an auto-approved
`Bash` rule could still write a file outside the memory tools. See [SECURITY.md](SECURITY.md).

### The same pipeline for a task (agent run)

trigger (cron in Europe/Zurich, one-shot, `alert[:glob]`, "Run now") → claim (one live run per
agent, `SOKKAN_AGENTS_MAX_CONCURRENT`) → **explicit credentials check** (no run without
configured model credentials) → session with recall, policy, secrets by name and budget →
stages 3-6 above for each model call → verdict `DELIVERY: done | incomplete` → deliverable
(board card in Review, note `agent-<name>-latest` via quarantine, file, notification) →
failure / timeout / budget → incident in Operate → audit `agent.*`.

## B. Release pipeline

Rules: [docs/RELEASING.md](../RELEASING.md). Upgrade and rollback for operators:
[docs/UPGRADE.md](../UPGRADE.md).

| Stage | Content |
|---|---|
| 1. Branch + tests | work on a branch / worktree; full suite with Postgres (`pytest`), `ruff`, `tsc`, `next build`; end-to-end tests that drive the real bundled Claude CLI against a fake model; a removed filter must turn a test red (mutation check on isolation code) |
| 2. Semver check | patch = fixes only; a new `SOKKAN_*` variable, route, field, MCP tool, column or **changed default** = minor; a break = major; security exception approved by the maintainers. Check: `git diff vX.Y.Z..HEAD` |
| 3. Changelog | `VERSION` + `CHANGELOG.md` entry with *Upgrade notes* for every behaviour change; a patch keeps the name of its minor |
| 4. Cut | the maintainers' release script: tag (never moved or deleted), immutable tarball named by its build hash on `sokkan.ch/dist`, `sokkan-latest` pointer, site and notes |
| 5. Channels | self-hosted: the installer (`curl -fsSL https://sokkan.ch/install.sh \| sh`) and the in-app update notice; managed cloud: rollout by the operator, one-click update in Profile; public demo |
| 6. Roll back | `./scripts/rollback.sh <hash>` — replaces the code with that release, keeps `.env`, workspace and volumes |

**Enterprise features and releases.** A feature ships in a release and is enabled per instance
afterwards (registry, [FEATURES.md](FEATURES.md)); turning a feature on is a configuration
change, not an upgrade, and turning it off again is the first rollback to try.
