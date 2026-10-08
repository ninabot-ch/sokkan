# Changelog

Notable changes, newest first. Versions: semver + release hash (see
`https://sokkan.ch/dist/VERSION`); dates are release days.

## 3.4.0 — 2026-10-08 — "Bridge"
The command bridge, and the bridge to Teams. A department head creates a project with Nina, sees
it in Helm, follows it, acts on a reframe suggestion and reads the brief — without docs (the same
journey was abandoned on 3.3.0). Helm and classification are **stable**; a sign-out at the
identity provider now ends the cockpit session; Teams is ready to plug into a real tenant.

### Helm → stable
- **The manager journey goes through** (played as a persona on a disposable instance, Nina on a
  real model): « ✦ New project with Nina » in Helm; Nina gets the projects the person can create
  in and their people (`GET /api/helm/targets`) — no invented owners, no copy-paste instructions;
  the proposal says where it is created (steered projects first), owners are picked among the
  project's people, decisions editable; the link after creation opens the right project.
  `POST /api/helm/projects` reports the owners it could not assign (`dropped_owners`).
- **Nina**: answer budget `SOKKAN_ASSISTANT_MAX_TOKENS` (default 2048, was 800 — a reasoning model
  answered empty or cut a proposal); `reasoning` (vLLM) never shown; a cut or empty answer says so
  (FR/EN); the knowledge sections follow the conversation; Markdown rendered; the panel's words
  follow the browser language; a sent message is never replaced by the answer.
- **« ☀ My brief »** in Helm: the morning brief on demand, for oneself or (managers) the whole
  project (`/api/helm/brief?all=1`); only non-empty sections.
- Suggestions say what Approve / Accept the new scope / Ignore did; accepting the new scope
  resolves the scope suggestion at once; « card not found » explained; long titles wrap.
- Stable criteria written in docs/HELM.md.

### Classification → stable
- **Helm applies the reader's clearance** (deck, popout, kanban, activity, costs, suggestions,
  brief, board card dialog, board MCP): a manager never reads a card above their clearance —
  not even its title in the parent's « child added » event; a classified ancestor shows
  « (classified) ».
- **The access log is readable in the cockpit** (Setup › Organization › Classification: filters,
  export of what is shown); reads of notes above the auditor's clearance are counted
  (`hidden_above_clearance`, CSV header `x-sokkan-hidden-rows`).
- Cards reclassify from their dialog (raising immediate, lowering with an inline reason).
- 29 mutations (the 18 filters of 3.4 + 11 of Bridge) — all red. SECURITY.md § 8.

### OIDC back-channel logout (lot 6 completed)
- `POST /api/auth/backchannel-logout` — OpenID Connect Back-Channel Logout 1.0: the IdP's signed
  `logout_token` (JWKS, iss, aud, iat, `events`, sid/sub, no nonce, jti used once) ends the cockpit
  sessions born from that IdP session: cookies refused, chat panes / terminals closed, live SDK
  sessions interrupted then closed when no other IdP session of the person remains; `sub` alone
  ends them all. Not a revocation: account, agents and forge tokens untouched. Audit
  `auth.backchannel_logout`.
- The cockpit cookie now carries the id_token's `sid`; the callback records sid/sub in identity.db.
- Entra ID (no back-channel): front-channel `GET /api/auth/frontchannel-logout?sid=` behind
  `SOKKAN_OIDC_FRONTCHANNEL_LOGOUT=1` (end-only; admin-side cuts stay SCIM / Revoke now).
  OPERATIONS.md § 2.2 (Authentik 2025.8+, Entra ID).

### Teams — ready to plug in (stays experimental)
- App package: `GET /api/admin/teams/package`, `scripts/teams-manifest.py` (manifest v1.17
  validated against the official schema, icons generated, valid domain = the public host).
- `scripts/teams-register.sh`: Teams Developer Portal path by default (no Azure subscription;
  changes nothing, prints the steps and the env lines); `--mode azure` with `--dry-run`.
- HITL approvals as Adaptive Cards 1.5 per the Universal Action Model (Action.Execute in an
  ActionSet + Action.Submit fallback, refresh, fallbackText), replaced by « Approved by X » once
  decided.
- **Pending approvals pushed proactively** to the project's Teams channel (agents waiting for
  activation or a change, tool calls of a run) and replaced once decided — in Teams or in the
  cockpit; an approval above the channel's level is announced without content.
  `SOKKAN_TEAMS_PROACTIVE_S`.
- `SOKKAN_TEAMS_APP_PASSWORD_FILE` (the secret from a 0600 file rendered from the vault).
- `tests/teams_live/` — the real-tenant checklist as tests (skipped without `SOKKAN_TEAMS_LIVE=1`;
  human steps under the `manual` marker). docs/enterprise/TEAMS-SETUP.md: day-of-the-tenant
  runbook.

### Registry
- `helm` and `classification`: **stable**. `teams`: experimental until validated on a real
  tenant. `multi_project`: stays **beta** (MULTIUSER.md « Status of multi_project »: proven by its
  tests, not yet by a multi-team deployment).

## 3.3.0 — 2026-10-08 — "Helm"
Steer your projects and your secrets. 3.3 makes OpenBao the reference place for an enterprise
instance's secrets and keys — no clear key left on the data volume, backups that carry no key —
and gathers the project-steering work shipped in beta through 3.2 (Helm, the manager's view).
Nothing changes on an existing instance until its operator chooses: the provider is explicit.

### Secrets provider — OpenBao as the reference (feature `secrets_provider`, beta)
- **Where secrets live is now a choice**: `file` (default, the 3.2 files unchanged), **`openbao`**
  (OpenBao / HashiCorp Vault: project secrets in KV v2 under `sokkan/<instance>/<project>/`, every
  data key — vault/BYOK, forge tokens, Teams tokens — wrapped by a transit key that never leaves
  OpenBao: no clear key on the data volume; AppRole or Kubernetes ServiceAccount auth, token
  renewal, TLS, namespaces) or `kubernetes` (Secrets of the namespace). `SOKKAN_SECRETS_PROVIDER`
  (explicit). docs/enterprise/SECRETS.md.
- **Hot migration** `scripts/secrets-migrate.py --from file --to openbao` (idempotent, verified by
  reading back, `--check`, `--purge` shreds the clear keys) and back to files; passphrase-encrypted
  export / import.
- **Rotation** `scripts/secrets-rotate.py`: `--master` (transit rotate + rewrap, operator token)
  and `--data-keys` (new key, every stored value re-encrypted, old key dropped), journaled.
- **Backups**: in openbao mode a set holds no key at all (KV secrets transit-encrypted,
  `OPENBAO_REQUIRED.txt`); `restore.sh` checks every wrapped key unwraps before touching anything,
  `--import-secrets`. In file mode `forge.key` and `teams.key` are no longer inside `data.tgz` in
  clear: they are encrypted with the passphrase like `vault.key` (`--keys-dir` at restore).
- **Helm**: optional one-node OpenBao (`openbao.enabled`, init/unseal documented, auto-unseal out
  of scope), kubernetes auth configured by a post-install Job, the customer's own Vault
  (`openbao.address`, `caSecret`, `auth.method` kubernetes | approle), `values-sks.yaml` on OpenBao.
- **Setup › Secrets**: provider, why, configuration, **Test connection**; a warning on an
  enterprise instance whose keys are files. `GET /api/admin/secrets-provider`, `POST …/test`.
- **The provider is explicit, never implicit**: only `SOKKAN_SECRETS_PROVIDER=openbao|kubernetes`
  leaves files. `SOKKAN_OPENBAO_ADDR` alone changes nothing — the api logs « OpenBao address set
  but provider is file » at startup and Setup › Secrets shows it — so an instance never starts
  answering 503 on its secrets because a variable appeared. An enterprise instance without an
  explicit provider stays on files with the warning in Setup › Secrets.
- **Our cloud and POC** (SECRETS.md § 6.1): the chart's single node for a POC, with an unseal
  runbook; under contract a 3-node OpenBao raft cluster auto-unsealed by transit from a second
  OpenBao, or the customer's Vault; unseal shares 5 / threshold 3 — 2 with the principal
  operator, 1 with the second authorised person, 1 sealed in the company password safe, 1 on
  paper in the physical safe, never 3 in the same place.

### Helm — steering projects (feature `helm`, still beta)
Shipped in beta in 3.2.0 (« 3.3 "Helm" » section below), stabilised through 3.2.x: hierarchical
cards (manager's project card → engineer's cards → sub-tasks), context that flows down into the
sessions, progress that flows up (computed, never declared), the Helm view for project managers,
Nina's breakdown and reframe suggestions, morning brief. Since 3.2.0: Control › Helm in the plane
navigation (3.2.2), read access for project members (`helm.can_view`, read-only demo Captains),
named progress bars and cards readable at 390 px (3.2.2 UI pass), Helm's cards keep the
classification of their parent (3.4 × 3.3). **Status stays `beta`** in 3.3.0: it requires
`multi_project` (beta), its manager journey (break down, reframe, brief) has not had its own UI
pass nor use on a real project yet — only the visitor's read-only view was walked in 3.2.2. It
turns stable when those two are done.

### Upgrading from 3.2
- Nothing changes until you choose. To move an instance's secrets to OpenBao: back up, give the
  api `SOKKAN_OPENBAO_*` (it stays on files and says « OpenBao address set but provider is
  file »), migrate, then set `SOKKAN_SECRETS_PROVIDER=openbao` and restart (SECRETS.md § 4).
- File-mode backups taken by 3.3 keep `forge.key` and `teams.key` out of `data.tgz` (they were
  in clear there): `<key>.enc` with the passphrase like `vault.key`. Without
  `SOKKAN_BACKUP_KEY_PASSPHRASE` no key is in the set at all — keep them yourself and give
  `--keys-dir` at restore.

## 3.2.3 — 2026-10-08 — "Captains"
- **Operate › Costs tells what is billed, and how.** On an instance that runs Claude through a
  Pro/Max login the tab showed hundreds of dollars a day. Four causes, all fixed: (1) every
  transcript of the workspace was counted, including the operator's own Claude Code sessions run
  in the same folder — only SOKKAN sessions (board sessions, agent runs, project workspaces, their
  sub-agents) are counted now, the rest is one « not counted » line
  (`SOKKAN_USAGE_EXTERNAL=include` to count it); (2) one API message was counted once per content
  block (thinking, text, tool call — each transcript line repeats the message's usage):
  deduplicated by message id; (3) models missing from a prefix list (`claude-opus-5-5`,
  `claude-opus-5`) fell to the legacy Opus 15/75 tariff, cache reads included — prices now come
  from a versioned table (`backend/model_prices.json`, public list of 2026-09-25: input, cache
  write ×1.25 / ×2, cache read, output, per model), an unknown model is counted in tokens and
  flagged, never priced at a guess (`SOKKAN_CLAUDE_PRICES` adds or overrides models); (4) every
  token was valued at the API price — each figure now has a **billing basis**: API key (billed at
  the public price), subscription (nothing billed; the API-equivalent is shown apart and labelled),
  SOKKAN Inference (tier price + the gateway's ledger), local engine (0 + tokens), other endpoint
  (`SOKKAN_MODEL_PRICES`). `SOKKAN_BILLING_BASIS=api|subscription` forces the Claude basis. The tab
  shows totals by basis, by model (tokens and dollars per kind), by project, per day and per
  session over 7 / 30 / 90 days, with the method in words. Budgets count the billed cost, and the
  API-equivalent on a subscription. Reconciliation test with a synthetic transcript of known usage
  in every basis. Docs: `docs/OPERATE.md` § Costs.
- **Host names instead of bare addresses.** Operate › Infra (topology cards, Prometheus targets),
  the Prometheus results an agent reads (`instance_name` label), the incidents opened by an alert
  and Magnitude's endpoints show « name (ip) ». The name comes from `SOKKAN_HOSTS` (JSON ip → name,
  new), then `SOKKAN_INFRA_NODES`, `/etc/hosts`, `tailscale status --json` (MagicDNS name) and
  reverse DNS (bounded to 0.5 s, cached). A node absent from `SOKKAN_INFRA_NODES` is no longer
  shown as its IP.
- **Magnitude sees Intel GPUs and the models already running.** The agent (0.2.0) detects Intel
  discrete cards through Level Zero / OpenCL (`xpu-smi`, `clinfo`, `sycl-ls`, PCI ids) before
  Vulkan: every card is a device of the same node (4× Arc Pro B60 = one node, 90.8 GB, class XL,
  L per card), with its VRAM; NVIDIA nodes with several cards are listed the same way. It also
  finds the OpenAI-compatible engines already served on the node (vLLM, llama.cpp, ollama: model,
  port, context, GPU cards from the container's env) every 30 s; the node's section lists them
  under « Engines running » and **Use for sessions** bridges one to SOKKAN through the shim, like a
  model Magnitude launched (action `attach`; the engine is not touched). New:
  `MAGNITUDE_DISCOVER_PORTS`, `MAGNITUDE_ENGINE_CARDS`, `MAGNITUDE_GPU_DEVICES`. Agents 0.1 keep
  working with this cockpit, and this agent with a 3.2.2 cockpit (unknown sync fields ignored).
- **Magnitude can load a model again.** Two reasons nothing started: upstream llama.cpp now
  publishes its `bNNNNN` builds as pre-releases and `releases/latest` points at a source-only
  release, so the first Run or Benchmark on any machine failed with « no llama.cpp prebuilt
  asset » — the agent now takes the newest release that ships a build for the platform, reuses
  a build already on the machine (no silent upgrade between two benches) and still honours
  `MAGNITUDE_LLAMA_TAG`; and a failed start now says why (the line of `llama-server.log`, e.g. an
  option it rejects, out of memory), with what to do on the machine in the cockpit. Run on Intel
  cards uses the SYCL image `ghcr.io/ggml-org/llama.cpp:server-intel` through Docker when the
  daemon is there (on the allowed cards only, GGUF read-only, loopback port, memory/CPU caps),
  else the Vulkan prebuilt; NVIDIA / AMD → Vulkan prebuilt; Apple → Metal; no card allowed → CPU
  (`MAGNITUDE_RUNTIME` forces). Every server gets `--cache-ram 2048` (`MAGNITUDE_CACHE_RAM`).
- **Magnitude shows the live load of each machine.** Agent 0.3.0 samples every 3 s, per card,
  busy %, VRAM used / total, temperature and power (Intel `xe`: debugfs `vram_mm` or DRM fdinfo,
  GT idle residency, hwmon; NVIDIA: `nvidia-smi`; AMD: sysfs), plus CPU %, load and RAM. The
  node's section shows them per card (meters, two-minute history) and for the machine; samples
  stay in memory, never in `magnitude.json`. **`GET /metrics`** (and `/api/magnitude/metrics`)
  exposes `sokkan_magnitude_*` gauges (node up / last seen, CPU, RAM, per-GPU busy, memory,
  temperature, power, engines up, serving) for Prometheus and Operate — `Authorization: Bearer
  $SOKKAN_METRICS_TOKEN` when set, otherwise direct loopback clients only (proxied requests are
  refused).
- **Run stays off the cards that serve production.** `MAGNITUDE_GPU_DEVICES` (card numbers, or
  `none`) is reported to the cockpit, which shows « Cards for Run: … · production: … » and tags
  each card *Run* or *prod*; `none` runs Magnitude's models on the CPU with no backend device at
  all (`--device none`, `MAGNITUDE_CPU_THREADS`). The catalogue's fit is computed where the run
  would happen — the FREE memory of the allowed cards, or the RAM still available — and the API
  refuses (409, with the reason) a Run or Benchmark that does not fit. Benches record where they
  ran; older ones say « on the hardware of that day ».
- **No more endless « waiting for the agent ».** A node silent for 10 minutes, or paired and never
  connected, shows « offline since … » / « never connected » with an Unpair button. The agent
  sends the engines it finds at every pass (0.2 sent them on change only: a cockpit upgraded
  afterwards never showed them) and re-sends its profile or engines when the cockpit asks
  (`resend` in the sync answer).
- **Memory card.** The recommendation names the machine that can serve it and its reason (it
  showed the cockpit's « 8 cores, 12.6 GB RAM » next to a GPU recommendation); a profile a
  Magnitude node can serve is no longer « too small »; the engine's servers and re-ranker are
  shown; on an instance without the 3.0 store it says the profile is set on the server and how;
  preparing a profile change requires ticking that every note will be re-read.
- **`GET /api/version`** (auth-free): `version` (the VERSION file baked into the image), `commit`
  (build arg `SOKKAN_COMMIT`), `dist` (`<version>+<commit>`), `image_tag` and `edition`. A rollout
  check can now require `version`/`dist` to equal what it deployed: `/api/health` alone answered
  200 from the OLD container while the new image was still building. The api image copies
  `VERSION`; build it with `--build-arg SOKKAN_COMMIT=$(git rev-parse --short HEAD)` (compose:
  `SOKKAN_COMMIT` in `.env`).

## 3.2.2 — 2026-10-08 — "Captains"
- **The cockpit is navigated by planes.** Eleven tabs in one row became four planes, each with its
  sub-tabs on a second row: **Control** (Helm · Board · CortHeXis), **Build** (Sessions · Crew ·
  Preview), **Operate** (Incidents · Infra · Costs · Journal) and **Setup**
  (Organization · Engines · Magnitude · Secrets · My account · Notifications). The « Profile &
  organization » dialog is gone: its sections are the Setup plane (the badge menu opens them). A
  sub-tab a person does not have (feature off, role too low) is not shown, and a plane left empty
  is not shown either.
- **« Operate › Operate » is now « Operate › Incidents »** (alerts, incidents with their diagnosis
  session, runbooks): the sub-tab no longer repeats its plane's name. New link
  `/?plane=operate&tab=incidents&incident=<id>` (notifications, board links and Crew's run history
  write it); the old `?tab=operate&incident=<id>` opens the same place.
- **Landing plane by role**: manager / maintainer → Control, developer → Build, ops team →
  Operate, instance admin → the plane they used last (kept per user: `GET/PUT /api/me/nav`,
  `$SOKKAN_DATA_DIR/navprefs.json`). Coming back to a plane reopens its last sub-tab.
- **Deep links**: new form `/?plane=build&tab=crew`. Every older link keeps working — the 11
  `?tab=` values (notifications, Crew ⇄ Operate links, Nina's cards, `?tab=operate&incident=…`,
  `?tab=crew&agent=…&run=…`) and the former Profile sections (`?tab=members`, `?tab=keys`, …,
  `?forge=` back from GitLab) — through one table (`frontend/lib/planes.ts`), tested.
- **Setup › Engines = « Connect your AI » + « Model keys » on one page.** For the admin, each
  engine card carries the instance key of its provider (« key …xxxx, set by X on date », Replace /
  Remove / Test) and, in governed mode, the policy of allowed engines. One store: a key posed
  through an engine is the one `/api/admin/model-keys` lists (that API stays), an Anthropic key
  posed on the Claude card reaches the gateway too. New: `POST /api/connect-ai/engines/{id}/test`,
  `DELETE /api/connect-ai/engines/{id}/key`. A non-admin no longer receives the last 4
  characters of a connected engine's key.
- **Readable at 1280 px**: the motto shows from 1440 px only (it wrapped on three lines), the
  identity badge is compact (name truncated, role kept).
- **Keyboard and accessibility**: `g` then `c` / `b` / `o` / `s` changes plane, `1`–`9` picks a
  sub-tab (never while typing; listed in the ⌨ tooltip); planes and sub-tabs are ARIA
  tablists (arrow keys, Home / End), focus is visible, the selected one is marked by weight and
  an underline, not by colour alone.
- The public demo's guided tour points to the new places (Build › Sessions, Control › Board,
  Control › CortHeXis, Operate › Costs, Build › Crew). Nina's knowledge base, the docs and the
  messages that said « Profile → … » now say « Setup › … ».

- **Captains on the public demo** (feature `demo_captains`, requires `demo_banner` + `multi_project`, public demo
  only): the visitor (a viewer) reads Control › Helm (project cards, hierarchy, computed progress, reframe
  suggestions), the board, the project selector over 3 fictional projects, a « Shared with me » session, Setup ›
  Engines and Setup › Organization — fictional people only (`@example.com`), no key, no « connected by », no base
  URL. **Nothing is written by a visitor**: every non-read request under `/api` from someone below instance admin
  answers 403 « read-only demo » before any route (Nina's chat excepted, capped per day); read-only actions are
  greyed « read-only demo ». Idempotent seed `backend/demo_captains.py seed <captains.json>` (refused unless
  `SOKKAN_DEMO_CAPTAINS=1` or `--force`; people must be `@example.com`, never admin; the agents of `demo_crew`
  are kept). `docs/enterprise/UI-FEATURES.md` § 4.
- **UI pass** (captures 1280 / 1440 / 390, scored critique, journey « how is an agent approved »: PARTIAL →
  SUCCESS): at 390 px the planes get a full-width row of their own (they were hidden behind a sideways scroll);
  the board's four columns fit 1280 px; empty columns say what goes there; Setup › Engines tells the admin where
  to start when no engine is allowed; a Crew card waiting for approval says so at its head and its popout names
  **who approves** (owner or instance admin, an instance admin, or a second person — per `SOKKAN_AGENTS_APPROVAL`);
  read-only buttons are really greyed (the dimming class was never generated); no internal pricing warning on the
  demo; the demo tour gains « ⑥ who approves what » and « ⑦ several teams, several projects » and folds on a
  phone; a deep link to a place you do not have says so; contrast of small blue and dimmed text ≥ 4.5:1.
- Only an instance admin's last plane is saved (`PUT /api/me/nav` is no longer called for other people).

## 3.2.1 — 2026-10-08 — "Captains"
- **Fix: the API no longer crashes at startup when a numeric setting arrives as an empty string.** `docker compose`
  passes `${VAR:-}` as `""`; `SOKKAN_ASSISTANT_DAILY_LIMIT` (declared in 3.1.2) made `assistant.py` fail on
  `int("")`, which put the public demo's API in a restart loop for about 12 hours. Every numeric setting now treats
  an empty value as its default (`features.env_num`), and a test imports the backend with every compose variable
  set to `""`. No behaviour change otherwise.

## 3.2.0 — 2026-10-08 — "Captains"
Several people and several projects on one instance — the base of SOKKAN Enterprise, in
the same open-source app: every enterprise capability is a feature of the registry, off in
the community edition. Shipped inside 3.2.0 as **beta / experimental** features (off in
community, see FEATURES.md): **3.3 "Helm"** (hierarchical boards, morning brief) and **3.4
"Classification and Teams"** (clearances, Nina in Microsoft Teams — experimental, off in
both editions).

### Upgrading from 3.1 — read before you update
**Back up first**: `scripts/backup.sh` (new in 3.2: Postgres dump, consistent SQLite copies,
`vault.key` kept apart and encrypted with a passphrase; `docs/enterprise/OPERATIONS.md` § 5).
Several stores are migrated in place at the first start and a 3.1 binary does not read them
back: rolling back = `scripts/restore.sh` of that backup (or the `.bak` files below).
- **Memory store (Postgres)**: migrations `0011_note_project` (notes get a `project`, all
  existing notes → `default`), `0012_project_names` (note names unique per (project, name);
  links, versions and recall log carry the project) and `0013_classification` (`notes.level`,
  default `project`; `note_access` audit table). Applied automatically at start; the index is
  rebuilt per project.
- **Vault v1 → per project**: `vault.json` becomes format 2 (`{"projects": {slug: {...}},
  "instance": {}}`) at its first read; every existing secret goes to the `default` project;
  the 3.1 file is kept once as `vault.json.v1.bak` (what a rollback to 3.1 puts back).
  `shared` never holds secrets.
- **Agents**: `agents.db` is rebuilt once with names unique per project (same ids, runs kept,
  `agents.db.pre-lot4.bak` taken first). Board `sessions` / `cards` get `project`, Helm
  (`parent_id`, `kind`, `intent`, …) and `level` columns (additive).
- **Secrets by name**: `SOKKAN_SESSION_SECRETS` defaults to `named` — a human session gets
  only the vault secrets picked when it is opened. Set `all` to keep the 3.1 behaviour.
- **Cockpit cookies last 8 h** (`SOKKAN_SESSION_TTL_S`, was 24 h, counted from issue): every
  3.1 cookie older than 8 h is refused after the update — people log in again.
- **No agent run without explicit model credentials**: an instance that relies on the CLI
  login only (no key in the cockpit's model settings, no provisioned inference, no key in
  the API's env) must set `SOKKAN_AGENTS_USE_CLI_LOGIN=1`, or its agents stay held.
- **Enterprise edition defaults** (`SOKKAN_EDITION=enterprise`): `four_eyes` approval
  (the approver is neither the proposer nor the owner), projects, vault/budgets per project,
  GitLab, revocation, sandbox, shared review, Model keys, Connect your AI, Helm and
  classification ON; Missions link OFF. Community defaults are unchanged (3.1 behaviour);
  every feature is listed with its switch in `docs/enterprise/FEATURES.md`.
- **Sandbox hooks-only = no Bash outside `default`**: with the `sandbox` feature on (enterprise
  default) and neither bubblewrap usable nor the Kubernetes runner, Bash is refused in every
  project but `default`, without asking anyone (an unconfined Bash could read the other
  projects); sessions and agents show « Bash is disabled in this project: sandbox is
  hooks-only… ». Install bubblewrap or enable the runner before moving work to a project
  (`docs/enterprise/OPERATIONS.md` § 4.1).
- **docker compose**: the SSO / OIDC variables (`SOKKAN_AUTH_MODE`, `SOKKAN_OIDC_*`), the ops
  group and the Prometheus / Grafana / CortHeXis URLs are now passed to the api container —
  until 3.2 a value in `.env` did not reach it.
- **Known limit — Kubernetes**: the Helm chart runs the api as a **single replica** (SQLite
  stores and live sessions are in the api's memory, strategy Recreate); `api.replicas > 1`
  is refused unless `allowMultipleReplicas=true`, which is NOT supported yet. Availability =
  a fast restart (sessions reattach to their pods), not several api pods.

### Projects, identity and the feature registry (lots 1–3)

- **Feature registry** (`backend/features.py`, the base of SOKKAN Enterprise — the same app,
  not a fork). Every feature is declared once: switch `SOKKAN_FEATURE_<ID>=1|0`, defaults per
  edition (`SOKKAN_EDITION` = community, the default, or enterprise), `requires`,
  `conflicts`, status, doc; the roadmap is declared as `planned` features. The variables used
  so far keep their meaning (read after the canonical name): nothing to change on an
  existing installation. A feature asked for whose dependency is off (or that conflicts, or
  is only planned) stays OFF — the API starts, logs `[features] <id> is OFF: <reason>`, and
  Profile → Features (admins) shows the state of every feature and why. `GET /api/features`
  adds `registry`; `docs/enterprise/FEATURES.md` is generated by `scripts/gen-features-doc.py`
  (a test fails when it is stale). Enterprise defaults: four-eyes approval, projects on,
  Missions link off. Creating a project now needs `multi_project` (enterprise default; set
  `SOKKAN_FEATURE_MULTI_PROJECT=1` in community). Compose no longer pins
  `SOKKAN_AGENTS_APPROVAL=owner` (empty = the edition's default, `owner` in community).
- **Projects, the groundwork** (lot 1 of `docs/MULTIUSER.md`, the 3.2 spec: SSO teams,
  projects as perimeters, rights from the forge, pushes with the person's own token).
  New `projects.db` (projects, teams, grants, repositories, resources, forge links, access
  cache). At the first start, everything the instance holds becomes the project
  `default`, whose rights are the instance roles: nothing moves, nobody gains or loses a
  right, and nothing changes on screen while it is the only project. Sessions, cards and
  agents get a `project` column (`default` for every existing row); `POST /api/spawn`
  takes an optional `project` (developer access required; terminal sessions stay in
  `default` for now).
- **Memory recall scoped by project** — the memory store gets `notes.project` (migration
  `0011`, existing notes → `default`) and every path that brings a note into a session
  keeps to the session's project: the pre-seed at spawn (agent runs included), the
  per-turn and sub-agent recall (chat and terminal sessions), `memory_search`,
  `memory_get` and `memory_links` (a note of another project answers "not found"), and a
  note quoted by name. The store filters at every stage of the search (dense, lexical,
  final row), the recall filters again, and anything unknown gives no recall rather than
  all of it. A search without a project scope (CortHeXis on its own, plain `.mcp.json`) is
  unchanged.
- **Tests never touch a real instance** — the test suite now runs on a throw-away
  `SOKKAN_DATA_DIR`: some modules used to resolve `~/.local/share/sokkan` before a test
  module set its own directory.
- **No agent run without model credentials configured for the instance** — the
  scheduler now runs only with credentials explicitly set for this instance: the cockpit's
  model settings, the provisioned inference, or a key in the API's environment. A Claude CLI
  login found on the host no longer counts (twice on 07.10 a test instance started on a
  copied data directory ran a queued run with it); an instance that really runs on a CLI
  login says so with `SOKKAN_AGENTS_USE_CLI_LOGIN=1`. Without credentials: runs queued
  before the boot are marked skipped, nothing is caught up, due schedules move on with one
  skipped run each, alerts and "Run now" start nothing (refused with the reason), and the
  Crew tab shows "Scheduler stopped". When credentials arrive, the schedule resumes from the
  next occurrence.
- **Secrets by name is the default** — `SOKKAN_SESSION_SECRETS` now defaults to `named`:
  a session receives only the vault secrets picked when it is opened (or listed by its
  playbook). **Upgrading:** if you did not set the variable, sessions you open now get no
  secret unless you pick them, and sessions opened before the upgrade get none after a
  restart; set `SOKKAN_SESSION_SECRETS=all` to keep the 3.1 behaviour (a start-up message
  says so when the vault is not empty). An unknown value means `named`.
- **Several projects on one instance** (lot 3) — a project is a perimeter: its sessions,
  board, agents, memory and quarantine. The cockpit gets a project selector (hidden while
  there is one project) and every call is answered for the selected project with your role
  in it; an object of another project does not exist for you (404), through the API, the
  session's MCP servers (memory, board, agents) or the WebSocket. Teams come from the SSO
  `groups` claim at each login; instance admins manage projects, grants (a person or a team)
  and the ops team in Profile → Projects & teams — and see no project content until they
  add themselves, which the journal records. A `shared` project, read by everyone, is
  recalled with every project. Note names are unique per project (memory migration `0012`),
  each project has its own memory directory, indexer and agent quarantine, and a session of
  another project works in its own workspace. Operate and Infra are for the ops team (an SSO
  group) and the instance admins. Until per-project vaults (next step), sessions of other
  projects receive no secret; the CortHeXis review runs for the default project only.
- **Managed instances always give sessions secrets by name** — on SOKKAN Cloud
  (`SOKKAN_TIER` set) `SOKKAN_SESSION_SECRETS=all` is ignored; elsewhere the cockpit shows a
  warning banner while an instance runs in `all`.
- **Failed agent runs open an Operate incident by default** when Operate is active
  (`SOKKAN_AGENTS_INCIDENTS` unset); `0` turns it off.
- **Cockpit sessions last 8 hours** (was 24) — `SOKKAN_SESSION_TTL_S`, 5 min to 24 h. The
  limit counts from when the cookie was issued, so a 24 h cookie from 3.1 does not outlive
  it after the upgrade: expect to log in again.

### Vault and budgets per project (lot 4)
- **Vault per project** (feature `project_vault_budgets`, beta, enterprise default; requires
  `multi_project` + `named_secrets`). `vault.json` becomes `{"format": 2, "projects": {slug:
  {NAME: token}}, "instance": {}}`, migrated in place at the first read: every existing secret
  goes to `default` (the 3.1 file is kept once as `vault.json.v1.bak`). Names are unique per
  (project, name). A session, an agent run, the MCP servers and the redaction of a run's
  transcript read the vault of their project only; `shared` never holds secrets. Profile →
  Secrets manages the selected project's vault (its admins / maintainers); `/api/vault*` is
  now project-scoped. Feature off: only `default` has a vault (lot 3 behaviour).
- **Project budgets**: a daily and / or monthly ceiling per project, USD or CHF
  (`SOKKAN_FX_USD_PER_CHF`), set by a project admin (`GET|PUT /api/budgets`, Costs → Project
  budget). Warning at 80 %, hard stop at 100 %: the project's sessions refuse new turns and
  its agent runs end `budget` before starting. Costs shows the selected project's totals,
  daily series and model split (transcripts of the project's workspace and sessions).
- **Agent names unique per project**: the `agents` table is rebuilt once (same ids, runs
  kept, one transaction, `agents.db.pre-lot4.bak` taken first) with `UNIQUE(project, name)`;
  names are resolved in the session's / card's project (MCP, assignee `agent:<name>`).
  Feature off: the per-instance uniqueness check of 3.1 stays.
- **Before each run, the owner must still be dev or more in the agent's project**; otherwise
  the run is skipped and the agent paused (journaled, owner notified).
- **CortHeXis review per project** (store 3.0): each project's corpus is reviewed on its own,
  proposals and curation belong to their project (another project's proposal = 404).
- **Journal per project**: events carry their project; an admin / maintainer of a project
  sees that project's journal, instance admins still see everything.
- **`scripts/backup.sh` / `scripts/restore.sh`**: Postgres dump + consistent SQLite copies
  and data tarball, `vault.key` kept apart (encrypted with a passphrase, or left out and
  flagged), MANIFEST with sha256, retention (`SOKKAN_BACKUP_KEEP`), restore verified before
  anything is touched; end-to-end test `tests/test_backup_restore.py`
  (docs/enterprise/OPERATIONS.md § 5).

### GitLab (lot 5)
- **GitLab projects** (feature `gitlab`, beta; requires `multi_project` + `sso`; on by
  default in the enterprise edition). A project whose access source is "GitLab roles" gives
  each person the **lowest** GitLab level they have over the project's repositories
  (Guest/Reporter → viewer, Developer → dev, Maintainer → maintainer, Owner → admin; group
  inheritance included), read with **their own** account, cached 10 min (refusals 2 min);
  GitLab unreachable → the last decision until it expires, then no access.
- **Profile → Linked accounts**: link GitLab (OAuth 2 + PKCE, scopes `read_user read_api
  read_repository write_repository`, no `api`), see the account, scopes, token expiry and the
  role in each project, "Refresh my access", unlink. Tokens are Fernet-encrypted with their
  own key (`$SOKKAN_DATA_DIR/forge.key`), refreshed server-side, never shown nor logged.
  Unlink (or a 401 from GitLab) withdraws the access at once; unlink also closes the
  person's live sessions of GitLab projects.
- **Push in the person's name**: sessions of a GitLab project get a git credential helper
  (no token in their environment, no credential stored on disk, the user's `store`/`cache`
  helpers bypassed) that asks the API on the loopback for the person's current token;
  `git push -o merge_request.create` opens the merge request. No linked account → the push
  fails at once and says why.
- **Push from a session container / pod or from Bash inside bubblewrap**: the helper no
  longer depends on the api's loopback, which a docker / kubernetes session or a sandbox
  without network cannot reach. In a session container it asks through the runner's
  authenticated relay (`sokkan-git-credential` in the session image; the relay token fixes
  which session asks, the HMAC ticket must name that same session and its live person — a
  ticket replayed on another session's channel or after the session ended gets nothing).
  Under bubblewrap it uses a per-session Unix socket of the api bound into the sandbox
  (`/run/sokkan/forge.sock`, 0600), which also tunnels git — and only git to the project's
  forge hosts — through a forwarder on the sandbox's private loopback: no general network is
  opened. The token still never touches the disk or the environment. Pods reach GitLab
  through the egress gateway: add its host to `runner.egress.allow`.
- Admin → Projects & teams: access source "GitLab roles" and the project's repositories
  (`/api/admin/projects/<slug>/repos`). `backend/forge/`: one `Provider` interface (GitLab
  implemented; GitHub and Gitea/Forgejo skeletons). Variables `SOKKAN_GITLAB_URL`,
  `_CLIENT_ID`, `_CLIENT_SECRET`, `_REDIRECT_URI`, `_CA_BUNDLE` (operator guide:
  `docs/enterprise/OPERATIONS.md` § 2b).

### Revocation and project sandbox (lots 6 and 8)
- **Revocation** (lot 6, feature `revocation`, requires `sso_teams`; on in the enterprise
  edition). One effect for every path: account disabled, cockpit cookies issued before now
  refused, open chat panes / terminals closed, live SDK sessions interrupted and closed,
  owned agents paused (runs cancelled, notification), forge tokens erased, access cache
  purged, SSO team memberships removed, audit `user.revoke`. Paths: a **SCIM 2.0** endpoint
  `/api/scim/v2` (Users create / deactivate / delete, Groups membership = SOKKAN teams;
  bearer `SOKKAN_SCIM_TOKEN`; Entra ID and Authentik shapes), the admin **Revoke now** button
  (Profile → Members; `POST /api/admin/users/{email}/revoke`, `…/reinstate`), each SSO login
  (teams recomputed, lost access withdrawn), and the scheduler (an agent never outlives its
  owner's access: paused + notification). New variables: `SOKKAN_FEATURE_REVOCATION`,
  `SOKKAN_SCIM_TOKEN`, `SOKKAN_SCIM_GROUP_KEY`.
- **Project sandbox** (lot 8, feature `sandbox`, requires `multi_project`; on in the
  enterprise edition). A session or an agent run of a project other than `default` reaches
  only its project's space: file tools checked by a PreToolUse hook (paths normalised,
  symlinks resolved, refusals in the audit log as `sandbox.deny`), no extra directory for the
  CLI, and Bash either run inside **bubblewrap** (workspace read-write, nothing of another
  project mounted, no network unless `SOKKAN_SANDBOX_NETWORK=1`, empty environment) or, when
  bubblewrap is absent or unusable, refused. Mode detected at start, served by
  `GET /api/features` (`sandbox`: off | hooks-only | bwrap | pod — `pod` = sessions in their
  own pod through the session runner, the reference isolation on Kubernetes / OpenShift
  restricted SCC; bubblewrap is opportunistic). `default` is unchanged; the raw
  terminal stays `default`-only. The API image installs `bubblewrap` (Docker's default
  seccomp profile keeps it in hooks-only mode — docs/enterprise/OPERATIONS.md § 4.1).

### Sharing for review, Model keys (lot 7), Connect your AI
- **Shared review** (`shared_review`, needs `preview` + `multi_project`): ⇪ share a session (pane
  header) or a captured preview (Preview) with a person or a team of its project, read or
  read-write, optionally for a limited time. The recipient finds it at the top of their rail
  (« Shared with me », shared by …); a write share can carry the session's delegated HITL
  approval (« ask X to validate » → ✋ allow / deny from the rail). Never wider than the project
  role (a viewer never gets write; write capped by the current role; a non-member sees nothing),
  revocable, every step in the journal. `docs/enterprise/UI-FEATURES.md`.
- **Model keys** (`byok_admin`, lot 7, needs `multi_project`): Profile → Model keys for the
  instance admin — set, replace, delete provider keys, stored encrypted with the vault key and
  never shown again (…last 4, date, who); optional validity test (status only, the key is never
  logged); the Anthropic key reaches the sessions through a reference in llm.json, and is pushed
  to the SOKKAN gateway's BYOK endpoint when `SOKKAN_GATEWAY_URL` / `_ADMIN_TOKEN` / `_CLIENT` are
  set. Scope `project:<slug>` reserved (BYOK per project: planned).
- **Connect your AI** (`connect_ai`): Profile → Model becomes engine cards (SOKKAN Router, Claude
  key or login, OpenAI / Codex, Gemini, OpenRouter, Ollama / local, Magnitude). Personal mode
  (community): any engine, SOKKAN Router preselected with a configurable welcome-credit link
  (`SOKKAN_ROUTER_WELCOME_URL`, no amount in the code). Governed mode (enterprise, or
  `SOKKAN_CONNECT_AI_MODE`): the admin allows engines, zones and SOKKAN tiers; people only see
  those; a project maintainer may pick the project's engine. A connected engine is selectable as
  a Crew card's model (`engine:<id>`). The login card says: check your provider's terms.

### Session runners and Kubernetes chart (experimental)
- **Session runners** (feature `kubernetes_runner`, experimental, off by default;
  `backend/runner/`, docs/enterprise/KUBERNETES.md). `SOKKAN_RUNNER=local` (default) keeps
  the CLI inside the api exactly as before. `docker` runs each session / agent run in its own
  container on the compose host (opt-in override `docker/runner/compose.docker-runner.yml`);
  `kubernetes` in its own Pod through the K8s API. A session container is non-root
  (arbitrary uid OK), read-only, capabilities dropped, CPU/memory limited, mounts only its
  workspace and transcript directory, and reaches nothing but the api's MCP relay and an
  allowlist egress gateway. The cockpit still drives it with the Agent SDK (permissions,
  hooks, budgets unchanged); a supervisor in the container keeps the CLI alive across an api
  restart and the restarted api reattaches to it. New image `docker/session.Dockerfile`.
- **Helm chart** `deploy/helm/sokkan` (vanilla Kubernetes; overlays `values-sks.yaml` for
  Exoscale SKS and `values-openshift.yaml` for the restricted SCC): api (single replica —
  refused otherwise, see the doc's limits), web, Ingress (/api and /term to the api),
  external Postgres via Secret (test-only pgvector pod `devPostgres`), optional embeddings
  and vLLM on GPU nodes, Secret / ExternalSecret, minimal namespaced RBAC, NetworkPolicies,
  PodDisruptionBudget. `tests/test_helm_chart.py` renders every overlay and checks: no root,
  no privileged, limits everywhere, dedicated ServiceAccounts.

### Board, driven from a session
- **The board, driven from a session** — the embedded `sokkan-board` MCP server
  gains `get_card` (fields, comments, history, links), `search_cards` (text in title,
  description and comments; tag, column, assignee), `update_card` (title,
  description, tag, priority, due date, assignee), `close_card` / `reopen_card`
  (closed means finished, not deleted: the card goes to Done with `closed_at` /
  `closed_by` and keeps everything), `archive_card`, `comment_card` and `link_card`
  (to a session, an agent, an agent run or an incident — only if it exists).
  Reads are auto-approved; writes go through the session's permission gate, like
  `create_card` / `move_card`; in a Crew run the server is there only if the agent
  was granted it, and a write runs unattended only if the agent's `auto_approve`
  lists it.
- **Signed card history** — every card action records who (the person driving the
  session, or `agent:<name>` in a run), when, and from which session and channel
  (`web`, `mcp`, `agent-run #N`). The identity comes from the environment the API
  gives the MCP server; no tool takes an author. A session driven by a viewer cannot
  write the board.
- **CardModal** — comments (and a box to add one), links to sessions / agents / runs /
  incidents, an assignee, close / reopen, and a History that shows who, when and
  from which session (click to open it). A card filed by an agent run is linked to
  the run and the agent. New routes: `POST /api/board/card/{id}/comment|close|reopen`;
  `PATCH` accepts `assignee` (an IAM user email or `agent:<name>`).

### 3.3 "Helm" — beta, shipped in 3.2.0 (enterprise default on, community off)
- **Hierarchical cards** (feature `helm`, beta; requires `multi_project` + `assistant`; on in
  enterprise, off in community). A card can sit under another (manager's project card →
  engineer's cards → sub-tasks; same project, no cycle, 6 levels): safe migration (new
  columns, every existing card stays a top-level task). Opening a parent shows its own
  kanban (the Board's component) with a breadcrumb. Merge requests become a link kind (`mr`).
- **Context flows down**: the intent, constraints, decisions and links of every card above
  are injected into the seed of a session spawned from a card below (before the memory
  pre-seed), and kept as a project memory note `helm-card-<id>` marked `card:<id>`.
- **Progress flows up**: a parent's state (done / in progress / waiting for approval /
  blocked / to do) is computed from its children and their live signals (sessions working,
  agent runs, merge requests, Operate incidents), recomputed on every board event, by a
  periodic job and on read, journaled on the card — never declared by hand.
- **Helm tab** for project managers (maintainer/admin, and instance admins in their own
  projects): every project card in a deck with Crew's grammar (state colour + label, the card
  breathes while work goes on), popout Kanban · Activity · Suggestions · Costs, filters by
  project / team / person.
- **Nina steers**: « create a project » — she interviews (goal, scope, constraints, deadline,
  team) and proposes the project card and its breakdown in an editable `sokkan-project`
  block that the person validates. **Reframe suggestions** (every 15 min + on demand): drift
  from the parent's goal (memory embedding), contradiction with a recorded decision, scope
  growth, cards outside the project, cards without owner, slowing down / racing ahead, linked
  incident — each one Approve (a `reframe` card for the manager) or Ignore, never applied alone.
- **Morning brief**: Crew template `morning-brief` (weekdays 07:30) built on a read-only board
  MCP tool `morning_brief`; delivered as a quarantined memory note + a notification; agenda
  from an ICS address kept in the vault by name (`helm_calendar`, Graph/Teams later).
- Board MCP: `create_card` / `update_card` take `parent_id`; new reads `get_card_tree`,
  `morning_brief`. New variables (compose `# helm` block): `SOKKAN_FEATURE_HELM`,
  `SOKKAN_HELM_TICK_S`, `SOKKAN_HELM_DRIFT_MIN`, `SOKKAN_HELM_SNOOZE_DAYS`,
  `SOKKAN_HELM_CALENDAR_ICS`, `SOKKAN_HELM_CALENDAR_ALLOW_PRIVATE`. Spec: `docs/HELM.md`.

### 3.4 "Classification and Teams" — beta / experimental, shipped in 3.2.0
- **Classification and clearances** (feature `classification`, requires `multi_project` +
  `sso_teams`; enterprise default on). Notes (frontmatter `classification:`), cards, agent
  deliverables carry a level `public < team < project < confidential < restricted`
  (default `project`; customer labels with `SOKKAN_CLASSIFICATION_LABELS`). A person's
  clearance per project = the highest of their project role's level (`SOKKAN_CLEARANCE_ROLES`)
  and the levels mapped to their SSO groups (Profile → Classification). The memory engine
  filters every search stage by `(project, clearance)` (migration `0013`: `notes.level`,
  every existing note at `project`); recall, `memory_search` / `memory_get`, the CortHeXis
  tab, the board (+ MCP), quarantine, sessions, runs and Nina answer only within the
  clearance of the person they act for — Nina always as the person who asks. Derived content
  (a note written by a session, an agent deliverable, a card from a session, a Nina answer)
  inherits the highest level of its sources; an index upsert never lowers a level; lowering
  takes a cleared maintainer/admin with a reason, journaled. **Audited recall**: every note
  handed out is logged (`note_access`: who, which note, via spawn / prompt / mcp / cockpit /
  nina / teams / brief), `GET /api/classification/audit` for project admins, CSV export.
  Off: nothing above `project` is reachable by anyone. See `docs/enterprise/SECURITY.md` § 8.
- **Microsoft Teams** (feature `teams`, requires `assistant` + `classification` + `sso`; off
  by default, experimental). `@Nina` in a channel, a group chat or 1:1: project status card,
  `card: …`, `note la décision : …` (a decision note in the project memory with author,
  date, link to the thread, level inherited from the channel), `run <agent>` → approval
  card, `approvals` → pending agents as cards. HITL approvals are Adaptive Cards with
  Approve / Refuse, signed (HMAC), expiring, single use, optionally bound to one approver,
  executed as the person who clicks (four-eyes refuses the requester). Single-tenant app:
  every request's Bot Framework JWT is verified (keys, issuer, audience, expiry, serviceUrl,
  msteams endorsement) and its tenant checked before anything is read; a Teams user acts only
  through the SOKKAN account linked at their Entra ID sign-in (`oid`), and Nina answers with
  their clearance capped by the channel's level. Outbound tokens cached Fernet-encrypted.
  Channel ↔ project mapping and the manifest in Profile → Teams. Calendar via Graph
  (`Calendars.Read`) behind the new `calendars` interface (for the brief). Built and tested
  against a Graph / Bot Framework simulator — no app registered yet. See
  `docs/enterprise/TEAMS.md`.

## 3.1.2 — 2026-10-07 — "Crew up"
Security patch of the agents (Crew), from an external review of 3.1. Upgrade notes:
alert-triggered agents that auto-approve write tools need an admin override to be
edited or re-approved, and their alert runs now ask a human for those calls.
- **An Operate alert is untrusted input.** Its payload now reaches the run inside an
  `<untrusted-data kind="alert">` block (per-prompt nonce) with the standing instruction
  never to follow what it says; the diagnosis session Operate opens gets the same frame.
  **Behaviour change:** an alert-triggered agent can no longer go live with a write tool in
  "Runs without asking" — `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Bash`, any MCP
  rule that is not a known read. Form activation, direct edit of a live agent and approval
  answer 400 with the rules to remove; an admin may override (`override_alert_writes`),
  which is stored on the agent and journaled (`agent.alert_write_override`). A run started
  by an alert holds those rules back — agents armed under 3.1.0/3.1.1 included — so the
  calls wait for a human (`agent.run.alert_writes_held`); manual and scheduled runs keep
  them.
- **Secrets masked in every form, and in transcripts.** `[secret:NAME]` now replaces
  the value base64-encoded (standard or url-safe, also inside a larger blob such as a
  Basic header), URL-encoded, hex, and any 12-character piece of a secret of 16+
  characters — in the deliverable and, new, in the live events of a run and the
  transcript shown in History. The CLI's own transcript file on disk is unchanged.
- **Budgets hold on non-Claude models.** The CLI prices an unknown model at a Claude
  tariff, so a run on SOKKAN Inference (`sokkan-ship`…) or a custom endpoint was costed,
  and stopped, at the wrong price. Such runs are now metered by SOKKAN: tokens × a price
  table (`SOKKAN_MODEL_PRICES`, the gateway's CHF tiers, `SOKKAN_FX_USD_PER_CHF`), and
  always capped in tokens (`SOKKAN_AGENTS_MAX_TOKENS_PER_RUN`, default 5,000,000 — never
  unlimited, the only limit when the price is unknown). Settings says which applies,
  History shows how each run's cost was obtained. Claude on Anthropic is unchanged.
- **Fix: every 3.1 agent variable reaches the container.** `SOKKAN_AGENTS_TICK_S` and
  `SOKKAN_MEMORY_QUARANTINE_DIR` were documented but missing from `docker-compose.yml`;
  declared, with the three variables above. A test now guards the list.
- **Nina's fallback model and daily limit reach the container.** `SOKKAN_ASSISTANT_LLM_API`, the `SOKKAN_ASSISTANT_LLM_FALLBACK_*` settings and `SOKKAN_ASSISTANT_DAILY_LIMIT` were read by the backend but never passed through `docker-compose.yml`, so setting them in `.env` had no effect.

## 3.1.1 — 2026-10-07 — "Crew up"
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
