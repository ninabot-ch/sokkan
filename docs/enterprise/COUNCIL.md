# Launch council (experimental)

Feature `council` (off by default: `SOKKAN_FEATURE_COUNCIL=1`). A reusable object for any project
— nothing in it is specific to one product.

The CEO puts a project (text and attached documents) in a **room**. Nine roles speak, each from
its trade; the finance lead (DAF) leads and arbitrates and hands back **one agreement card**.

## The nine roles

`backend/council/roles/*.yaml`, one file each, FR + EN, same shape as the job kits of
`backend/roles/` (`id`, `order`, `i18n.<lang>`) plus `tier`, `avatar` (fixed initials and icon —
never a generated face) and, per language, `system_prompt` and `limits` (« 150 words at most;
every figure is sourced (an attached document, named) or marked “estimated” »).

Order: `daf` (leads, arbitrates, prices) · `code` · `ops` · `conception` (specification, mechanics,
electronics — schematics, PCBs, BOM —, power, sensors, tests) · `achats` · `qa` · `marcom` ·
`juridique` (IP, licences, compliance of a physical product — battery, electrical and optical
safety, CE/LVD/RED, sensor data — nFADP) · `coach` (the AGENTS to recruit: profile, scope, tools,
model tier; the HUMAN roles: profile, % of a post). `SOKKAN_COUNCIL_ROLES_DIR` adds or replaces a
role file (the nine ids must stay).

## The rounds

| State | What happens |
|---|---|
| `brief` | brief and documents; the CEO may write |
| `round1` | each role reacts to the brief — parallel, **2 at a time at most**, shown DAF → coach |
| `round2` | the DAF raises 2 or 3 disagreements; only the roles concerned answer, addressed, **one turn** |
| `synthesis` | the DAF's synthesis, strict JSON (**one retry** if invalid), fact-checked |
| `card` | the agreement card waits for the CEO |
| `accepted` / `refused` | decision; a refusal with a reason makes the DAF write a new synthesis, **twice at most** (the third refusal is final) |
| `stopped` / `stopped_budget` / `failed` | the CEO stopped it / the ceiling was reached / the model failed (reason shown) |

The CEO's messages enter the journal at once and are read by every role that starts a call after
them (a call already in flight is not interrupted). Stop: a live council stops before its next call.

## The card and what acceptance creates

Synthesis keys: `perimetre`, `budget` (`lignes` {poste, montant_chf, source}, `total_chf`),
`risques` {risque, gravite, responsable}, `kanban` {titre, responsable, critere_de_fin},
`agents_a_recruter` {nom, profil, perimetre, outils, palier}, `roles_humains` {profil,
pourcentage_poste, perimetre}, `calendrier` {jalon, echeance, responsable}.

Checks after validation (`backend/factcheck.py` and the council's own): the budget total is
**replaced by the sum of the lines** when it differs; a line whose source is neither an attached
document nor « estimated » is **marked estimated**; both are listed under « To check » on the card.

On **accept**: each kanban item → a **board card in Backlog** (tag `council`, never spawned); each
recruit → a **draft agent** in Crew (`status=draft`, trigger `manual`: it runs only after a person
approves it in Crew, one by one); each human role → an entry of the council's **open positions**
(`GET /api/council/{id}/positions`). No external action.

## Models, Claude and personal data

Through the existing inference layer, direct calls, the project's documents injected as untrusted
data, no tools.

- **Roles**: the machine's tier (`SOKKAN_COUNCIL_SHIP_URL/_MODEL/_KEY`, else the quality gate's
  local reviewer `SOKKAN_REVIEW_LOCAL_*`, else the router's `SOKKAN_SHIP_*`).
- **DAF**: **Deep** by default (`SOKKAN_COUNCIL_DEEP_*`, else `SOKKAN_REVIEW_*`, else
  `SOKKAN_DEEP_*`). Deep leaves the machine, so the review's ceiling applies
  (`SOKKAN_REVIEW_REMOTE_MAX_LEVEL`, default « project »): a more confidential project is answered by
  the local model, and the journal says so (`ship*`).
- **Claude**: only if the CEO ticks it (`claude`, scope `daf` by default or `all`) **and** the
  project is not confidential — rule D9 of `claudetier` (`HARD_MAX`: confidential and restricted
  never, no variable raises it). Checked at opening (`409 claude_level`) **and at every call**: a
  project reclassified meanwhile stops the council (`failed`, `claude_refused`) — never a silent
  fall back. If the brief or documents hold personal data, opening answers `409 claude_pii` (counts
  only) and needs `pii_ack`; the request then leaves through the PII sandwich (`backend/pii.py`:
  stable pseudonyms out, values put back in the answer). Documents cannot be added after opening a
  council that uses Claude.
- The engine is an interface (`council/engine.py`: `Call` → `Reply`): a kernel session per role can
  be plugged in later; `FakeEngine` serves the tests.

## Budget per council

Defaults: **400 000 tokens** and **CHF 5.00** (`SOKKAN_COUNCIL_MAX_TOKENS`,
`SOKKAN_COUNCIL_MAX_COST_CHF`; per council at opening). A typical council is 15 calls
(9 + 1 + 4 + 1) of 6–12 k tokens. Cost per million tokens (CHF) of the non-Claude tiers is an
estimate, `SOKKAN_COUNCIL_PRICES='{"ship":[0.2,0.8],"deep":[1.0,4.0]}'`; Claude uses the price table
and `SOKKAN_FX_USD_PER_CHF`. Tokens and cost are shown live. At the ceiling the council **stops**
(`stopped_budget`, a clear message, journal kept); the calls already in flight finish (at most 2),
so the final figures can exceed the ceiling by those calls.

## Journal and export

Every speech: timestamp, role, model, route, tokens in/out, duration, cost. Read-only replay in the
room; `GET /api/council/{id}/export?format=json|md`. The pseudonym table never leaves the machine.

## Routes (`/api/council`)

`GET config` · `GET` (list) · `POST` (open) · `GET {id}?after=` · `POST {id}/documents` · `POST
{id}/start` · `POST {id}/say` · `POST {id}/stop` · `POST {id}/accept` · `POST {id}/refuse` · `GET
{id}/positions` · `GET {id}/export`. Viewer reads, dev writes; only who opened the council (or a
project admin) starts, speaks, stops and decides. Audit actions `council.*`.

## UI

« Council room » page in the Atelier (nav) and Build › Council in the cockpit, FR/EN through the
Atelier dictionary: nine fixed tiles with a written « speaking » indicator (colour is never the only
signal, the pulse stops under `prefers-reduced-motion`), the discussion by round, the CEO's input,
attached documents, the budget meter and the card.
