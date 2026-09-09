# SOKKAN 2.0.0 — "Memory, guaranteed"

The 2.0 is about turning the promise at the heart of SOKKAN — *every session
starts with your project's memory* — from an instruction the model was asked
to follow into a mechanical guarantee.

## Memory

- **Deterministic recall at spawn.** The server performs the semantic search
  itself and injects the top notes into the session's first message. If the
  memory is empty or the embedding backend is down, the classic ritual
  remains as fallback — but the happy path no longer depends on model
  obedience.
- **Wikilinks are first-class.** `[[links]]` (with `[[target|label]]`
  aliases) are parsed at index time into the store; backlinks are served
  from the DB; sessions navigate the graph with the new auto-approved
  `memory_links` MCP tool.
- **Bounded priority boost.** `priority: high` is now a multiplicative,
  configurable nudge (`SOKKAN_PRIORITY_BOOST`) instead of a flat bonus that
  could outrank genuinely relevant notes.
- **Graph you can work with**: filter by note type, isolate connected
  clusters, spot orphan notes at a glance.
- **Memory onboarding**: on a fresh repo, **⚡ onboard** (Memory tab) spawns
  a session that writes the first notes — conventions, ports, architecture —
  useful memory in five minutes instead of a cold start.

## Working

- **Playbooks** — curated session templates: refactor, debug, ops incident,
  code review, memory digest, memory onboarding. Pick one at spawn; it
  shapes the mission, the HITL guardrails stay on top.
- **Cost budgets** — estimated-USD budgets per session and per day
  (Profile → Organisation). A session warns at 80% and stops accepting new
  turns at its budget; raising it is an explicit human decision. The daily
  budget warns at spawn and colors the Costs tile.
- **Preview, structured** — per-file +/- counters, click a file to filter
  the diff, and an optional per-repo `test_cmd` with a human-triggered
  **run tests** button (pass/fail badge, failure output inline). The
  Preview tab now works in the Docker install.

## Fixes

- The Costs tab was empty on every Docker install (transcript directory
  resolution ignored `CLAUDE_CONFIG_DIR` and the workspace slug).
- CI is green again (stale test labels from the card-journal i18n change).
- Internal developer defaults leaked into the OSS preview config — purged;
  preview URLs are now an explicit `host` key.
- The embedding model shown in Memory stats now reflects the backend
  actually loaded.
- `memory/README.md` rewritten — it described a pre-extraction internal
  layout that never existed in this repository.

## Upgrading

Re-run the installer from the parent directory (`curl -fsSL
https://sokkan.ch/install.sh | sh`) — `.env` and data volumes are preserved.
The memory index migrates itself (a `links` table and one wallet column are
added on first boot); no manual step. Managed cloud instances: **Infra → My
fleet → ⬆ update**.
