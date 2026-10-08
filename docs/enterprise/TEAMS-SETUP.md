# Teams — day-one setup on a real tenant

Step by step for the person (or agent session) who connects a SOKKAN instance to a real
Microsoft 365 tenant the first time. Reference: [TEAMS.md](TEAMS.md) (what the feature does,
security model, variables). Scripts: `scripts/teams-register.sh`, `scripts/teams-manifest.py`.
Checks: `tests/teams_live/`.

**Path chosen: no Azure subscription.** A Microsoft 365 Business tenant (Basic, Standard…)
has Entra ID and Teams but **no Azure subscription**, so no « Azure Bot » resource and no
`az bot create`. The bot is registered in the **Teams Developer Portal**, which creates the
Bot Framework registration and its Entra app without Azure. The Azure path
(`teams-register.sh --mode azure`) is the variant for a tenant that has a subscription.

Rules for the whole day: **never paste a secret** (client secret, admin password, cookie)
into a chat, a ticket, a commit or a shell history line; secrets go straight from the portal
into the secrets store. Nothing here needs a Global Administrator once the app is consented:
use the admin account only for steps 2-5.

## 1. Prerequisites — check each one before starting

| # | What | How to check |
|---|---|---|
| 1 | The instance runs a build **with SOKKAN 3.4.0 Teams** (app package route, proactive approvals, `SOKKAN_TEAMS_APP_PASSWORD_FILE`) — the live tests target it | `curl -s https://<host>/api/version` ≥ 3.4.0 (or the 3.4 branch build); `curl -s -o /dev/null -w '%{http_code}' https://<host>/api/admin/teams/package` → 401/403 (route exists), not 404 |
| 2 | Edition enterprise (or `SOKKAN_FEATURE_ASSISTANT=1`, `SOKKAN_FEATURE_CLASSIFICATION=1`, `SOKKAN_FEATURE_SSO=1` — `teams` requires them) | Setup › My account › Features: `assistant`, `classification`, `sso` green |
| 3 | `https://<host>/api/teams/messages` is reachable **from the Internet without any access gate** (Cloudflare Access, VPN, basic auth…): Microsoft calls it, the Bot Framework JWT is its authentication | from outside: `curl -s -o /dev/null -w '%{http_code}' -X POST https://<host>/api/teams/messages -H 'content-type: application/json' -d '{}'` → **401** (SOKKAN refusing an unsigned call) — a 302 / 403 from a proxy means the gate must get a bypass for that one path |
| 4 | The cockpit's SSO is **Entra ID of the same tenant** (OIDC): the id_token's `oid` + `tid` link a Teams user to their SOKKAN account | `SOKKAN_OIDC_ISSUER=https://login.microsoftonline.com/<tenant id>/v2.0`, `SOKKAN_OIDC_SCOPES` includes `openid email profile` |
| 5 | A secrets store for the client secret (OpenBao / Vault, or the instance's 0600 env file) and a password manager entry for the tenant admin account | the operator knows where `SOKKAN_TEAMS_APP_PASSWORD_FILE` will be rendered from |
| 6 | **Two licensed Teams users** besides the admin if possible (a `dev` and a `maintainer` of a test project): four-eyes needs a second person | Microsoft 365 admin center → Users → licenses |
| 7 | A test project in SOKKAN with an agent (to propose / run) | Setup › Organization › Projects |

## 2. Two Entra apps (recommended) or one

| | Login app (OIDC of the cockpit) | Bot app (Teams) |
|---|---|---|
| Created in | Entra admin center → App registrations (free) | Teams Developer Portal → Bot management (creates it) |
| Used by | `SOKKAN_OIDC_CLIENT_ID` / `_SECRET` | `SOKKAN_TEAMS_APP_ID` / `_APP_PASSWORD(_FILE)` |
| Permissions | delegated sign-in only (`openid email profile`, groups claim) | Bot Framework; optional Graph **application** permissions (`Calendars.Read`, `Presence.Read.All`) |
| Redirect URI | `https://<host>/api/auth/callback` | none |

**Recommended: two apps.** The bot app may hold tenant-wide *application* permissions; the
login app holds only what a sign-in needs. Separate secrets, separate rotation, and revoking
the bot never locks people out of the cockpit. One app for both works (SOKKAN checks the
token audience per use) but mixes a sign-in secret with a tenant-wide read permission —
avoid it outside a demo.

### 2.1 Login app (skip if the cockpit already signs in with this tenant's Entra ID)

1. Entra admin center → Identity → Applications → **App registrations → New registration**:
   *SOKKAN cockpit*, **Accounts in this organizational directory only**, redirect URI (Web)
   `https://<host>/api/auth/callback`.
2. Certificates & secrets → New client secret → straight into the secrets store.
3. Token configuration → **Add groups claim** (security groups; « Group ID » or « sAMAccountName /
   cloud-only group names » — see OPERATIONS.md § SSO for the object-id caveat).
4. Instance env: `SOKKAN_OIDC_ISSUER=https://login.microsoftonline.com/<tenant id>/v2.0`,
   `SOKKAN_OIDC_CLIENT_ID=<login app id>`, `SOKKAN_OIDC_CLIENT_SECRET` (from the store),
   `SOKKAN_OIDC_SCOPES=openid email profile`, `SOKKAN_OIDC_GROUPS_CLAIM=groups`.

## 3. Register the bot — Teams Developer Portal

Print the steps with this instance's values (changes nothing, needs no Azure CLI):

```bash
scripts/teams-register.sh --public-url https://<host> [--calendar] [--presence]
```

1. <https://dev.teams.microsoft.com> (tenant admin) → **Tools → Bot management → + New Bot** →
   name *Nina (SOKKAN)*. The portal creates the bot and an Entra app; **bot id = that app's
   Application (client) ID**.
2. The bot → **Configure** → *Endpoint address* `https://<host>/api/teams/messages` → Save.
3. The bot → **Client secrets → Add a client secret for your bot** → copy it once into the
   secrets store (the portal never shows it again).
4. The bot → **Channels**: Microsoft Teams present.
5. Entra admin center → App registrations → **All applications** → the bot's app:
   *Authentication → Supported account types* shows **Multiple organizations** — **leave it**:
   the Bot Framework signs activities of a multi-tenant bot with its own issuer
   (`api.botframework.com`), the only one SOKKAN accepts; switching to single tenant breaks
   the bot (tokens issued by Entra, refused 401). The tenant is enforced by SOKKAN on every
   activity (`SOKKAN_TEAMS_TENANT_ID`, TEAMS.md § 2). Overview → *Directory (tenant) ID*.
6. Optional (the morning brief's Graph reads): same app → **API permissions → Add a
   permission → Microsoft Graph → Application permissions** → `Calendars.Read` and/or
   `Presence.Read.All` → **Grant admin consent**. Nothing else: answering and posting
   approvals need no Graph permission. Limit `Calendars.Read` to the POC mailboxes with an
   Exchange application access policy (TEAMS.md § 3.2 step 3).

Then, with the bot id:

```bash
scripts/teams-register.sh --public-url https://<host> --app-id <bot id> --tenant-id <tenant id> --out ./teams-app
```

builds `./teams-app/sokkan-teams-app.zip` and prints the env lines of § 4.

*Variant with an Azure subscription*: `scripts/teams-register.sh --mode azure --public-url
https://<host> --resource-group <rg> [--calendar] --dry-run`, read every command, then run it
without `--dry-run` (it writes the secret to a 0600 file and prints nothing secret).

## 4. Configure the instance

Render the client secret from the store to a file readable only by the API (0600, owner of
the API process / mounted into the container), then set:

```bash
SOKKAN_FEATURE_TEAMS=1
SOKKAN_TEAMS_APP_ID=<bot id>
SOKKAN_TEAMS_TENANT_ID=<tenant id>
SOKKAN_TEAMS_PUBLIC_URL=https://<host>
SOKKAN_TEAMS_APP_PASSWORD_FILE=/run/secrets/sokkan-teams-app-password   # 0600
# optional
SOKKAN_TEAMS_PROACTIVE_S=60          # pending approvals pushed to channels (0 = off)
SOKKAN_TEAMS_CALENDAR=0              # if no Graph calendar permission was granted
```

Recreate the API container (compose: `docker compose up -d --force-recreate api`; Kubernetes:
rollout). Check, as an instance admin:

```bash
curl -s -b "sokkan_session=$COOKIE" https://<host>/api/admin/teams | jq '{enabled, missing, tenant, app_id, endpoint, proactive}'
```

→ `enabled: true`, `missing: []`, the endpoint as registered. The admin cookie is read from
the browser's dev tools after signing in; it is a credential — an env var of this shell only.

## 5. Install the app in Teams

1. Teams admin center → **Teams apps → Manage apps → Upload new app** → `sokkan-teams-app.zip`
   (or Setup › Organization › Teams → *App package (.zip)* on the instance — same manifest).
   Alternative: Developer Portal → Apps → *Import app* → Publish → to your org.
2. Teams admin center → **Org-wide app settings**: custom apps allowed; **Permission
   policies**: the app allowed for the POC users; **Setup policies** (optional): pin it.
3. In Teams: a team → *Apps* → *Nina (SOKKAN)* → *Add to a team*. Uploads can take a few
   minutes to propagate.
4. Each test user signs in **once** to the cockpit with Entra ID (step 2.1): that records the
   link Teams user → SOKKAN account.

## 6. Map a channel

Channel ⋯ → *Get link to channel* → the `19:…@thread.tacv2` part. Setup › Organization ›
Teams → channel id, project, level (its audience), « post approvals here » → *map*; or

```bash
curl -s -b "sokkan_session=$COOKIE" -X PUT https://<host>/api/admin/teams/channels \
  -H 'content-type: application/json' \
  -d '{"channel_id": "19:…@thread.tacv2", "project": "<slug>", "level": "project", "approvals": true}'
```

Smoke: in the channel, `@Nina status` → a card answers; `GET /api/admin/teams` →
`last_inbound` now shows the claim names Microsoft sent (write them down).

## 7. Run the live checks

From a checkout of the same version, with the backend's Python environment:

```bash
export SOKKAN_TEAMS_LIVE=1
export SOKKAN_TEAMS_APP_ID=<bot id> SOKKAN_TEAMS_TENANT_ID=<tenant id>
export SOKKAN_TEAMS_APP_PASSWORD="$(cat /run/secrets/sokkan-teams-app-password)"   # this shell only
export SOKKAN_TEAMS_LIVE_INSTANCE=https://<host>
export SOKKAN_TEAMS_LIVE_CHANNEL='19:…@thread.tacv2'
export SOKKAN_TEAMS_LIVE_ADMIN_COOKIE=<sokkan_session of an instance admin>
# optional: SOKKAN_TEAMS_LIVE_USER=<upn> SOKKAN_TEAMS_LIVE_USER_OID=<object id>
#           SOKKAN_TEAMS_LIVE_EXPECT_ROLES=Calendars.Read
python -m pytest tests/teams_live -v -rs -s                 # automatic: ~1 min
SOKKAN_TEAMS_LIVE_MANUAL=1 SOKKAN_TEAMS_LIVE_TIMEOUT_S=300 \
  python -m pytest tests/teams_live -v -rs -s -m manual     # guided: a person acts in Teams
```

Reading the result:

* `PASSED` — that contract holds against Microsoft. `SKIPPED (missing X)` — a variable is not
  set (or an optional permission was not granted): not a failure, but say so in the report.
* `test_proactive_post_and_update_in_the_channel` posts a card « SOKKAN live check » in the
  channel and replaces it a few seconds later: watch it happen.
* Each `manual` test prints `>>> what to do` and waits for the matching journal event
  (`teams.status`, `teams.approval.approve`, `teams.decision`, `teams.approval.post`,
  `agent.approve`); `FAILED … within N s` = the action did not reach SOKKAN (check the API log
  for `[teams] rejected request: <reason>`).
* Visual checks the tests cannot do (tests/teams_live/README.md): full-width card, colored
  Approve / Refuse, the second viewer's copy turning into « Approved by … », no `<at>` tag in
  answers, mobile rendering. Screenshots desktop + mobile.

## 8. Rollback

In order, stop at the level you need:

1. **Silence**: `SOKKAN_FEATURE_TEAMS=0` (or unset) + recreate the API → `/api/teams/messages`
   answers 404, no proactive post; nothing else changes (teams.db kept).
2. **Uninstall**: Teams admin center → Manage apps → the app → *Block* (or delete); remove it
   from the team.
3. **Revoke the secret**: Developer Portal → the bot → Client secrets → delete (or Entra →
   the bot's app → Certificates & secrets → delete); remove it from the store and the
   rendered file.
4. **Remove the Graph permissions**: Entra → the bot's app → API permissions → remove → also
   Enterprise applications → the app → Permissions → revoke admin consent.
5. **Delete the bot**: Developer Portal → Bot management → delete; Entra → App
   registrations → the bot's app → Delete (and its Enterprise application). Azure variant:
   `az bot delete -g <rg> -n <name>`, `az ad app delete --id <app id>`.
6. On the instance: unset the `SOKKAN_TEAMS_*` variables; `teams.db` / `teams.key` can be
   deleted (cached tokens, channel map, approval tokens — no business data).

## 9. What to bring back

The bot id and tenant id (not secret), the supported account type the portal left on the bot
app, `last_inbound.claims`, the pytest summary of both runs, the screenshots, every difference
from the simulator (an invoke payload, a claim name, an HTML shape) — each one becomes a case in
`tests/teams_sim.py` before `teams` moves from experimental to beta.
