# Operations runbook — SOKKAN Enterprise

Scope: a self-hosted SOKKAN instance run by the customer's ops team (single VM, Docker
Compose). Managed instances (SOKKAN Cloud) are operated by SOKKAN; the steps marked
*managed* say what differs. Commands are exact where the tool exists; **TBD** marks what is
not built or not yet validated — never improvise around a TBD, ask the maintainers.

> Rule for anyone scripting against an instance: a script or REPL that imports a SOKKAN
> backend module must set `SOKKAN_DATA_DIR` to a scratch directory. The default is the
> instance's real data.

## 0. Prerequisites

| Item | Requirement |
|---|---|
| Host | Linux x86_64 / arm64; Docker Engine 24+ with Compose v2 (2.12 or newer) |
| Resources | ≥ 4 GB RAM free, ~3 GB disk for a small team (README). **Sizing for N seats: TBD** (to be measured during the POC) |
| Network | a DNS name and TLS in front of the `web` port (`SOKKAN_PORT`, default 3009) — your reverse proxy, or the bundled `edge` Caddy profile; outbound HTTPS to the model endpoint and, optionally, `sokkan.ch` (update check) |
| Identity | an OIDC client in your IdP (Entra ID, Authentik, Keycloak…) that sends `email` and a `groups` claim |
| Models | explicit credentials: Anthropic API key, the SOKKAN Inference tenant token (`ANTHROPIC_BASE_URL` + token), a custom gateway in the cockpit's model settings, or a Magnitude node |
| Check | `./scripts/doctor.sh` — read-only, safe anytime |

## 1. Install

```bash
curl -fsSL https://sokkan.ch/install.sh | sh        # or: git clone … && cp .env.example .env
cd sokkan && $EDITOR .env
docker compose up -d --build
./scripts/doctor.sh
curl -fsS http://localhost:3009/api/health
```

Minimum `.env` for an enterprise instance (in addition to the model credentials):

```dotenv
SOKKAN_EDITION=enterprise
SOKKAN_PUBLIC_URL=https://sokkan.example.org
SOKKAN_WORKSPACE=/srv/sokkan/workspace
SOKKAN_OWNER_EMAIL=first.admin@example.org
SOKKAN_UPDATE_CHECK=1                 # 0 to forbid the daily GET to sokkan.ch
```

`SOKKAN_EDITION` changes defaults only; every feature can still be set explicitly
([FEATURES.md](FEATURES.md)).

## 2. Single sign-on

```dotenv
SOKKAN_AUTH_MODE=oidc
SOKKAN_OIDC_ISSUER=https://idp.example.org/…
SOKKAN_OIDC_CLIENT_ID=…
SOKKAN_OIDC_CLIENT_SECRET=…           # keep .env at 0600, never in git
SOKKAN_OIDC_SCOPES=openid email profile   # add the scope your IdP needs to release groups
SOKKAN_OIDC_GROUPS_CLAIM=groups       # claim that carries the groups
SOKKAN_DEFAULT_ROLE=none              # unknown emails get 403 instead of viewer
SOKKAN_OPS_GROUP=sokkan-ops           # bootstrap value; then Admin → Projects & teams
```

* Redirect URI to register in the IdP: `https://<SOKKAN_PUBLIC_URL host>/api/auth/callback`.
* At each login the `groups` claim **replaces** the person's `sso:*` memberships.
* **Entra ID**: groups arrive as object ids unless "cloud-only group names" is set; above
  200 groups the claim overflows — reading `/me/memberOf` through Graph is in the spec,
  **check the release notes of your version before relying on it (TBD)**.
* Instance admins (`admin` / `owner`) administer the instance; they **do not** see a project's
  content until they grant themselves a role there (logged as `project.grant.self`).
* Cockpit session lifetime: 8 h (`SOKKAN_SESSION_TTL_S`, 5 min … 24 h). Without SCIM (§ 2.1)
  a person disabled in the IdP keeps the cockpit until the cookie expires — or until an admin
  presses **Revoke now**.

Verify: log in with a test account of each profile; `GET /api/me` shows the role in the
selected project; Admin → Projects & teams → "why" (`GET /api/admin/explain?email=…&project=…`)
explains each access.

### 2.1 Revocation and SCIM (feature `revocation`, lot 6)

```dotenv
SOKKAN_FEATURE_REVOCATION=1            # on by default in the enterprise edition; needs sso_teams
SOKKAN_SCIM_TOKEN=<openssl rand -hex 32>   # the bearer token the IdP sends; unset = SCIM closed
SOKKAN_SCIM_GROUP_KEY=displayName      # or externalId (Entra ID sending object ids in `groups`)
```

**What a revocation does** — the same effect for SCIM deactivate / delete and the admin
button (Profile → Members → **Revoke now**, or `POST /api/admin/users/<email>/revoke`):

```
account disabled ─┬─ every cockpit cookie issued before now refused (next request: 401/403)
                  ├─ open chat panes and terminals of the person closed (WebSocket 4401)
                  ├─ agents they own paused, their queued/running runs cancelled, notification
                  ├─ their live SDK sessions interrupted then closed
                  ├─ forge tokens erased (forge_links blanked, revoked_at) + access cache purged
                  ├─ SSO team memberships removed
                  └─ audit: user.revoke (counts), agent.pause.owner_access, scim.*
```

Reinstate: Profile → Members → *reinstate* (or SCIM `active=true`). Paused agents stay paused:
someone with access resumes or takes them over on purpose. The instance `owner` and yourself
cannot be revoked from the button (SCIM can disable anyone).

**SCIM endpoint**: `https://<public host>/api/scim/v2` — Users (create, get, filter
`userName eq`, PUT, PATCH, DELETE) and Groups (create, filter `displayName eq`, PUT, PATCH
add/remove members, DELETE), `ServiceProviderConfig`, `ResourceTypes`. No bulk, no sort, no
etag. A SCIM group **is** the SOKKAN team `sso:<displayName>`: a member removed from it loses
the projects that team granted at once (live sessions there closed, agents paused).

* **Entra ID** — Enterprise application → Provisioning → Automatic. Tenant URL
  `https://<host>/api/scim/v2`, Secret token = `SOKKAN_SCIM_TOKEN`, *Test connection*. Mappings:
  `userPrincipalName` (or `mail`) → `userName`, `mail` → `emails[type eq "work"].value`,
  `Switch([IsSoftDeleted]…)` → `active` (default mapping), groups → `displayName`. Entra sends
  `active` as the string `"False"`: accepted. If the OIDC `groups` claim carries object ids,
  set `SOKKAN_SCIM_GROUP_KEY=externalId` and map `objectId` → `externalId`.
* **Authentik** — Applications → Providers → *SCIM provider*: URL `https://<host>/api/scim/v2`,
  Token = `SOKKAN_SCIM_TOKEN`; bind it to the SOKKAN application as backchannel provider; group
  filter = the groups used for project grants. Authentik PUTs whole users and groups: handled.

Check: `curl -s -H "Authorization: Bearer $SOKKAN_SCIM_TOKEN" https://<host>/api/scim/v2/Users`
→ a `ListResponse`; deactivate a test account in the IdP → within the provisioning cycle its
cockpit answers 403 and `GET /api/audit?q=user.revoke` shows the entry. **Delay**: SOKKAN acts
within a second of the call; the IdP decides when it calls (Entra ID: a cycle every ~40 min,
or *Provision on demand*; Authentik: on save). For an immediate cut, press Revoke now.

At each SSO login the teams are recomputed from the `groups` claim and access that was lost is
withdrawn (sessions in projects no longer reachable closed, agents the person may no longer run
paused). Before each run, the scheduler checks the owner still has `dev` in the agent's project.

## 3. Model credentials and the scheduler guard

Agents only run with **explicit** credentials (cockpit model settings, provisioned inference,
or `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` in the API's environment). An instance that
deliberately runs on a CLI login must say so: `SOKKAN_AGENTS_USE_CLI_LOGIN=1`. Without
credentials the Crew tab shows "Scheduler stopped" and nothing that fell due is replayed later.

## 4. Enable features one by one

Source of truth: [`backend/features.py`](../../backend/features.py) and the generated
[FEATURES.md](FEATURES.md) (exact variable names, defaults per edition, dependencies).
Canonical switch: `SOKKAN_FEATURE_<ID>=1|0`; legacy names still accepted. After each step:
`docker compose up -d api`, then **Profile → Features** (or `GET /api/features`) must show the
feature on with no red "problem", then run the check of the step. Rolling a step back = set the
switch to `0` and restart `api`.

| Step | Feature ids | Settings | Check | Status |
|---|---|---|---|---|
| 1. Identity | `sso` | § 2 | a test login per profile; unknown email → 403 | ● |
| 2. Secrets by name | `named_secrets` | `SOKKAN_SESSION_SECRETS=named` (3.2 default) | a session gets only the secrets picked at opening | ◐ default in 3.2 |
| 3. Agents | `agents`, `four_eyes`, `memory_quarantine` | `SOKKAN_AGENTS_APPROVAL=four_eyes` | an agent proposed by A is approved by B, refused for A | ● |
| 4. Operate | `operate`, `agent_incidents`, `ops_team` | alert webhook, notification channel, `SOKKAN_OPS_GROUP` | a test alert opens an incident; a failed agent run opens one incident | ● / ◐ |
| 5. Projects | `multi_project`, `sso_teams` | `SOKKAN_FEATURE_MULTI_PROJECT=1`; create projects and grants in Admin → Projects & teams (`POST /api/admin/projects`, `…/grants`) | two people, two projects: neither sees the other's sessions, cards, agents or notes; `GET /api/audit?q=memory.scope_violation` stays empty | ◐ |
| 6. Vault and budgets per project | `project_vault_budgets` | per registry | secrets of X never in a session of Y; budget stop per project | ○ lot 4 |
| 7. GitLab | `gitlab` | per registry | Reporter cannot push; Developer pushes a branch and opens an MR | ○ lot 5 |
| 8. Revocation | `revocation` | § 2.1 (`SOKKAN_SCIM_TOKEN`, IdP provisioning) | SCIM deactivate → 403 at once, sessions closed, agents paused | ◐ lot 6 |
| 9. BYOK screen, sandbox, shared review | `byok_admin`, `sandbox`, `shared_review` | per registry | per feature spec | ○ |
| 10. Helm, classification, Teams | `helm`, `classification`, `teams` | — | — | ○ 3.3 / 3.4 |

Planned features cannot be switched on: asking for one is reported, never honoured.

### 4.1 Project sandbox (feature `sandbox`, lot 8)

```dotenv
SOKKAN_FEATURE_SANDBOX=1               # on by default in the enterprise edition; needs multi_project
SOKKAN_SANDBOX_BWRAP=                  # path to bwrap; empty = the one on PATH
SOKKAN_SANDBOX_NETWORK=0               # 1 = a sandboxed shell may reach the network
SOKKAN_SANDBOX_RO_PATHS=               # extra read-only mounts for the shell (a:b), e.g. /opt/tools
SOKKAN_SANDBOX_READ_PATHS=             # extra paths file tools may read (a:b)
```

Every project except `default` is confined; `default` keeps the 3.1 behaviour. **The reference
isolation of SOKKAN Enterprise is the pod**: on Kubernetes (SKS) / OpenShift restricted SCC —
no root, arbitrary uid, no privileged pod, no user namespaces, hence no bubblewrap — each
session / run executes in its own pod through the session runner (`SOKKAN_RUNNER=kubernetes`),
with only its project's volume mounted. bubblewrap is an opportunistic extra for a compose / k3s
host. Mode, detected at start (`[sandbox] mode …` in the API log, `GET /api/features` →
`sandbox`):

| Mode | When | File tools | Bash |
|---|---|---|---|
| `pod` | session runner on Kubernetes (`SOKKAN_RUNNER=kubernetes`) | hook as a second layer | runs in the session's pod (the boundary) |
| `bwrap` | bubblewrap installed **and** a probe run succeeds | own workspace (write), project + `shared` (read) | inside bubblewrap: workspace rw, project memory + `shared` ro, /usr ro, private /tmp, no network, empty env |
| `hooks-only` | no usable bubblewrap | same | **refused** outside `default` |
| `off` | feature off | lot 3 behaviour (own working directory, not a boundary) | allowed |

**Prerequisites for `bwrap`** (compose / k3s host only — never available under restricted SCC): package `bubblewrap` (in the API image since 3.2) and user
namespaces. On a host install (systemd) as root: works as is. In Docker the default seccomp
profile refuses the namespaces → the probe fails → `hooks-only` (safe). To get `bwrap` in a
container, run the API with a seccomp profile that allows `unshare`/`clone` of user, mount, pid,
net namespaces (or `security_opt: [seccomp=unconfined]` on a dedicated host — your security
officer's call). Ubuntu 24.04 hosts with `kernel.apparmor_restrict_unprivileged_userns=1` need
the API to run as root or an AppArmor profile for bwrap.

**Limits** (say them to the client): the hook confines the CLI's built-in tools; MCP servers are
scoped by project (lot 3) but run outside the sandbox; the raw terminal stays `default`-only;
in `bwrap` the shell reads /usr and a few /etc files of the host; a session of a project still
runs under the API's uid outside `pod` mode (no per-project uid: a kernel escape is out of scope); refusals are in
the audit log (`sandbox.deny`).

Check: in a project other than `default`, ask the session to `Read` and to `cat` a file of
another project → both refused (`GET /api/audit?q=sandbox.deny`); the same in `default` works.

## 5. Backup and restore

What holds state:

| Where | Content | Criticality |
|---|---|---|
| volume `sokkan-data` (`/data`) | SQLite: `board.db`, `agents.db`, `projects.db`, `iam.db`, `audit.db`, `usage.db`, `incidents.db`, `assistant.db`; `vault.key` + `vault.json` (secrets, Fernet); `session.key`; `llm.json`, `settings.json`, `notify.json`; per-project memory directories and workspaces; `memory-quarantine/` | **critical** — `vault.key` is the only key to `vault.json`: back it up, store it separately |
| volume `sokkan-pg` | CortHeXis (Postgres + pgvector: notes, links, versions, recall log) | critical |
| `SOKKAN_WORKSPACE` | the code sessions work on (also in your forge) | per your forge policy |
| `.env` | configuration and secrets (0600) | critical, store in your secrets manager |
| volume `corthexis-models` | embedding model files (re-downloadable) | low |

Procedure (no bundled backup script yet — **TBD `scripts/backup.sh`**; test the restore on a
staging copy before relying on it):

```bash
cd sokkan
docker compose exec -T db pg_dump -U sokkan -Fc sokkan > sokkan-pg-$(date +%F).dump
docker compose stop api            # consistent SQLite copy; short interruption
docker compose run --rm -T --no-deps --entrypoint tar api czf - -C /data . > sokkan-data-$(date +%F).tgz
docker compose start api
```

Restore (on a stopped stack, same release as the backup):

```bash
docker compose stop api web
docker compose run --rm -T --no-deps --entrypoint sh api -c 'cd /data && tar xzf -' < sokkan-data-YYYY-MM-DD.tgz
docker compose exec -T db pg_restore -U sokkan -d sokkan --clean --if-exists < sokkan-pg-YYYY-MM-DD.dump
docker compose up -d
```

Frequency, retention and off-site copy: **TBD with the customer's backup policy** (recommended
starting point: daily, 30 days, encrypted off-site; always one before an upgrade).

## 6. Upgrade and roll back

1. Read the `CHANGELOG.md` entry, *Upgrade notes* first ([RELEASING.md](../RELEASING.md)).
2. Back up (§ 5).
3. `curl -fsSL https://sokkan.ch/install.sh | sh` from the parent directory (keeps `.env` and
   volumes) — or the manual steps in [UPGRADE.md](../UPGRADE.md). *Managed*: Profile → update.
4. `./scripts/doctor.sh`, `GET /api/health`, Profile → Features (no red problem), one session,
   one agent "Run now" on a harmless agent.

Roll back: `./scripts/rollback.sh <hash>` (keeps `.env`, workspace, volumes). **Going back from
3.2 to 3.1 after the memory migrations `0011`/`0012` (notes keyed by project): not validated —
TBD; restore the Postgres dump taken before the upgrade.**

3.1 → 3.2 specifics: the instance becomes project `default` with the same rights;
`SOKKAN_SESSION_SECRETS` becomes `named` (set `all` to keep 3.1 behaviour); 3.1 cookies
expire at the new 8 h limit; an instance on a CLI login needs `SOKKAN_AGENTS_USE_CLI_LOGIN=1`.

## 7. Monitoring

| Signal | How | Expected |
|---|---|---|
| Liveness | `GET /api/health`; `docker compose ps` (healthchecks on `db`, embedding service) | 200, all healthy |
| Features | Profile → Features / `GET /api/features` | no `problem` |
| Scheduler | Crew banner / `GET /api/agents` → `scheduler.held` | `held: false` |
| Agent failures | Operate incidents (`agent_incidents`) | none open, or acknowledged |
| Isolation | `GET /api/audit?q=memory.scope_violation` | always empty |
| Spend | Costs tab; budgets warn at 80 %, stop at 100 % | within budget |
| Inference (operated) | gateway metrics and the avoided-cost report (operator side) | — |
| Host / containers | your Prometheus, or the Operate observability stack | — |

Alert thresholds and on-call routing: **TBD with the customer's ops team**.

## 8. Incident management

| Situation | Immediate action | Then |
|---|---|---|
| An agent misbehaves | Crew → the agent → **Pause** (or cancel the run) | History of the run; fix its settings → approval again |
| A secret may have leaked | rotate it at its source, then update the value in the vault (§ 9) | Journal for the sessions that held it; report |
| A note from the wrong project appeared in a session | stop the session; capture the session id and the note name | `memory.scope_violation` entries; report to security@ninabot.ch — this is a security incident |
| A person leaves | disable them in the IdP; remove their grants (Admin → Projects & teams) | cookie expires ≤ 8 h; "Revoke now" / SCIM: planned (lot 6) |
| Inference upstream down | nothing: failover to the next upstream / tier; Claude tier with the customer key if the profile has one | gateway report |
| No model credentials | Crew shows "Scheduler stopped"; fix credentials | nothing is replayed; next occurrences run |
| Bad upgrade | § 6 roll back | changelog, issue to the maintainers |

Severity levels, notification chain and post-mortem template: **TBD with the customer**.

## 9. Secret rotation

| Secret | Where | Rotation | Effect |
|---|---|---|---|
| Vault values (DB passwords, tokens) | Profile → Secrets (maintainer+ of the project in 3.2) | set the new value | sessions and runs started afterwards get it; redaction uses the current value |
| `vault.key` (vault encryption key) | `/data/vault.key` | **TBD — no re-encryption tool** | — |
| Cockpit session signing (`SOKKAN_SESSION_SECRET` or `/data/session.key`) | `.env` / data volume | change and restart `api` | everyone is logged out |
| OIDC client secret | IdP + `.env` | rotate in the IdP, update `.env`, `docker compose up -d api` | logins fail between the two steps |
| Model API key / tenant token | cockpit model settings or `.env` | replace, restart `api` if in `.env` | — |
| Claude BYOK key at the gateway | operated: set by the operator (encrypted) | replace; the old one is erased | — |
| Forge tokens (lot 5) | per person, encrypted | refreshed automatically; unlink to erase | — |

Rotation calendar: **TBD with the customer's policy**.

## 10. Go-live checklist

- [ ] `./scripts/doctor.sh` green; `/api/health` 200; TLS valid on `SOKKAN_PUBLIC_URL`
- [ ] `SOKKAN_AUTH_MODE=oidc`, `SOKKAN_DEFAULT_ROLE=none`, test login per profile
- [ ] `SOKKAN_LOCAL_TOKEN` not used for people; `.env` mode 0600, not in git
- [ ] Profile → Features reviewed; no `problem`; every enabled feature has passed its check (§ 4)
- [ ] `SOKKAN_SESSION_SECRETS=named`; `SOKKAN_AGENTS_APPROVAL=four_eyes` if required by policy
- [ ] Explicit model credentials; scheduler not held; budgets set
- [ ] Projects, grants and the ops group set; two-person isolation test passed
- [ ] Workspace mounts only what sessions should touch
- [ ] Backup taken **and restored once** on a staging copy; `vault.key` stored separately
- [ ] Monitoring signals of § 7 wired to the customer's alerting
- [ ] Incident contacts and rotation calendar agreed (TBD items closed)
- [ ] Data location of each inference tier agreed with the customer (DPA)
