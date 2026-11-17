# The coworker knows the house (4.0)

Three pieces that make every work of SOKKAN Atelier start from what the organisation already
knows: its profile, the project's validated work, and what the people chose to remember.

## 1. The organisation's profile

Filled once by an administrator in **Settings › Organisation** (or by the « organisation » step
of the first launch, which calls the same routes). Stored in `<data dir>/orgprofile.json`
(0600); the organisation's name stays in `instance.json` (`org_name`), its one place.

| Field | Checked | In a work's context |
|---|---|---|
| `name`, `legal_name`, `legal_form`, `sector` | length | always |
| `street`, `postcode`, `city`, `country` (ISO-2, default `CH`) | country code | always |
| `uid` (CHE-123.456.789), `vat` | UID check digit (mod 11) | always |
| `iban`, `bank` | ISO 13616 (mod 97), 21 characters for CH/LI | **only** when the work is about a payment (invoice, reminder, offer, order — the request's words or a `finance.*`/`sales.*` template) |
| `signatories[]` (`name`, `function`) | ≤ 6 | always |
| `people[]` (`initials`, `name`, `function`, `email`, `team`) | ≤ 40, initials derived when empty | always — « initials or a first name mean these people, never another first name » |
| `lang`, `address_form` (`vous`/`tu`), `formats` (`ch`/`fr`/`de`/`en`), `tone` | enumerations | always; `CH` adds « nLPD, not the GDPR; CO; never an article number not read » |
| logo (PNG, JPG, SVG ≤ 512 KB, SVG without script) | magic bytes | never in a prompt; served at `/api/org/logo` for documents and screens |

The person a work is for is named in the block (« You are working with: Sophie Chevalley,
Assistante de direction ») when the directory knows them (by e-mail, else by name).

Routes (`backend/orgprofile_api.py`):

    GET    /api/org/profile            any member; an administrator sees the IBAN whole
    PUT    /api/org/profile            administrator; 400 {code: iban_invalid | uid_invalid | country_iso2 | …}
    GET    /api/org/profile/status     {filled, missing, ready} — the onboarding step shows it
    GET    /api/org/profile/preview    administrator: the exact block a work receives (?ask=…&playbook=…)
    GET|PUT|DELETE /api/org/logo       PUT takes the file as the body, ?name=logo.png

A field absent from a `PUT` stays as it is; `""` empties it; the masked IBAN sent back
unchanged keeps the stored one. The journal (`org.profile.update`) names the fields changed,
never their values.

## 2. Continuity between the works of a project

When an Atelier work starts, `backend/househead.py` lists the project's deliverables whose
CURRENT content was validated by a person (a validation the file no longer matches does not
count), newest first: title, who validated it and when, its section titles, its first lines,
and the path of the full text. Bounds: 6 deliverables, 180 days, 320 characters of summary,
4 200 characters for the whole list (≈ 1 000 tokens). Filter: a deliverable is listed only if
the person may read its level in the project (the highest level of the documents its work read
and of what its session obtained) and, for a work written with Claude, at most the level Claude
may receive. The rule given with it: read the one about the same subject first; when the
request contradicts it (an invoice it shows as paid), say it and put it under « Points to
confirm ».

## 3. Delivery: the run's head

The cockpit computes the two blocks once per run and hands them in `RunSpec.head`
(`backend/harness/contract.py`):

- **legacy engine** (Claude Code CLI): the system prompt (`--system-prompt`, empty otherwise) —
  first in the prefix the model caches, never a message of the conversation;
- **kernel**: the host contract's `project` block. The kernel's host API has no head block yet,
  so the adapter puts it in front of the FIRST turn of a thread (inside `<sokkan-brief>`, so no
  thread shows it), once; a resumed thread already has it; `history()` gives the prompt back
  without it.

Both engines announce it with a `context.injected` event (`channel: head`).

## 4. « Remember for next time? »

Under a deliverable the person just validated (`backend/learn.py`, panel `LearnOffer.tsx`):

    GET  /api/simple/sessions/{sid}/deliverables/{name}/learn   {state: offer|none|retained|dismissed|not_validated, facts}
    POST /api/simple/sessions/{sid}/deliverables/{name}/learn   {facts: [{text, kind}]} or {dismiss: true}

Facts offered (3 at most): the person's own corrections in the work (their later messages that
correct or set a rule — never a sentence of the model), the template the work followed (an
attached document named template / modèle / charte…), the structure validated (its section
titles) when no template was attached. The person reads each one exactly as it will be saved,
may rewrite or untick it, and clicks once. Each fact is written to the project's quarantine
with its provenance (`via: atelier.learn`, session, deliverable, sha256) and approved by that
same person in the same request: same journal (`memory.quarantine.approve`, then
`memory.learn`), the note's level = the deliverable's, the person's clearance checked, a memory
that could not index it refused (409) before anything is written. Only the person who drives
the work (or a project admin) can do it.

## One name

The organisation is named in ONE place, `orgprofile.set_name` (`instance.json`; the common project
follows unless renamed): Settings › Organisation, the first-launch step « Organisation »
(`PUT /api/org/profile {name}`, plus the logo and the profile's completeness) and the cockpit's
`POST /api/instance` all go through it. `PUT /api/onboarding/org` only sets the team's language.
