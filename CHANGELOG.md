# Changelog

Notable changes, newest first. Versions: semver + release hash (see
`https://sokkan.ch/dist/VERSION`); dates are release days.

## 3.1.1 — 2026-10-07 — "Crew on show"
- **Crew in read-only for viewers** — `SOKKAN_CREW_VIEWER_READONLY=1` (off by default).
  A viewer then sees the deck and opens every agent (Settings, Live, History, the
  deliverables) but changes nothing: every write route stays `403`, the action buttons
  are greyed out with a "read-only" tooltip, secrets show by name only, quarantined
  memory notes are not shown. With the flag on, a dev also *reads* the agents of
  others, and still only changes their own.
- **A living Crew on the public demo** — `SOKKAN_DEMO_CREW=1`, honoured only on the
  demo instance (`SOKKAN_DEMO_BANNER=1`): the scheduler is replaced by a simulator that
  never opens a session nor calls a model. Agents fire at their hour and one agent
  runs back to back, replaying a recorded run in the Live tab, labelled *simulated*.
  `backend/demo_crew.py seed <crew.json>` writes the fictional crew (idempotent,
  validated like the API, refuses copy-pasted purpose / deliverable / done criteria).
  The demo banner gets a fifth stop: the agents.
- **Failed agent runs open an incident in Operate** — `SOKKAN_AGENTS_INCIDENTS=1` (off
  by default). A run that ends failed, timeout or over budget opens the agent's
  incident, linked to the agent and the run; ONE open incident per agent, later
  failures join it (×N), the next successful run resolves it. The first failure is
  notified once through Operate's channel instead of the agent's own ping.
- **Links between Operate and Crew** — an incident shows the agent runs its alert
  started (and, for an agent incident, the failed run) and opens Crew on that run;
  History shows the incident that triggered a run, the incident a failure opened, and
  the board card a run filed (opens the card). Deep links:
  `/?tab=crew&agent=<id>&run=<id>`, `/?tab=operate&incident=<id>`. Read-only viewers
  follow the links, nothing more.
- **Fix: the board MCP now knows which SDK session calls it.** `open_preview` from a
  session spawned by a card (or any cockpit chat / agent run) wrote an empty
  `session_id` and `tag` — the server only looked at tmux, and could even inherit the
  API's own tmux pane. It now reads `SOKKAN_SESSION_ID`, set by the API for every
  embedded MCP server; cards created from such sessions are attributed too.
- "Done when" is a multi-line field in the agent settings.

## 3.1.0 — 2026-10-07 — "Crew up"
- **Agents, in a new Crew tab.** An agent is a named, owned job: model, purpose,
  expected deliverable and when it is done, trigger (manual, one-shot, cron in
  Europe/Zurich wall-clock time, or an Operate alert), allowed tools and MCP servers,
  vault secrets by name, budget and time limit per run, what may run without asking,
  and where the deliverable goes (board card in Review, memory note, file,
  notification). Spec: `docs/AGENTS.md`.
- **The deck.** One card per agent, in four columns that are its state: idle (blue),
  armed (green), running (orange — the card breathes; a static ring under
  `prefers-reduced-motion`), error (red). Labels and icons, never colour alone. A click
  opens the agent above the deck: Settings (everything editable, approve/reject),
  Live (the running session, approve a waiting tool call right there), History (status,
  cost, tokens, duration, deliverable, transcript).
- **Built in a chat.** "New agent → Build it in a chat" opens a session (`new-agent`
  playbook) that asks one question at a time, recaps, then creates the card. Nina does
  the same in her chat and hands back a card you create in one click. The form is still
  there.
- **`sokkan-agents` MCP, in every session**: create_agent, update_agent, list_agents,
  get_agent, run_agent_now, pause/resume/archive_agent, list_runs, get_run. A session
  only proposes: a new agent, or a change to an approved one, waits for a human in Crew.
  Inside a run the server is read-only (agents do not breed agents).
- **A run is an ordinary session**: memory recalled at spawn, the agent's policy (tools
  outside its list are not even offered, `auto_approve` rules in Claude Code syntax,
  only the vault secrets it names, `max_budget_usd`), the HITL ping when a call waits
  for you. The deliverable is stored with every secret value replaced by
  `[secret:NAME]`; cost, tokens and turns come from the SDK result.
- **Scheduler**: no double run (a scheduled occurrence is unique per agent, a run starts
  through an atomic claim), one live run per agent, `SOKKAN_AGENTS_MAX_CONCURRENT`
  (default 2), runs cut by a restart become `interrupted` and notify, occurrences missed
  while the API was down get one catch-up run if younger than `SOKKAN_AGENTS_MISFIRE_S`
  (6 h). DST: a time in the spring-forward gap is skipped, the repeated autumn hour runs
  once. `SOKKAN_FEATURE_AGENTS=0` turns it all off.
- **Owner and access**: every agent has an owner; a dev sees and manages their own, an
  admin all of them; viewers do not see Crew yet.
- **Approval modes** — `SOKKAN_AGENTS_APPROVAL=owner|admin|four_eyes` (default `owner`):
  `admin` = only an admin activates an agent or applies a change to an approved one;
  `four_eyes` = the approver must be someone other than the proposer and the owner, for
  creation and changes alike. The deck says *needs a second approver* / *needs an admin*.
- **Memory written by agents is quarantined.** A note an agent run writes (its `memory`
  output or a `memory_write` from inside the run) is kept outside the memory, with its
  provenance (agent, run, date), and is never recalled — not at spawn, not by
  `memory_search`, not by the per-turn hooks — until a human approves it, from the run in
  Crew or from the new **Quarantine** panel of the CortHeXis tab. Reject archives it.
  Agents read the outside world; their notes do not get to brief your next session unread.
- **Secrets by name for human sessions too, optional** — `SOKKAN_SESSION_SECRETS=all|named`.
  `all` (the 3.1 default) keeps today's behaviour: a session gets the whole vault. `named`:
  a session gets only the secrets picked when it is opened (or listed by its playbook).
  **`named` will become the default in 3.2** — try it now.
- Defaults validated: 2 concurrent runs, 6 h catch-up window — `SOKKAN_AGENTS_MAX_CONCURRENT`,
  `SOKKAN_AGENTS_MISFIRE_S` to change them (both passed through `docker-compose.yml`).
- No existing API changes; `POST /api/observability/alert` also returns `agent_runs`,
  `POST /api/spawn` accepts an optional `secrets` list.

## 3.0.2 — 2026-10-07 — "One memory"
- **Magnitude: images reach vision models.** Images — including the ones Claude Code
  gets back from reading a .png — used to be dropped by the local shim. With
  `MAGNITUDE_VISION=1` (a vision model served with its mmproj) they are forwarded;
  otherwise the model is told an image was left out and why.
- **Magnitude: works with Claude Code 2.1.29x on strict Qwen3 templates.** System
  messages sent mid-conversation answered 400 "System message must be at the
  beginning"; they are now passed as tagged user content.
- **Magnitude: one immediate retry when llama-server drops the connection** before
  answering (never once the response has started).

## 3.0.1 — 2026-10-03 — "One memory"
- **The 2.x memory migration no longer stops on a note owned by another user.** When a
  note file belongs to someone else (e.g. created by root while the API runs as
  `sokkan`), restoring its date after a rename failed with "Operation not permitted" and
  the migration stopped at the repair step (the 2.x index kept serving, nothing lost).
  A rename keeps the date anyway: the migration now logs it and goes on.

## 3.0.0 — 2026-10-03 — "One memory"
- **A new memory engine, CortHeXis.** The SQLite index (`memory.db`, vectors as
  JSON scanned in Python at every search) is replaced by a Postgres + pgvector
  store (`db` service): hybrid search (dense HNSW + lexical on name/description
  and body), index generations (a new model is indexed in the background while
  the current one serves, then switched atomically), dates with their provenance
  in every result. The `.md` notes stay the source of truth. Load test: p95
  52-115 ms at 250 000 chunks in a 1 GB container.
- **Local embedding models, three profiles.** `corthexis-embed` (llama.cpp) serves
  EmbeddingGemma-300m by default — downloaded at first run only after its licence,
  the Gemma Terms of Use, is accepted; declined or undecided = multilingual-e5-base
  (MIT). `leger`, `standard`, `gpu`, recommended by Magnitude from cores, RAM and
  GPU (`./scripts/memory-setup.sh`). Bench (300 questions): MRR 0.82 on CPU, 0.88
  on GPU with the reranker, against 0.55 for the 2.x model.
- **Recall at every message, and in every sub-agent.** SOKKAN installs two hooks in
  every session it starts (chat and terminal): each message brings the related notes
  into the context (top 4, a threshold calibrated per embedding model, a note named in
  the message always comes, no note twice in a session), and a sub-agent started with
  the Task / Agent tool receives the recall of its own task in its prompt — until now
  it started with nothing. Every recall is recorded (`GET /api/memory/recall-log`):
  which session or sub-agent received which notes, with which score, from which index.
- **The 3.0 memory store is the default**: notes are indexed into Postgres + pgvector
  at start, when a file changes (~6 s) and periodically; `sokkan memory
  index|search|get|status`. `CORTHEXIS_MEMORY_BACKEND=auto` (default) migrates a 2.x memory first (below),
  `sqlite` keeps the 2.x index, `postgres` = the store only.
- **Automatic migration from 2.x, without loss** (`docs/UPGRADE.md`). At the first
  start the notes are archived in the data volume, then repaired (one naming
  convention, orphan updates merged, broken frontmatter rewritten — the plan is
  logged and shown before it is applied), their dates imported (frontmatter first,
  else the 2.x file date, labelled as reconstructed) and re-encoded into the
  store. Two date tests (on the files, then in the store) and a check that every
  2.x note is in the store gate the switch; until then the 2.x `memory.db` keeps
  serving, read-only, so search never stops. Interrupted at any step, it resumes.
  Rollback: the 2.3 image with the untouched `memory.db`.
- **2.x settings are kept.** `ML_SERVICE_URL` becomes the `remote` profile and a
  custom `SOKKAN_EMBED_MODEL` the `legacy` profile (same vectors as before);
  `SOKKAN_EMBED_MODEL` is now declared in the compose file, so a value in `.env`
  actually reaches the container.
- **New API**: `GET /api/memory/migration` (steps, repair plan, progress, checks,
  log, which index serves) and `POST /api/memory/migration/approve` (admin).
- **The installer** sets up the memory profile and asks about the model licence;
  unattended: `SOKKAN_ACCEPT_GEMMA_TERMS=1|0`.
- **Updating a self-hosted install is tested end to end** (`tests/e2e_upgrade/`, results
  in its `RESULTS.md`): real installs of 0.1.0, 2.0.1, 2.2.0 and 2.3.0 with a fictional
  memory, a session and board cards, updated by the installer and by the manual steps,
  then rolled back. What it changed:
  - any Docker Compose v2 from 2.12 runs the compose file (no `include:`, no nested
    default — both needed 2.20); GPU overrides go through `COMPOSE_FILE` in `.env`;
  - `./scripts/rollback.sh <hash>` replaces the code instead of extracting an older
    tarball over a newer folder (the older build tripped on the newer files);
  - the web font ships with the code (no Google Fonts download during the build);
  - a data volume of 0.1.0 (owned by root) is handed over at start, and a `memory.db`
    of 0.x-1.x keeps serving searches during the migration.
- **Note repairs keep links**: a note renamed from its file name (`Team Calendar` in
  `TeamCalendar.md` → `teamcalendar`) brings `[[team-calendar]]` and every other
  variant of its old name along. The review no longer reports an Exoscale key
  identifier alone (its public half) as a secret.
- **The memory tab is now CortHeXis: the memory, visible and repairable.** A live graph
  of the notes (links, missing notes, meaning, age, health), the note with its problems,
  the memory's health score and its history, and the recall bench. The review runs every
  hour in the backend — where the sessions start — so "the memory server does not answer"
  is checked the way a session would see it.
- **Repairs in one click, never without approval.** Re-point a broken link, merge two
  notes, rename a note to the convention, close a dormant project: each one shows the
  exact diff first and writes only after someone approves it (journaled, with a copy of
  what it replaced). Cases that need judgement open a « Memory curation » session loaded
  with the findings.
- **Alerts.** One digest a day when something changed, at once on a critical problem,
  through the notification channels already configured.

## 2.3.0 — 2026-09-14 — "Memory writes back"
- **Sessions can write to memory.** Recall was solid — `memory_search`,
  `memory_get`, `memory_links`, plus a deterministic pre-seed at spawn — but the
  memory was read-only: a session asked to record a decision had no tool for it,
  had to guess where the notes live, and ended up running `find /`. New
  `memory_write(name, description, body, priority, type, overwrite)` MCP tool:
  it writes the note atomically, in the project format
  (frontmatter + `[[wikilinks]]`), refuses to clobber an existing note unless
  you ask, and returns a readable error — with the remedy — when the memory
  directory is not writable. The index and the embeddings follow on their own.
  It is a write, so it goes through the approval gate like any other: you see
  the note before it enters the memory — reads stay auto-approved.
  Found the hard way: two candidates on a hands-on trial were both asked to
  document their decisions, and neither could.
- **Every session is told how.** The spawn seed now carries one line naming the
  memory directory and the write tool, in both the pre-seeded and the fallback
  form. Until now only the `onboard-memory` and `digest` playbooks mentioned it,
  so a free-form session — the common case — knew how to read the memory but not
  how to add to it.
- **A playbook that needs a subject no longer starts without one.** Spawning
  "Debug" with an empty subject produced the bare prompt `Bug to investigate:`
  and sent the agent exploring at random; `POST /api/spawn` now answers 400 and
  the button in the session rail stays disabled until you type the subject.

## 2.2.0 — 2026-09-11 — "Nina, without the wait"
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
- **Fix: an empty answer when the endpoint does not stream.** The SSE reader
  looked for `data:` frames and found none when the endpoint replies with a
  single JSON body — which is what the managed gateway does on house accounts,
  precisely Nina's *fallback* path. A failure of the primary would have produced
  an empty answer instead of a working fallback. The reader now checks the
  content type and reads the whole body when it is not an event stream.

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
