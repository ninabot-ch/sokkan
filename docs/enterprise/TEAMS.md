# Microsoft Teams — Nina in Teams (3.4)

Feature `teams` (requires `assistant`, `classification`, `sso`; off by default, status
experimental). Code: `backend/teams/` (bot, signed approvals, Graph), `backend/calendars/`
(the calendar interface of the brief). Built and tested against a Graph / Bot Framework
simulator (`tests/teams_sim.py`, `tests/test_teams.py`): **no real app has been registered
yet** — § 7 lists what only a real tenant can confirm.

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

In a 1:1 chat with no mapping, start with `in <project>: …` (or nothing if the person has a
single project). The answer carries `_classification: <label>_` when it was built from notes
above `project`.

## 2. Security model

* **Single tenant.** Every activity whose tenant (`channelData.tenant.id`) is not
  `SOKKAN_TEAMS_TENANT_ID` is refused (401) before anything is read.
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
  `teams.approval.request|approve|refuse`, `teams.channel.map|unmap`, `board.card.create`;
  every note Nina used: `note_access` with `via = teams` (SECURITY.md § 8).

## 3. Register the app (customer's tenant)

1. **Entra ID → App registrations → New registration**: *Nina (SOKKAN)*, **single tenant**
   (Accounts in this organizational directory only). Note the *Application (client) ID* and
   the *Directory (tenant) ID*. Certificates & secrets → a client secret (24 months max,
   calendar the rotation).
2. **Azure Bot** resource (*Azure Bot*, type **Single Tenant**, the app above): messaging
   endpoint `https://<sokkan host>/api/teams/messages`; Channels → **Microsoft Teams**.
3. **API permissions → Microsoft Graph → Application** (only what you use), then
   **Grant admin consent**:

   | Permission | Type | Why | Needed |
   |---|---|---|---|
   | `Calendars.Read` | Application | the brief reads the calendar of the person it is for | optional (brief) |
   | `Presence.Read.All` | Application | presence in the brief | optional |

   Answering in Teams needs **no Graph permission** (Bot Framework only). Restrict
   `Calendars.Read` to the people of the POC with an Exchange **application access policy**
   (`New-ApplicationAccessPolicy -AccessRight RestrictAccess -AppId <id>
   -PolicyScopeGroupId <mail-enabled group>`) or RBAC for Applications.
4. **SSO**: SOKKAN's OIDC login must be Entra ID (same tenant) so that the id_token carries
   `oid` and `tid` (default claims). Each person signs in to SOKKAN once.
5. **Manifest**: `GET /api/admin/teams/manifest` (instance admin) gives `manifest.json`
   (v1.17, bot scopes personal / team / groupChat, command list). Zip it with `color.png`
   (192×192) and `outline.png` (32×32, transparent) → Teams admin center → Manage apps →
   Upload; allow it for the POC users (app permission policy).
6. Add the app to the team; get each channel id (channel ⋯ → *Get link to channel*: the
   `19:…@thread.tacv2` part) and map it: Setup › Organization › Teams, or
   `PUT /api/admin/teams/channels {"channel_id": "19:…", "project": "radio", "level": "project"}`.

## 4. Variables

| Variable | Meaning |
|---|---|
| `SOKKAN_FEATURE_TEAMS=1` | the switch (requires `assistant`, `classification`, `sso`) |
| `SOKKAN_TEAMS_APP_ID` | Application (client) ID = bot id |
| `SOKKAN_TEAMS_APP_PASSWORD` | client secret (a secret: from the vault / the env file, 0600) |
| `SOKKAN_TEAMS_TENANT_ID` | the customer's tenant; every other tenant is refused |
| `SOKKAN_TEAMS_PUBLIC_URL` | public base URL (default `SOKKAN_PUBLIC_URL`) |
| `SOKKAN_TEAMS_SERVICE_HOSTS` | extra allowed serviceUrl hosts (sovereign clouds), comma list |
| `SOKKAN_TEAMS_CALENDAR=0` | do not offer the Graph calendar to the brief |
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

## 7. Limits — what needs a real app

* Not verified against Microsoft yet: the exact JWT claims of the live Bot Framework
  (`serviceurl` casing), Teams' HTML in `text`, `adaptiveCard/action` invoke payloads from
  the desktop/mobile clients, the manifest upload, the deep-link format of a thread. The
  simulator follows the published contracts; the first POC session must run § 6 end to end.
* No proactive push of every new pending approval to Teams yet (on demand: `@Nina approvals`);
  no thread reading (a decision is the sentence after the command, not a summary of the thread);
  no Graph channel listing (channel ids are pasted by the admin).
* Presence is exposed (`teams.graph.presence`) but not yet shown anywhere.
* One tenant per instance; one Teams app per instance.
