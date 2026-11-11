# Simple profile and document drop

Two features of the registry (`backend/features.py`, generated table in [FEATURES.md](FEATURES.md)),
both **beta**, both **off by default** in every edition:

| Feature | Switch | Requires | What it adds |
|---|---|---|---|
| `documents` | `SOKKAN_FEATURE_DOCUMENTS=1` | — | People attach PDF, docx, pptx, xlsx, txt, md files to a project or a session. |
| `simple_profile` | `SOKKAN_FEATURE_SIMPLE_PROFILE=1` | `documents` | A second way into the same cockpit for people who are not developers. |

It is the **same app**: no second front end, no fork. A person on the technical profile sees every
screen exactly as before; nothing of the simple profile's styles or language reaches those screens.

## Who sees what

The profile is a **per-person setting** (`simple` / `technical`), stored in
`$SOKKAN_DATA_DIR/uiprefs.json`:

* a person changes it in *Setup › My account › Interface profile* (technical → simple) and in the simple
  profile's *Réglages* (simple → technical);
* `SOKKAN_PROFILE_DEFAULT=simple` makes it the profile of everybody who has not chosen (default:
  `technical` — turning the feature on changes nothing until someone chooses);
* `SOKKAN_PROFILE_LOCKED=1`: only an instance admin changes somebody's profile
  (`PUT /api/admin/prefs/{email}` `{"profile": "simple"}`) — the setting of a pilot.

A preference is not a right: roles and features still decide what a person may do. The one thing the
profile gates is the **approval mode** of a session, which cannot be changed from the simple profile
(refused by the session WebSocket, whatever the client sends): every action that changes something
waits for the person.

## What a person does in it

1. **Home** — « Bonjour Camille. Sur quoi travaille-t-on ? »: one text area, *Joindre des documents*
   (picker or drag and drop), four task templates (training plan, summary, client proposal, assessment
   quiz — playbooks with `audience: simple` in `backend/playbooks.py`), *Lancer*.
2. **Thread** — what the agent does, in sentences (« L'agent lit "Brief client.docx"… »). No tool name,
   no path, no internal brief; the raw material is under *Détail technique*, folded.
3. **Approval card** — « L'agent veut enregistrer "Plan de formation – v2" », the person's documents the
   agent actually opened with their age, *Autoriser / Voir le brouillon / Refuser*.
4. **Deliverable** — preview, *Télécharger en Word* (or the text), *Valider le livrable*: who, when and
   the SHA-256 of exactly what was validated go to the journal; a later change shows « modified since
   your validation ».

Drawer: Accueil, Mes travaux, Documents, Mon équipe d'agents, the recent works with the agent's last
sentence, today's usage gauge (shown when a daily ceiling exists: gateway quota, project budget or
instance budget), Nina (« your guide to SOKKAN » — when the `assistant` feature is on), Réglages
(language, light / night / as the device, profile).

French and English: two dictionaries (`frontend/lib/simple/dict.ts`), the browser's language unless the
person chose; `<html lang>` follows. The technical profile stays in English.

## Documents: what is accepted, where it goes

| | |
|---|---|
| Types | pdf, docx, pptx, xlsx, txt, md — the extension **and** the content are checked (a renamed executable, a zip called .docx, a macro-enabled package, a PNG called .pdf are refused) |
| Size | `SOKKAN_DOCUMENTS_MAX_MB` (25) per file, enforced while the upload streams; `SOKKAN_DOCUMENTS_MAX_PER_PROJECT` (500) |
| Name | a label only: directory parts, control and reserved characters removed; the file is stored under an id SOKKAN generates |
| Place | `<project space>/documents/<id>/<name>` — the workspace for the default project, `$SOKKAN_DATA_DIR/projects/<slug>/work` otherwise (what the project sandbox lets a session read) — plus a text copy `<name>.txt` the agent reads on any engine |
| Roles | the caller's role **in the document's project**: dev adds, viewer reads, the person who added it or a project admin removes; another project's document answers 404 |
| Classification | a document carries a level; listed and served at or under the reader's clearance; one above the default level cannot be attached to an agent session |
| Journal | `document.upload`, `document.refused` (with the reason), `document.download`, `document.attach`, `document.delete`; `session.spawn`, `deliverable.save`, `deliverable.download`, `deliverable.validate`, `profile.set` |

Deliverables are the Markdown files an agent saves in `<project space>/deliverables/<session id>/`.

`pypdf` (in `backend/requirements.txt`) extracts the text of a PDF; without it a PDF is stored with
`text_state: unavailable` and the agent is pointed at the original file. The text copy is a plain
reading aid, **not** the memory ingestion (structure, chunks, notes), which is separate work.

## API

    GET/PUT /api/me/prefs                      PUT /api/admin/prefs/{email}
    GET /api/simple/home · /api/simple/works   POST /api/simple/start
    GET /api/simple/sessions/{sid}             POST …/message
    GET|POST …/deliverables                    GET …/deliverables/{name}[/download?format=md|docx]
    POST …/deliverables/{name}/validate
    GET|PUT /api/documents                     GET|DELETE /api/documents/{id}
    GET /api/documents/{id}/content | /text    POST /api/documents/{id}/attach
    WS  /api/agent/ws/{sid}?view=simple        the session's events in plain language (backend/plain.py)

## Design tokens

`frontend/app/simple.css` defines the tokens (surfaces, four text levels, lines, accents, two shadows,
radii 10 / 12 / 999, one motion curve) for a light and a dark theme, under `html[data-sk-theme]` — an
attribute that exists only while the simple profile is on screen. `scripts/check-simple-tokens.py`
fails when a rule or a component writes a literal colour or pixel font size, when a text / surface pair
falls under 4.5:1 in either theme, or when the two dictionaries differ.

## Known limits (beta)

* A person's approve / refuse click is not yet a line of the journal (the validation of a deliverable is).
* A classified document cannot go to an agent session (transcripts do not carry a level yet).
* The study-mode band of a pilot has its slot (`components/simple/BannerSlot.tsx`) but is not in this feature.
* Local auth has one identity: several people on the simple profile need SSO.
* The sign-in screen is the cockpit's (English).
