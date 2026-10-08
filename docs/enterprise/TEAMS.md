# Microsoft Teams — Nina in Teams (3.4)

Feature `teams` (requires `assistant`, `classification`, `sso`; off by default, status
experimental). Code: `backend/teams/` (bot, signed approvals, Graph), `backend/calendars/`
(the calendar interface of the brief). Built and tested against a Graph / Bot Framework
simulator (`tests/teams_sim.py`, `tests/test_teams.py`, `tests/test_teams_bridge.py`): **no
real app has been registered yet** — § 8 lists what only a real tenant can confirm,
`tests/teams_live/` checks it, § 9 is the order of the day the tenant exists. 3.4.0
« Bridge » adds the app package and the registration script (§ 3), Adaptive Cards checked
against the 1.5 schema and the proactive push of pending approvals (§ 7). **Day-one
setup on a real tenant, step by step (Developer Portal path, no Azure subscription):
[TEAMS-SETUP.md](TEAMS-SETUP.md).**

## 1. What it does

| In Teams | What happens | As whom |
|---|---|---|
| `@Nina <question>` in a channel, a group chat or 1:1 | Nina answers from the project's memory | the sender, capped by the channel's level |
| `@Nina status` / `état` | card: board columns, latest agent runs, waiting approvals | the sender's clearance ∧ the channel's level |
| `@Nina card: <title>` / `carte : …` | a card in the project (dev+) | the sender; level = channel level |
| `@Nina note la décision : …` / `decision: …` | a note `decision-<date>-<slug>` in the project memory: author, date, link to the thread, level inherited | the sender (dev+) |
| `@Nina run <agent>` / `lance l'agent …` | an approval card *Run the agent X?* | requested by the sender |
| `@Nina approvals` / `approbations` | one card per agent waiting for activation | — |
| click **Approve** / **Refuse** on a card | the action, as the person who clicked | the clicker (four-eyes: not the requester) |
| *(nothing — 3.4.0)* an agent proposal, a pending change or a tool call of an agent run starts waiting | its approval card is **posted in the project's channel** (proactive) and replaced by its outcome once decided — in Teams or in the cockpit | the clicker; a tool call: the agent's owner or a project admin |
| *(in the cockpit — 3.4.1)* « find me someone available to help with ‹X› » to Nina | Nina proposes who (presence + calendar), the channel and the message; **Send** posts it with a real @mention (§ 10) | the requester, on their click |

In a 1:1 chat with no mapping, start with `in <project>: …` (or nothing if the person has a
single project). The answer carries `_classification: <label>_` when it was built from notes
above `project`.

**Nina answers in the language of the message** (3.4.2): French when the French trigger
matched (`état`, `carte :`, `note la décision :`, `lance l'agent`, `approbations`, `dans
<projet> :`) or the sentence reads as French, English otherwise — the replies, the refusals
(not linked, no access, no agent…) and the cards' headings and buttons (*Approuver / Refuser /
Ouvrir dans SOKKAN*). A card keeps its language until it is decided.

**Prerequisite — the 3.0 memory store.** The decision capture writes into the **project's**
memory and carries the channel's level: both need the 3.0 store (Postgres + pgvector,
`CORTHEXIS_MEMORY_BACKEND=auto`, the default). On an instance kept on the 2.x index
(`CORTHEXIS_MEMORY_BACKEND=sqlite`) a decision for a project other than `default`, or from a
channel above the default level, is **refused** — Nina says so in the language of the
message (« La mémoire par projet et la classification demandent le store 3.0 (Postgres)… —
rien n'a été écrit ») and journals `teams.decision.refused`; nothing is written anywhere.
Seen live on 08.10: before 3.4.2 the note landed under `projects/<slug>/memory/` where nothing
indexed it. How to turn the store on: [UPGRADE.md § Turn the store on
later](../UPGRADE.md#turn-the-store-on-later-sqlite-mode); the state is read in Setup › Engines
and `GET /api/memory/stats` → `store`.

## 2. Security model

* **One tenant per instance — checked by SOKKAN, not by the registration.** The bot's Entra
  app is **multi-tenant** (« Multiple organizations », what the Teams Developer Portal
  creates; an Azure Bot of type *Multi Tenant*): the Bot Framework then signs every activity
  with a token issued by `https://api.botframework.com`, which is the only issuer `botauth`
  accepts. A *Single Tenant* bot would send tokens issued by the tenant's Entra endpoint and
  be refused. The tenant itself is enforced on every activity: `channelData.tenant.id` must be
  `SOKKAN_TEAMS_TENANT_ID`, else 401 before anything is read; and app tokens are requested
  from that tenant's endpoint only (`login.microsoftonline.com/<tenant>`). Confirmed on a
  real tenant (3.4.1).
* **Signed requests.** `Authorization: Bearer <JWT>` verified on every call: RS256 key of the
  Bot Framework OpenID metadata (cached 24 h, refreshed on an unknown `kid`), issuer
  `https://api.botframework.com`, audience = `SOKKAN_TEAMS_APP_ID`, expiry (5 min leeway),
  key endorsed for `msteams`, `serviceUrl` claim = the activity's `serviceUrl`, and that URL
  is a Microsoft host (`smba.trafficmanager.net`, `*.botframework.com`…; add a sovereign
  cloud with `SOKKAN_TEAMS_SERVICE_HOSTS`) — our bot token is never sent anywhere else.
* **The person, not the bot.** A Teams user acts only through their SOKKAN account. The link
  Entra object id → email is recorded when they **sign in to SOKKAN with Entra ID** (OIDC
  claims `oid` + `tid`, tenant checked): never inferred from a display name, no Graph lookup.
  Not linked → Nina answers only "sign in once to SOKKAN". Then their role in the channel's
  project decides (no role = "no access"), and Nina reads memory with **their** scope —
  `classification.scope_for(person, project)` — capped by the channel's level.
* **Channel level.** A mapped channel has a level (its audience): what Nina says there is
  read by every member, so she never uses a note above it, whoever asks; decisions and cards
  written from it inherit it (at least `project`).
* **Approvals.** A card carries a token `base64(payload).HMAC-SHA256` (key of the instance),
  expiring (24 h), single use (one row per nonce, consumed atomically), optionally bound to
  one approver (Entra object id). A click = the clicker's identity + their role; four-eyes
  (`four_eyes` on) refuses the requester; a refused action gives the token back for someone
  entitled. Every decision is journaled (`teams.approval.approve|refuse`).
* **Tokens at rest.** Outbound tokens (Bot Framework, Graph — client credentials) are cached
  in `teams.db`, **Fernet-encrypted** with `teams.key` (0600, in `SOKKAN_DATA_DIR`). The client
  secret stays in the environment (`SOKKAN_TEAMS_APP_PASSWORD`), never logged.
* **Audit.** Refused requests are logged by the API (`[teams] rejected request: <reason>`,
  not in the journal: an unauthenticated caller must not fill it); `teams.status`, `teams.decision`,
  `teams.decision.refused` (3.4.2: the store does not serve the project / the level),
  `teams.approval.request|approve|refuse`, `teams.channel.map|unmap`, `board.card.create`;
  every note Nina used: `note_access` with `via = teams` (SECURITY.md § 8).

## 3. Register the app (customer's tenant)

The full day-one procedure (prerequisites, two Entra apps, Developer Portal, live checks,
rollback) is [TEAMS-SETUP.md](TEAMS-SETUP.md); this section is the reference. Two ways, same
result. Both need a **tenant admin** (Global Administrator, or Application
Administrator + Teams Administrator). The Azure Bot resource needs an **Azure subscription in
that tenant** (F0 is free, but a Microsoft 365 Business tenant has none by default) — without
one, register the bot in the **Teams Developer Portal** instead (step 2).

### 3.1 With the script

`scripts/teams-register.sh --public-url https://<host> [--app-id <bot id>]` (default mode
`portal`: no Azure CLI, changes nothing) prints the Developer Portal steps with this
instance's values, then — given the bot id — builds the package and the env lines. With an
Azure subscription, `--mode azure`:

```bash
az login --tenant <tenant id or domain>
scripts/teams-register.sh --mode azure --public-url https://sokkan.example.ch --resource-group rg-sokkan \
    [--calendar] [--presence] [--secret-file ./teams-app-secret] [--no-bot] [--dry-run]
```

It creates the multi-tenant app registration and its service principal, a client secret
(written to `--secret-file`, mode 0600, never printed), the Azure Bot (type **MultiTenant**,
endpoint `https://<host>/api/teams/messages`, channel Microsoft Teams), the optional Graph
application permissions (role ids looked up by name) with the admin consent, and the app
package (`teams-app/sokkan-teams-app.zip`). It prints the `SOKKAN_TEAMS_*` lines of § 4.
`--dry-run` prints every command and changes nothing.

### 3.2 In the portals, step by step

1. **Entra admin center → Identity → Applications → App registrations → New registration**:
   *Nina (SOKKAN)*, **Accounts in any organizational directory** (multi-tenant — the token
   issuer `botauth` expects; SOKKAN still refuses every tenant but yours, § 2), no
   redirect URI. Note the *Application (client) ID* and the *Directory (tenant) ID*.
   Certificates & secrets → New client secret (24 months max; calendar the rotation).
2. **Teams Developer Portal** (no Azure subscription — the usual case for a Microsoft 365
   Business tenant): TEAMS-SETUP.md § 3. **With a subscription — Azure portal → Create a resource → Azure Bot**: type of app **Multi Tenant**, *Use existing
   app registration* (the id above); Configuration → messaging endpoint
   `https://<sokkan host>/api/teams/messages`; Channels → **Microsoft Teams** → accept.
3. **API permissions → Microsoft Graph → Application** (only what you use), then
   **Grant admin consent**:

   | Permission | Type | Why | Needed |
   |---|---|---|---|
   | `Calendars.Read` | Application | the brief reads the calendar of the person it is for | optional (brief) |
   | `Presence.Read.All` | Application | presence in the brief | optional |

   Answering in Teams and posting approvals needs **no Graph permission** (Bot Framework only).
   Restrict `Calendars.Read` to the people of the POC with an Exchange **application access
   policy** (`New-ApplicationAccessPolicy -AccessRight RestrictAccess -AppId <id>
   -PolicyScopeGroupId <mail-enabled group>`) or RBAC for Applications.
4. **SSO**: SOKKAN's OIDC login must be Entra ID (same tenant) so that the id_token carries
   `oid` and `tid` (default claims). Each person signs in to SOKKAN once — that is what links
   their Teams identity to their SOKKAN account.
5. **App package**: Setup › Organization › Teams → **App package (.zip)**
   (`GET /api/admin/teams/package`), or `scripts/teams-manifest.py --out teams-app --check`
   (same manifest — one source, `backend/teams/manifest.py`): `manifest.json` (schema v1.17,
   bot in personal / team / groupChat, command list, `validDomains` = the instance's host
   only; `webApplicationInfo` only with `--sso` / `?sso=1`, when the Entra app exposes
   `api://<host>/<app id>`), `color.png` 192×192, `outline.png` 32×32 white on transparent.
   **Teams admin center → Teams apps → Manage apps → Upload new app**; then allow it for the
   POC users (permission policy) and pin it if wanted (setup policy). In a small tenant,
   check *Org-wide app settings → Custom apps* is allowed.
6. Add the app to the team; get each channel id (channel ⋯ → *Get link to channel*: the
   `19:…@thread.tacv2` part) and map it: Setup › Organization › Teams (« post approvals
   here » on by default), or
   `PUT /api/admin/teams/channels {"channel_id": "19:…", "project": "radio", "level": "project", "approvals": true}`.

## 4. Variables

| Variable | Meaning |
|---|---|
| `SOKKAN_FEATURE_TEAMS=1` | the switch (requires `assistant`, `classification`, `sso`) |
| `SOKKAN_TEAMS_APP_ID` | Application (client) ID = bot id |
| `SOKKAN_TEAMS_APP_PASSWORD` | client secret (a secret: from the vault / the env file, 0600) |
| `SOKKAN_TEAMS_APP_PASSWORD_FILE` | or: a 0600 file holding it (preferred — rendered from your secrets store, not in the environment) |
| `SOKKAN_TEAMS_TENANT_ID` | the customer's tenant; every other tenant is refused |
| `SOKKAN_TEAMS_PUBLIC_URL` | public base URL (default `SOKKAN_PUBLIC_URL`) |
| `SOKKAN_TEAMS_SERVICE_HOSTS` | extra allowed serviceUrl hosts (sovereign clouds), comma list |
| `SOKKAN_TEAMS_CALENDAR=0` | do not offer the Graph calendar to the brief |
| `SOKKAN_TEAMS_PROACTIVE_S` | period of the proactive-approvals sync (default 60 s; it also runs on every change); `0` = no proactive post |
| `SOKKAN_TEAMS_SERVICE_URL` | where to post in a channel Teams has not written to yet (default `https://smba.trafficmanager.net/teams/`); once a channel has sent one activity, the serviceUrl Microsoft gave is used |
| `SOKKAN_TEAMS_OPENID_URL`, `SOKKAN_TEAMS_LOGIN_URL`, `SOKKAN_TEAMS_GRAPH_URL` | endpoints (defaults: Microsoft public cloud; the tests point them at the simulator) |
| `SOKKAN_TEAMS_DB`, `SOKKAN_TEAMS_KEY_FILE` | default `$SOKKAN_DATA_DIR/teams.db` / `teams.key` (back them up with the data) |

## 5. The calendar interface (brief)

`backend/calendars/` — `CalendarProvider` (`configured()`, `events(email, start, end)`),
`register()`, `provider()`, `events_for(email, day)`; `teams.graph.GraphCalendar` is the
Microsoft 365 provider (`/users/{email}/calendarView`, UTC). Named `calendars` (plural): a
`calendar` package in `backend/` would shadow the Python standard library module. A brief
built from project notes inherits their highest level and logs its reads with `via = brief`
(`classification.log_access`).

## 6. Check after activation

1. `GET /api/admin/teams` → `missing: []`, the channels listed.
2. In a mapped channel, `@Nina status` → the card; `GET /api/audit?q=teams.status` shows the
   sender's email.
3. Two people of different clearances ask the same question in 1:1 → different answers; the
   access log (`/api/classification/audit`) shows both with `via = teams`.
4. `@Nina run <agent>` → the requester's click is refused (four-eyes), a maintainer's runs
   it; a second click says *already approved by …*.
5. `@Nina note la décision : …` → the note appears in CortHeXis with author, date, thread link.
6. A request with a forged token (`curl -X POST …/api/teams/messages`) → 401 and
   `[teams] rejected request` in the API log.

## 7. Proactive approvals (3.4.0)

`backend/teams/proactive.py`. What waits for a human in a project — an agent proposal or a
pending change (`agent.activate`), a tool call of an agent run (`run.tool`) — is posted in
every channel mapped to the project with « post approvals here » on, the moment it starts
waiting (hooks in `agents`, debounced) and by a periodic safety net
(`SOKKAN_TEAMS_PROACTIVE_S`).

* One card per (approval, channel), recorded in `teams.db` (`proactive`): a sync twice posts
  nothing new; a post that failed is retried at the next sync (journal: `teams.approval.post`).
* When it stops waiting — decided in Teams, in the cockpit, the run moved on — the card is
  **replaced** (`PUT /v3/conversations/{id}/activities/{activityId}`) by its outcome
  (« Approved by max@… — decided in SOKKAN ») and its signed token is spent: a late click on
  an old copy does nothing.
* A tool call is decided by the agent's owner (dev+) or an admin of the project, as in the
  cockpit; the decision reaches the live session on the API's event loop.
* **Level**: an approval whose object is above the channel's level (a run whose session read
  confidential notes, in a « project » channel) is announced without its content — « an
  approval waits in <project> at level X, open SOKKAN » — with no button and no token.
* Cards: Adaptive Cards 1.5 (`backend/teams/cards.py`) checked against the official schema
  (vendored in `tests/fixtures/teams/`): `Action.Execute` (verb + data) in an `ActionSet`,
  with an `Action.Submit` fallback carrying the verb for older clients, `refresh`
  (verb `refresh`: any viewer gets the current state — decided, expired — without deciding;
  automatic for ≤ 60 members, « Refresh card » beyond), `fallbackText`, `msteams.width = Full`.

## 10. Nina asks for help (3.4.1)

From the cockpit's Nina panel, in the project selected in the header:

> *trouve-moi quelqu'un de disponible pour aider sur ‹Upgrade Postgres›* · *qui est disponible pour
> aider sur la carte #16 ?* · *demande de l'aide sur la carte #16* · *who is available to help with
> the TLS rotation?* · *find me someone to help with “Upgrade Postgres”* · *ask for help on card #16*

The request is recognised in Python (FR / EN, `teams.outreach.intent` — no model decides an
action); a question (« how do I get help… ») goes to Nina as usual. Nina answers with a
**proposal**, nothing else happens:

* **who** — the members of the project with the role `dev` or above (grants, SSO teams, instance
  roles), minus the requester and the assignee of the card; each with their availability now
  and why: Teams presence (`Presence.Read.All`, for people linked to Entra by their SOKKAN
  sign-in) and today's calendar (`Calendars.Read`: busy / out of office now, next free slot,
  in `SOKKAN_TZ`). Ranked available › free (calendar only) › unknown › away › busy › out of
  office. Graph down, permission missing, no link → « availability unknown », never an error;
* **where** — the channel mapped to the project; several → the widest audience whose level
  allows the subject; none → Nina says so (Setup › Organization › Teams) and still shows who
  is available;
* **what** — « @Ana, could you help Claire with “Upgrade Postgres 15 → 16”? » in the requester's
  language, a link to the card when there is one. **Classification**: a card above the
  channel's level is « a confidential card (#16) » — its title never reaches Teams — and the
  proposal says so; a free-text subject is the person's own words;
* **send** — the person picks the recipient among the candidates and clicks **Send**
  (`POST /api/assistant/outreach/send`, the proposal's signed, single-use, 1 h token, valid for
  the requester in that project only). The bot posts a new thread in the channel with a real
  mention — `<at>Ana</at>` in the text + `entities: [{type: mention, text, mentioned: {id:
  <Entra object id>, name}}]` — or the plain name when the person never signed in to SOKKAN with
  Entra ID. Journal: `teams.outreach` (actor, channel, recipient, card, subject). Nina answers
  with the link to the thread. **Cancel** sends nothing; the token expires unused.

Prerequisites: `teams` on, a channel mapped to the project, the candidates signed in once with
Entra ID (presence and a real mention), the two optional Graph application permissions. Live
check: TEAMS-SETUP.md § 7.1.

## 8. Limits — what needs a real tenant

The simulator follows the published contracts; these points are confirmed only against
Microsoft (`tests/teams_live/`, see its README):

* ~~the claim names of a live Bot Framework token~~ — confirmed (3.4.1): `aud, exp, iss, nbf,
  serviceurl` (lowercase), issuer `api.botframework.com` with a multi-tenant bot registration
  (`GET /api/admin/teams` → `last_inbound.claims` records them without values);
* Teams' HTML in `text` around the @mention; the `adaptiveCard/action` invoke payloads of the
  desktop, web and mobile clients; the `Action.Submit` fallback on an old client;
* posting to `/v3/conversations/{channel id}/activities` = a new thread in the channel, and
  `PUT` of that activity (the live test does both); the default serviceUrl for a channel
  never seen;
* the manifest upload in the admin center; the deep-link format of a thread (decision notes).

Not built: thread reading (a decision is the sentence after the command, not a summary of
the thread); Graph channel listing (channel ids are pasted by the admin); presence is exposed
(`teams.graph.presence`) but not shown; one tenant and one Teams app per instance.

## 9. Day of the tenant — in this order

Detailed in [TEAMS-SETUP.md](TEAMS-SETUP.md).

1. Tenant admin account in the password manager; MFA on.
2. `scripts/teams-register.sh --public-url …` (Developer Portal steps), or `--mode azure
   --dry-run` then for real with a subscription. Secret → secrets store, rendered 0600.
3. Instance env: `SOKKAN_FEATURE_TEAMS=1`, `SOKKAN_TEAMS_APP_ID`, `SOKKAN_TEAMS_APP_PASSWORD_FILE`,
   `SOKKAN_TEAMS_TENANT_ID`, `SOKKAN_TEAMS_PUBLIC_URL` (an https host Microsoft can reach);
   SSO through Entra ID of the same tenant (`oid`/`tid`); restart; `GET /api/admin/teams` →
   `missing: []`.
4. Two test users (one dev, one maintainer of a test project) sign in once to SOKKAN with
   Entra ID (links recorded).
5. Upload the app package (§ 3.2 step 5), add it to a team, map a channel to the test project.
6. `SOKKAN_TEAMS_LIVE=1 python -m pytest tests/teams_live -v -s` (automatic checks), then
   `SOKKAN_TEAMS_LIVE_MANUAL=1 … -m manual` with the two users (mention, four-eyes approval on
   desktop then mobile, decision, approval proposed in the cockpit → posted → approved in the
   cockpit → card replaced).
7. Record `last_inbound.claims`, screenshots of the cards (desktop + mobile) and the results
   in the POC report; fix what differs from the simulator, extend `tests/teams_sim.py` with
   the real payloads.
8. Only then: `teams` from experimental to beta (feature registry, CHANGELOG).
