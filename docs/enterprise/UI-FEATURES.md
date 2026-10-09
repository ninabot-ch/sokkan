# Sharing, BYOK, Connect your AI

Three cockpit features of 3.2. Each one is a switch of the feature registry
(`backend/features.py`, [FEATURES.md](FEATURES.md)): `SOKKAN_FEATURE_<ID>=1|0`, on by default
in the enterprise edition, off in community. Off = the routes answer 404 and the UI hides them.

| Feature | Id | Requires | Where |
|---|---|---|---|
| Shared session / preview for review | `shared_review` | `preview`, `multi_project` | ⇪ share (session pane, Preview), rail « Shared with me » |
| Model keys (BYOK admin, lot 7) | `byok_admin` | `multi_project` | Setup › Engines (instance admins; 3.2.2: no separate screen, the key sits on its engine card — the API stays) |
| Connect your AI | `connect_ai` | — | Setup › Engines (replaces Model), Crew card engine |

## 1. Shared review — `shared_review`

Preview is the place of validation: the person who built something shares the session, or the
captured preview, with the person who must validate it.

```
owner (dev+) ── ⇪ share ──► person or team OF THE PROJECT ── read | write ── [until …]
                               │
                               ├─ rail: « Shared with me » · shared by owner · 👁 read / ✎ can reply
                               ├─ write + « ask X to validate » → the session's tool approvals
                               │     (HITL) are delegated: ✋ N to validate → allow / deny
                               └─ revoke (owner or project maintainer) — everything in the journal
```

Rules (enforced server side, `backend/sharing.py`):

- **A share never widens access.** Recipients must already have a role in the object's project;
  a non-member cannot be a recipient and gets the not-found answers on every route.
- **A viewer never gets write.** Refused at creation; at use time, write is capped by the
  CURRENT project role (demoted to viewer = read; left the project = nothing).
- **Delegated approval** needs a write share of a session. The owner keeps the hand.
- **Previews** are served by the `default` project (no preview elsewhere before the sandbox). A
  preview share shows a screenshot of the shared URL only (`GET /api/shares/{id}/shot`), even to
  a viewer — never another URL.
- **Journal**: `share.create`, `share.view`, `share.approver`, `share.hitl.allow|deny`,
  `share.revoke`.

API: `POST /api/shares`, `GET /api/shares?kind=&target=`, `GET /api/shares/people`,
`GET /api/shares/inbox`, `GET|DELETE /api/shares/{id}`, `POST /api/shares/{id}/approver`,
`POST /api/shares/{id}/decide`, `GET /api/shares/{id}/shot`. Store: `$SOKKAN_DATA_DIR/shares.db`.

## 2. Model keys — `byok_admin`

```
admin ── key ──► modelkeys.json (Fernet, vault key, 0600) ──► llm.json {"key_ref": "instance:anthropic"}
                     │                                            └─► sessions: ANTHROPIC_API_KEY
                     ├─ UI: …abcd · date · who (never the key again)
                     ├─ test (optional): one GET of the provider's model list → valid / rejected (HTTP 401)
                     └─ SOKKAN gateway configured? PUT {gateway}/admin/tenant/{client}/byok  (delete → DELETE …/byok/anthropic)
```

- Instance admins only. Scope `instance`; `project:<slug>` exists in the API and is refused
  until BYOK per project ships (decision of 07.10).
- The key is never written to a log, the journal or an API answer. A test reports ok or the HTTP
  status only.
- Gateway push: `SOKKAN_GATEWAY_URL` (falls back to `SOKKAN_INFER_BASE_URL`),
  `SOKKAN_GATEWAY_ADMIN_TOKEN` (Bearer admin of the gateway), `SOKKAN_GATEWAY_CLIENT` (tenant).
  The gateway refuses a BYOK key without `SOKKAN_INFER_SECRETS_KEY` (503) — shown as a push error.
- An instance in managed inference (operated by NINABOT) keeps its gateway for sessions.

## 3. Connect your AI — `connect_ai`

Cards: SOKKAN Router, Claude (API key or login token), OpenAI / Codex, Gemini, OpenRouter,
Ollama / local, Magnitude (your GPUs). Sessions run the Claude Code engine: an engine drives them
natively (Claude) or through a base URL that speaks the Anthropic Messages API (the provider's
own door, or a proxy such as LiteLLM).

| Mode | When | What people see |
|---|---|---|
| **personal** | community default | every engine; SOKKAN Router preselected, « welcome credit » link = `SOKKAN_ROUTER_WELCOME_URL` (no amount in the code; hidden when unset) |
| **governed** | enterprise default | only what the admin allowed: engines × zones (CH, EU, US, local) × SOKKAN tiers; a project maintainer picks the project's engine when the policy allows it |

`SOKKAN_CONNECT_AI_MODE=personal|governed` forces a mode. Sessions speak the Anthropic Messages
API, so the SOKKAN Router card points at the Anthropic door of SOKKAN Inference:
`SOKKAN_ROUTER_URL` (default `https://infer.sokkan.ch`) and `SOKKAN_ROUTER_MODEL` (default
`sokkan-ship`, used when the admin connects the card without a model; Ship escalates to Deep by
difficulty) override them. The key is a SOKKAN inference key (`sik_…`); its test calls
`/usage` with the key (401 = rejected). Before 3.4.4 the card pointed at `router.sokkan.ch`, which
only speaks the OpenAI API: every session on it failed.

- **Setup › Engines (3.2.2)** = « Connect your AI » and « Model keys » merged on one page. For the
  admin, each engine card carries the instance key of its provider — « key …xxxx, set by X on
  date » with **Replace / Remove / Test** — and, in governed mode, the policy of allowed
  engines. It is ONE store: a key posed through an engine (`PUT /api/connect-ai/engines/{id}`)
  is the record `GET /api/admin/model-keys` lists, and the other way round; an Anthropic key
  posed through the Claude card is pushed to the gateway like one posed in Model keys.
  `POST /api/connect-ai/engines/{id}/test` and `DELETE /api/connect-ai/engines/{id}/key`
  (admin; removing the key also disconnects the engines that used it and clears the
  sessions' reference). The `/api/admin/model-keys` routes stay (scripts, `connect_ai` off).
  A non-admin sees neither the key nor its last 4 characters.
- Connecting an engine (admins) stores its key in Model keys' encrypted store; « use for
  sessions » writes the llm.json reference. Login mode shows: *check your provider's terms* —
  SOKKAN makes no promise about consumer plans.
- **Crew**: a connected engine is selectable as a card's model (`engine:<id>`). A run on an
  engine that was disconnected or disallowed falls back to the instance default, never to a
  stale key. A session of a project with a chosen engine runs on it.
- Journal: `connect_ai.connect|disconnect|default|policy|project`.

## 4. Captains demo — `demo_captains` (3.2.2, public demo only)

What 3.2 « Captains » brings, shown to the anonymous visitor of the public read-only demo
(everyone there is one viewer identity, `SOKKAN_OWNER_EMAIL`). Requires `demo_banner` and
`multi_project`; `SOKKAN_DEMO_CAPTAINS=1` anywhere else is ignored (the feature stays off).

| The visitor sees | Read-only how |
|---|---|
| **Control › Helm** — project cards of the 2-3 demo projects, their hierarchy and computed progress, one reframe suggestion | `/api/helm/access` answers `read_only: true`, `helm.can_view` opens the READ routes to project members; actions greyed « read-only demo » |
| **Control › Board** and the **project selector** | the visitor has a viewer grant on each demo project |
| **« Shared with me »** — one read share from a fictional colleague | a normal share (`shared_review`), read access |
| **Setup › Engines** | engines allowed by the instance; no key tail, no « connected by », no base URL for a non-admin |
| **Setup › Organization** — Members, Projects & teams | `GET /api/demo/organization`: fictional people (`@example.com`) only — any other email is never served |

**Nothing is written by a visitor**: with the feature on, every non-read request under `/api`
from someone below instance admin answers **403 « read-only demo »** in the auth middleware,
before any route — Nina's chat excepted (`/api/assistant/`, capped per day by
`SOKKAN_ASSISTANT_DAILY_LIMIT`). The demo's operator (an instance admin) is not affected.

**Seed**: `python3 backend/demo_captains.py seed <captains.json>` (refused unless
`SOKKAN_DEMO_CAPTAINS=1` or `--force`; `check` validates without writing). People must be
`@example.com` with role viewer or dev (never admin); 2 or 3 projects; teams are local
(`local:demo-<id>`); each project's grants are exactly the seed's (its teams + the visitor
as viewer); cards are remembered in `board.db` table `demo_captains` and rewritten on each
run (idempotent); a project with the same slug not made by the seed is refused. The agents
seeded by `demo_crew` and every other card are left alone. Example: `tests/fixtures/demo_captains.json`.
