# Changelog

Notable changes, newest first. Versions: semver + release hash (see
`https://sokkan.ch/dist/VERSION`); dates are release days.

## Unreleased
- **Nina's prompt got smaller.** The product knowledge base was re-injected
  whole on every turn — 7.5 kB, two thirds of the system prompt. It now carries
  the full table of contents (so she always knows what exists and where to point
  you) plus the spine and the two sections closest to your question. On
  self-hosted silicon, where prefill costs ~3.3 ms/token, that is ~1,500 fewer
  tokens to chew before the first word appears: measured 12.8 s → 9.2 s to first
  token on a 80B, with no change in eval score. Selection is lexical and
  cross-lingual (an English question finds the French section), normalised by
  section length so the longest file cannot win by accident.
- **Nina streams.** Her answers now arrive token by token
  (`POST /api/assistant/chat/stream`, SSE) instead of landing whole after a
  spinner. The model's throughput is unchanged — what changes is that you read
  while it writes. This matters most on self-hosted silicon, where decoding runs
  at ~37 tok/s: a detailed answer takes ~30 s to finish but starts appearing
  immediately. A DevOps assistant should be free to give a long answer when the
  question is an infrastructure trade-off; streaming is what makes that
  affordable, rather than capping her output. Failover to the secondary endpoint
  stays possible until the first byte — after that the stream is committed.

## 2.1.1 — 2026-09-11 — "Nina, in your language"
- **Fix: Nina now answers in the language you asked in.** The persona already
  said so, but buried in ~3,100 tokens of context the instruction was ignored
  about half the time by locally-served open models — measured on both
  `gpt-oss-20b` and `qwen3-next-80b`, which answered a question asked in
  English in French. A frontier model obeyed, which hid the defect for anyone
  serving through the managed gateway. The language is now decided in Python
  from the message and stated explicitly at the end of the system prompt, where
  it carries most weight; an ambiguous or very short message falls back to the
  persona. Same doctrine as deterministic memory recall and the dossier
  allowlist: what can be decided in code is not left to the model's goodwill.

## 2.1.0 — 2026-09-11 — "Nina, briefed"
- **Nina knows your instance (S2).** Her prompt now carries a read-only client
  dossier — plan, fleet resources and their `.fleet` names, orderable catalogue
  with prices, credit balance and spend, agent-session consumption — plus the
  memory notes relevant to the question asked. She answers with your real
  figures instead of generalities. Two structural guardrails: the dossier is
  built from an **allowlist** of fields, so a database connection URI cannot
  reach the prompt even though the portal returns one; and memory is
  **pre-retrieved** rather than exposed as a tool, so recall does not depend on
  the model choosing to call it — the same doctrine as spawn-time recall in 2.0.
  Every source is isolated: a dead one drops a line, it never breaks the chat.
- **Assistant failover**: `SOKKAN_ASSISTANT_LLM_FALLBACK_*` defines a second
  endpoint. Nina prefers the primary, falls back on connection failure, and
  retries the primary every two minutes — so you can point her at your own GPU
  box without her going down when it is off.

## 2.0.1 — 2026-09-10 — "Nina, visible"
- **Fix: the embedded assistant was never reachable.**
  `SOKKAN_FEATURE_ASSISTANT` and the three `SOKKAN_ASSISTANT_LLM_*` variables
  were documented and shipped, but were missing from the `api` service's
  `environment:` block in `docker-compose.yml` — Compose interpolates `.env`
  into the compose file, so an undeclared variable never enters the container.
  `/api/features` therefore reported `assistant: false` on every instance,
  self-hosted and managed alike, and Nina's panel stayed hidden. Declared now.
- **Nina can run on your own hardware**: `SOKKAN_ASSISTANT_LLM_API=openai`
  points her at any `/chat/completions` endpoint (Ollama, vLLM, LiteLLM)
  instead of an Anthropic-shaped one — e.g. `URL=http://<host>:11434/v1`,
  `MODEL=phi4:14b-q4_K_M`. Default stays `anthropic`; managed instances are
  unaffected.

## 2.0.0 — 2026-09-10 — "Memory, guaranteed"
- **Deterministic memory recall at spawn**: the server performs the semantic
  search itself and injects the top notes into the session's first message.
  The moat stops depending on the model obeying an instruction — it is now a
  mechanical guarantee (the ritual remains as fallback on empty memory).
- **Wikilinks are first-class**: parsed at index time into the store (with
  `[[target|label]]` aliases), backlinks served from the DB, and a new
  auto-approved `memory_links` MCP tool lets sessions navigate the graph.
- **Priority boost bounded**: `priority: high` is now a multiplicative,
  configurable nudge (`SOKKAN_PRIORITY_BOOST`) — a weak match can no longer
  jump above genuinely relevant notes.
- **Knowledge graph**: filter by note type, isolate connected clusters.
- **Playbooks**: session templates (refactor, debug, ops incident, review,
  digest, memory onboarding) — spawn selector + `GET /api/playbooks`.
- **Memory onboarding**: one click on a fresh repo writes the first notes
  (conventions, ports, architecture) — useful memory in 5 minutes.
- **Cost budgets**: estimated-USD budget per session (warn at 80%, HITL
  hard-stop at 100%) and per day (spawn warning, Costs tile) — Profile →
  Organisation.
- **Preview, structured**: per-file +/- counters and file filtering on the
  diff; optional per-repo `test_cmd` with a human-triggered "run tests"
  button; the Preview tab now works in the Docker install.
- Fixes: Costs tab was empty on Docker installs (transcript dir resolution);
  green CI (stale test labels); internal defaults purged from previewenv;
  truthful embedding-model id in stats; memory README rewritten.

## 1.6.1 — 2026-08-31 — "Pin the mast"
- **Fix: fresh installs were broken** — the unpinned `mcp` dependency started
  resolving to mcp 2.x (FastMCP renamed → `ModuleNotFoundError`, api
  crash-loop on any new build). Now pinned `mcp>=1.2,<2`. Existing installs
  keep their built image and were not affected; re-run the installer if you
  hit the crash on a new machine.
- README: European positioning up front, honest comparison table, First
  steps guide link. New site pages: [/en/trust](https://sokkan.ch/en/trust/)
  and [/en/docs/first-steps](https://sokkan.ch/en/docs/first-steps/).

## 1.6.0 — 2026-08-11 — "Sovereign inference"
- **SOKKAN Inference**: managed inference now runs on our own sovereign-EU
  gateway with agent-native tiers, instead of a single fixed upstream. Pick a
  coding tier per instance from **Settings → Model**: **Ship** (fast coding
  workhorse — Sonnet-level on our coding benchmark, ~30× cheaper), **Fast**
  (economical generalist), **Deep** (frontier-open reasoning, the boost tier).
  Requests escalate automatically when the fast tier struggles — never a flat
  "not capable" — and fall over to a second EU provider on an outage. A
  deterministic guard blocks secrets (API keys, tokens, private keys) from ever
  leaving in a prompt. Prepaid in CHF, billed per token, data stays in the EU
  (GDPR, no US CLOUD Act). New managed instances default to the Ship tier.

## 1.5.0 — 2026-08-11 — "Your own silicon"
- **Magnitude**: find out what your machine can really run — locally, privately.
  Pair the cockpit with a tiny host agent (`python3 -m magnitude`, pure stdlib)
  that profiles your GPU/RAM, benchmarks a curated catalogue of open models
  with real numbers (gen tok/s, prefill speed, watts, €/Mtok), then downloads
  and serves the one you pick with llama.cpp in a single click. One more click
  connects it to the session router through a local Anthropic-compatible
  endpoint: every new session runs on your own hardware — zero cloud, zero
  cost per token. Opt out per instance: `SOKKAN_FEATURE_MAGNITUDE=0`.
  Named in homage to [Magnitude](https://github.com/magnitudedev/magnitude)
  by Tom Greenwald and Anders Lie, whose hardware-profiling onboarding
  inspired this feature (independent from-scratch implementation, no code
  shared, not affiliated).
- **Magnitude is multi-machine**: the tab is a registry of nodes — pair every
  machine you own (the office workstation, the GPU box, a MacBook), each with
  its own token, hardware profile, benchmarks and served model; pick which one
  powers SOKKAN. Per-node endpoint override for remote nodes (`shim_url`,
  default `SOKKAN_MAGNITUDE_SHIM_URL`). Existing single-agent state migrates
  automatically.
- **One-line install**: pairing a machine is now a single
  `curl … /api/magnitude/install.sh?token=… | sh`. It lays down a standalone
  Python when the host has none (macOS without Command Line Tools — no sudo,
  nothing outside `~/.sokkan`), fetches the agent, and pairs. Validated on
  Linux and Apple Silicon; `python3 -m magnitude` remains the manual path.

## 1.4.0 — 2026-08-10 — "Open for missions"
- **SOKKAN Missions link**: the header now shows a small live counter of open
  missions on the [SOKKAN Missions marketplace](https://sokkan.ch/missions/) —
  fixed-price client projects you can deliver and get paid for, in a provided
  per-mission environment. The cockpit fetches aggregate public counters only
  (a plain GET, no identifier of any kind is ever sent), fails silently when
  offline, and hides itself when there is nothing open.
  Opt out per instance: `SOKKAN_FEATURE_MISSIONS_LINK=0`.
- New backend feature flag `missions_link` in `/api/features`.

## 1.3.1 — 2026-08-04 — "Clear view"
- **Session history survives restarts**: the session pane now rehydrates its
  full history (user turns, assistant messages, tool calls and results) from
  the persisted transcript after a cockpit restart — panes no longer come back
  empty.
- **Viewers can read the chat**: the viewer role is read-only, not blind — the
  agent stream now accepts viewer connections; every mutation (messages,
  approvals, interrupts, mode changes) stays gated at dev and above, with an
  explicit read-only notice.
- **Journal & Costs for viewers**: the audit journal and the cost/usage view
  are read-only supervision data — both are now visible to the viewer role
  (mutations everywhere else unchanged).
- **Mobile layout**: on small screens the Sessions view now stacks — full-width
  session list, full-screen pane with a back bar, single-column panes,
  scrollable tab bar.

## 1.3.0 — 2026-07-23 — "Companion"
- **`sokkan` CLI**: a zero-dependency terminal companion for the cockpit —
  `sokkan login/spawn/status/sessions/board/card/mem/note/digest/health`.
  Install: `pipx install "git+https://github.com/ninabot-ch/sokkan"`.
  Spawn and inspect from the terminal; approvals stay in the cockpit (HITL
  unchanged). Local-token auth.

## 1.2.0 — 2026-07-23 — "Open helm"
Multi-provider, a self-summarizing memory, an easier first contact — and Nina.
- **Nina, the embedded DevOps assistant (S1)**: a floating 🧭 in the cockpit that
  knows the product — sessions, memory, fleet, runbooks — and answers next to
  your work. Strict guardrails: she never sees your secrets or your code, and
  touches nothing — she guides, you hold the helm. Included on every SOKKAN
  Cloud instance (zero setup); self-host ships her behind
  `SOKKAN_FEATURE_ASSISTANT=1` with the model of your choice.
- **Multi-provider models**: point sessions at any Anthropic-compatible endpoint
  (Kimi/Moonshot, GLM/Z.AI, DeepSeek, or a local LiteLLM→Ollama proxy) from
  **Profile → Model** — base URL + key + model, applied per session, presets
  included. Sessions still run the Claude Code engine.
- **Priority notes**: mark a durable fact `priority: high` — it gets a recall
  boost, a ★ in the cockpit, and tops the generated `MEMORY.md`.
- **Memory digest**: one click spawns a session that condenses the whole memory
  (+ recent git history) into a `project-status` note.
- **Knowledge graph**: the Memory/KB tab renders the `[[wikilinks]]` as an
  interactive force-directed map (no dependencies added).
- **Sample workspace**: `examples/fastapi-notes/` — a tiny FastAPI project plus
  the memory notes that make recall click, with a board-seeding script.
- **`scripts/doctor.sh`**: read-only install checkup (prereqs, `.env`, stack health).
- CI now cross-builds both images for **linux/arm64**; new integration tests for
  the critical flow (spawn → memory pre-seed → tool approval) and the LLM modes.
- GitHub Discussions opened; `good first issue` backlog seeded.

## 1.1.0 — 2026-07-22 — "Operate"
The loop doesn't stop at deploy. New **Operate** capabilities — run your
production from the cockpit, with agents that share the project memory:
- **Observability**: a Prometheus + Grafana + Loki stack your sessions read and
  write via the `sokkan-observability` MCP (« build a dashboard for my p95 »).
  Managed cloud provisions it as a fleet resource; self-hosted wires its own.
- **Alerts → supervised incidents**: a production alert becomes an incident *and*
  spawns a pre-seeded diagnosis session (metric + context + memory) that waits
  for your go-ahead. Post-mortem goes back to memory.
- **Secrets vault**: encrypted at rest, injected into sessions as env vars, never
  shown to the UI or the model.
- **HITL push**: get pinged (Telegram/webhook) when a session waits on your
  approval and you've stepped away.
- **Runbooks**: replay a `runbook-*` memory note as a guided, supervised session.
- **Deploy & rollback** a Docker image to a fleet worker (managed cloud).

See [`docs/OPERATE.md`](docs/OPERATE.md).

## 1.0.0 — 2026-07-22
First stable release.
- **English UI** throughout (the cockpit was previously part French).
- **Security hardening** (pre-1.0 review): cf-access mode no longer falls back
  to owner off the loopback path; the ttyd terminal requires admin + the tmux
  feature flag; route hostnames/ports are validated before the edge Caddyfile;
  local-login rate-limiting keys on the real client IP. Control plane: closed a
  cross-tenant `*.sokkan.ch` namespace collision, restricted the fleet SSH-key
  comment, and made the self-service plan change CSRF-proof (session cookie +
  token).
- **Upgrade & rollback** documented end to end (`docs/UPGRADE.md`), self-hosted
  and managed; `SECURITY.md` added.
- Everything in 0.9.0 below (fleet web exposure, one-click/managed upgrades).

## 0.9.0 — 2026-07-22
- **Self-hosted upgrade path**: re-running the installer
  (`curl -fsSL https://sokkan.ch/install.sh | sh`) now upgrades an existing
  install in place — `.env` and data volumes preserved, short rebuild. The
  daily update check is now surfaced in **Profile** with the exact command.
- **Managed**: one-click cockpit update from the fleet tab; new releases roll
  out to the managed fleet automatically.

## 2026-07-17
- **Fleet web exposure** (managed): publish fleet services on
  `<name>-<tenant>.sokkan.ch` subdomains (via the tenant tunnel) or on your
  own domain (one CNAME, automatic Let's Encrypt TLS), managed from the
  fleet tab. Free, admin-gated, audited.

Older changes: see the git history.
