# Connectors — mail, calendar, shared documents

Feature `connectors` · status **experimental** · **off by default** in both editions ·
code `backend/connectors/` · MCP server `sokkan-connectors` (`backend/connectors_mcp.py`) ·
API `/api/connectors/*` · settings in **Settings › Connectors**.

> A coworker works where things are: the mailbox, the calendar, the shared documents. SOKKAN
> reads them **on demand, as the person** who drives the session, and **never acts outside
> SOKKAN without that person's approval card**.

Contents: [1 What a session can do](#1-what-a-session-can-do) · [2 Providers](#2-providers) ·
[3 Approval cards](#3-approval-cards) · [4 Delegation and secrets](#4-delegation-and-secrets) ·
[5 Classification](#5-classification-where-content-may-go) · [6 What a project may reach](#6-what-a-project-may-reach) ·
[7 Settings](#7-settings) · [8 Engines (harness contract)](#8-engines-harness-contract) ·
[9 Indexing shared documents](#9-indexing-shared-documents-doc_ingest) · [10 Demo](#10-demo-mode) ·
[11 Tests](#11-tests) · [12 Limits](#12-limits)

## 1. What a session can do

| Tool (`sokkan-connectors__…`) | Kind | Effect |
|---|---|---|
| `status` | read | which connectors the person has, whether content may reach this model, cards waiting |
| `mail_list`, `mail_read_thread` | read | a folder of the person's mailbox (or a shared mailbox opened to the project), a whole conversation |
| `mail_draft` | **write** (engine permission) | a reply or a new message as a **draft in the person's own mailbox** — never sent |
| `mail_propose_send` | card | asks the person to approve sending a draft |
| `calendar_events`, `calendar_free_slots`, `calendar_prepare_meeting` | read | events, free slots (with colleagues' busy times where Microsoft 365 shares them), a meeting with its agenda, attachments and the mails that mention it |
| `calendar_propose_event` | card | asks the person to approve creating an event (with invitations) |
| `files_list`, `files_search`, `files_read` | read | browse, find, read as text (Word, PowerPoint, Excel, PDF, text) |
| `files_propose_write` | card | asks the person to approve depositing a deliverable in the shared folder |
| `actions_list` | read | the person's cards and their outcome |

No tool takes a person argument: the server reads `SOKKAN_SESSION_USER`, set by the API. The
model cannot choose whose mailbox it reads. A session without an identified person reads nothing.

## 2. Providers

| Family | Provider | Connection | Notes |
|---|---|---|---|
| mail | `graph` (Microsoft 365) | OAuth, the person's consent | Mail.ReadWrite, Mail.Read.Shared, Mail.Send only when sending is allowed |
| mail | `imap` | the person's login (app password) | IMAP for reading and drafts (APPEND `\Draft`), SMTP submission on a card; `ssl` / `starttls`; clear text refused outside a private test network |
| calendar | `graph` | OAuth | calendarView, getSchedule, events |
| calendar | `caldav` | the person's login | Nextcloud, Radicale, SOGo, iCloud…; REPORT with server-side expansion, PUT `If-None-Match: *` |
| calendar | `ics` | the private ICS address set for the brief (Helm) | read only |
| files | `graph` (SharePoint / OneDrive) | OAuth | `/me/…` = OneDrive, `/<alias>/…` = a library the admin declared (`graph_sites`) |
| files | `webdav` | the person's login | Nextcloud, ownCloud, a NAS |
| files | `folder` | none (host mount) | an SMB/NFS share mounted by the operator; `write_root` = a separate read-write mount for deliverables, absent = read only |
| all | `demo` | one click | invented data, see § 10 |

A person may connect several; the first connected in the order above answers unless a tool is
given `provider`.

## 3. Approval cards

Sending, creating an event and depositing a file are **actions**:

1. the session calls `*_propose_*`: SOKKAN stores the action (payload **encrypted**), shows a
   card to the person (Settings › Connectors › To approve), journals the request
   (`hitl_journal`, kind `connector`, `audit` `connectors.<kind>.proposed`);
2. the person approves or rejects — **only the person whose account acts** (another member, an
   administrator, a delegated reviewer get « not found »);
3. the **API** runs it with the person's credentials, never the session's process; the result
   is journaled (`connectors.<kind>.done | failed | rejected`, `hitl` decision `allow | deny`).

Guarantees, each tested: a card is decided once (atomic claim; a double click gets 409); it
expires after 24 h; **what runs is what the card showed** — a draft edited after the card is
refused (« the draft changed »), the person proposes again; nothing is overwritten (a numbered
copy); turning sending off (`allow_send: false`) refuses new cards and any card still pending.
A session in `bypassPermissions` changes none of this: the card is a row in SOKKAN, not the
engine's permission prompt.

## 4. Delegation and secrets

- **Microsoft 365**: one application in the customer's tenant (Entra ID, *Web* platform,
  redirect `https://<instance>/api/connectors/graph/callback`, a client secret), **delegated**
  permissions only — no application permission, no service account reading every mailbox.
  Each person consents (authorization code + PKCE S256, state single use, 10 minutes, bound to
  the person who started it: a consent finished in another person's browser is not redeemed).
  Exchange and SharePoint apply that person's own rights.
- **Logins** (IMAP, CalDAV, WebDAV) are checked against the server before they are stored.
- Storage: `connectors.db` (0600). Refresh tokens, passwords, cached access tokens, the
  application's client secret and the actions' payloads are **encrypted with the vault's data
  key** (Fernet, `secrets_provider`, context `vault`); `secrets rotate --data-keys vault`
  re-encrypts them. Nothing a connector reads (mail, event, document) is stored.
- **Revocation**: the person (Settings › Connectors › Revoke), an instance administrator
  (`DELETE /api/connectors/accounts/{email}/{provider}`), or turning a provider off (every
  connection to it is dropped). For Microsoft 365 the person may also remove the consent in
  their account (myapps.microsoft.com).

## 5. Classification: where content may go

The content of a connector enters a session of a **project** and goes to that session's model.

| Project level | Model in `CH` or `local` | Model elsewhere (`EU`, `US`, unknown) |
|---|---|---|
| public, team, project | yes | yes (lower with `SOKKAN_CONNECTORS_MAX_LEVEL_OFFSHORE`) |
| confidential, restricted | yes | **no** — a refusal a person can read, no content |

The zone of a session (`policy.session_zone`): `SOKKAN_CONNECTORS_MODEL_ZONE` if the operator
declares it; a « Write with Claude » work = `US`; an appliance = `local`; else the zone of the
project's engine or the instance default (Connect your AI zones); unknown = not Swiss.
The check runs **before** the provider is called: a refused read fetches nothing.

## 6. What a project may reach

`scopes` (per project, `*` = every project): `shared_mailboxes` (addresses), `sites`
(Graph paths such as `/finance`), `folders` (WebDAV / network folder paths). A person's own
mailbox and calendar are theirs in every project. A shared mailbox not listed is refused; with
`folders` listed, nothing outside them is listed, found, read or written; the network folder
needs at least one folder opened to the project.

## 7. Settings

Settings › Connectors (instance administrators): providers on/off, sending allowed, Microsoft
365 tenant, client id and secret (never shown back). Servers, shared mailboxes and folders per
project through the API:

```http
PUT /api/connectors/settings
{"providers": {"imap": {"enabled": true, "imap_host": "mail.example.ch", "imap_port": 993,
                        "smtp_host": "mail.example.ch", "smtp_port": 465, "security": "ssl"},
               "caldav": {"enabled": true, "url": "https://dav.example.ch/{login}/calendar/"},
               "webdav": {"enabled": true, "url": "https://cloud.example.ch/remote.php/dav/files/{login}"},
               "folder": {"enabled": true, "root": "/mnt/partage", "write_root": "/mnt/partage-livrables"},
               "graph": {"enabled": true, "tenant": "<tenant id>", "client_id": "<app id>"}},
 "graph_client_secret": "…", "allow_send": true,
 "graph_sites": {"finance": "<site id>"},
 "scopes": {"default": {"shared_mailboxes": ["info@example.ch"], "folders": ["/Qualite"]}}}
```

Variables: `SOKKAN_FEATURE_CONNECTORS`, `SOKKAN_CONNECTORS_DEMO` (=1 turns the demo provider on),
`SOKKAN_CONNECTORS_MODEL_ZONE`, `SOKKAN_CONNECTORS_MAX_LEVEL_OFFSHORE`,
`SOKKAN_CONNECTORS_GRAPH_URL` / `SOKKAN_CONNECTORS_LOGIN_URL` (sovereign clouds, tests),
`SOKKAN_CONNECTORS_ALLOW_PLAIN` (tests on a private network only), `SOKKAN_CONNECTORS_DB`.

Each person: Settings › Connectors › connect (Microsoft sign-in, or a login), revoke, decide cards.

## 8. Engines (harness contract)

The server is declared in `agentchat.MCP_SERVERS` and handed to a session only when the feature
is on, with the session's identity in its environment. Tool names, for both engines, come from
`connectors/tools.py`: `mcp__sokkan-connectors__<tool>` (legacy engine) and
`sokkan-connectors__<tool>` (kernel). `tools.read_tools()` = the tools that need no engine
permission (reads and proposals — a proposal only stores a card); `mail_draft` keeps the
engine's permission. The legacy engine takes them in `SAFE_TOOLS`; the kernel adapter takes
`read_tools("kernel")` when its `read_tools` lands (kernel host-3).

## 9. Indexing shared documents (doc_ingest)

Reading on demand (this feature) and the curated memory (`doc_ingest`) are separate:
SharePoint / OneDrive are mirrored by rclone, an SMB share is a `folder` source on a read-only
mount (DOC-INGEST.md § 3-4), and this feature adds the source kind **`webdav`**: an allow-listed
WebDAV directory mirrored one way (PROPFIND + GET only), then read by the memory agent with the
source's classification and quarantine.

```json
{"id": "nextcloud-qualite", "kind": "webdav", "allow": ["Qualite"], "classification": "project",
 "options": {"url": "https://cloud.example.ch/remote.php/dav/files/ingest", "user": "ingest",
             "password_secret": "INGEST_WEBDAV_PASSWORD"}}
```

## 10. Demo mode

Provider `demo` (or `SOKKAN_CONNECTORS_DEMO=1`): a mailbox, a calendar and a shared folder of
invented data per person (`$SOKKAN_DATA_DIR/connectors-demo/`). Same tools, same cards;
« sending » moves the draft to Sent, nothing leaves the machine. For trainings, demos, tests.

## 11. Tests

- `tests/test_connectors.py` — demo provider, a **simulated Microsoft Graph and identity
  platform on a real socket** (`tests/fake_graph.py`: PKCE checked, refresh, shared mailbox,
  drafts, send, calendar, getSchedule, OneDrive / SharePoint), a real folder (traversal, symlink
  out of the share, read-only), classification × zone, cards (owner only, once, expiry, changed
  draft, rejection, journal), encryption at rest and key rotation.
- `tests/test_connectors_live.py` via `tests/connectors_live/run.sh <src>` — real protocol
  servers in containers on a private subnet: **Greenmail** (IMAP + SMTP), **Radicale** (CalDAV),
  **WsgiDAV** (WebDAV), including the `webdav` ingest mirror. Invented users, no real account.

## 12. Limits

- SMB is read through the host's mount (no SMB client in SOKKAN): not tested against a Samba
  container (mounting needs privileges the test runner does not have).
- Google Workspace (Gmail, Calendar, Drive) is not a provider yet (IMAP/CalDAV work with an app
  password; Drive through doc_ingest's rclone).
- Attachments are listed, not read or attached; a draft carries text only.
- Cards are decided in Settings › Connectors; not yet in the work's thread nor in Teams.
- The kernel reaches the server through its stdio mode (`SOKKAN_KERNEL_MCP_STDIO=1`) like the
  other SOKKAN servers; its `read_tools` contract is pending (kernel host-3).
- Real Microsoft 365 / mail providers: validated against simulators only.
