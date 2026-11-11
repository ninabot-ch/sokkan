# Document ingestion — the memory agent that reads the organisation's documents

Feature `doc_ingest` · status **experimental** · **off by default** in both editions ·
requires `agents` (its scheduler) and `memory_quarantine` · code `backend/docingest/` ·
API `/api/doc-ingest/*` · operator CLI `python -m docingest`.

> SOKKAN sells a **curated memory**, not a search engine over every file. The agent reads
> documents and keeps what someone will need in six months: one sourced reference note per
> document worth keeping, one note per **decision** it records, tags, links to neighbouring
> notes, a classification. It **abstains** on the rest, and says so in a journal. Nothing it
> writes reaches the memory before a person — or a trust policy that a person earned —
> approves it.

Contents: [1 Architecture](#1-architecture) · [2 What leaves the machine](#2-what-leaves-the-machine-nothing)
· [3 Sources](#3-sources) · [4 Microsoft 365 and Google Drive](#4-microsoft-365-and-google-drive-rclone)
· [5 Extraction](#5-extraction) · [6 The notes](#6-the-notes) · [7 Guardrails](#7-guardrails-and-the-test-of-each)
· [8 Review by batch](#8-review-by-batch) · [9 Passes](#9-passes-scheduling-resume-budget-journal)
· [10 Settings](#10-settings) · [11 Operating it](#11-operating-it) · [12 Measures](#12-measures-on-the-gold-set)
· [13 Limits](#13-limits-and-what-is-not-done)

## 1. Architecture

One pass over one source, document by document:

| Step | Module | What happens | What cannot happen |
|---|---|---|---|
| 1. List | `sources.py` | The source lists files under its **allow-list**; an rclone source first mirrors the allowed remote directories one way. | A path outside the allow-list is never listed, hashed or opened. |
| 2. Changed? | `runner.py`, `state.py` | Same size and date → skipped. Else SHA-256; same hash → skipped. | An unchanged document is never sent to the model twice. |
| 3. Extract | `extract.py`, `extract_worker.py` | A **child process** turns the file into text, headings, metadata (MarkItDown; Docling for PDFs without a text layer). | A parser that hangs, crashes or eats memory is killed; the file is consigned as an error, the pass goes on. |
| 4. Same text? | `state.py` | Same extracted text as a file already read → `duplicate`, no model call. | — |
| 5. Neighbours | `state.py` | The closest existing notes (the agent's own lexical index + the project memory when the 3.0 store serves). Notes of other sources and of the memory: **never above the source's level**. Notes of the same source that the model itself raised: shown, and the new notes **inherit the highest level shown**. | A note of another source above this folder's classification is never shown to the model; a derived note is never below what it was derived from. |
| 6. Read | `agent.py`, `llmclient.py` | The **local** model answers one JSON object (strict schema, no tool, reasoning off). | The endpoint must be on the machine or its private network. |
| 7. Validate | `agent.py` | Code re-checks every field against the document (§ 7) and renders CortHeXis markdown. | The model cannot choose a file name, a frontmatter key, a lower classification, a link target that was not offered. |
| 8. Quarantine | `batches.py` | Notes are written under `memory-quarantine/batches/<id>/` and indexed by nothing. | Nothing is recalled, by anyone, before approval. |
| 9. Review | `api.py`, `batches.py` | A person samples the batch, approves or rejects it, or decides note by note; a trusted source approves itself (§ 8). Every decision is journaled. | A flagged note is never approved in bulk. |

State lives in `$SOKKAN_DATA_DIR/doc-ingest/`: `sources.json` (the allow-list),
`docingest.db` (ledger: files, passes, batches, decisions — **metadata and the agent's own
notes only, never the text of a document**), `mirror/<source>/` (rclone mirrors),
`rclone.conf`.

Why a pipeline and not a Crew agent: a Crew run is a Claude Code session with tools. Here
the model must have **no tool at all** — the only thing the reader of an untrusted document
can do is describe it. The scheduling, however, is the Crew scheduler's (§ 9): one
scheduler on the instance.

## 2. What leaves the machine: nothing

| Flow | Direction | Content | Guard |
|---|---|---|---|
| Source folder / NAS | read | files | mount `ro`; `require_readonly`; the package opens source files in `rb` only |
| SharePoint / OneDrive / Drive | **download only** | allow-listed directories | `rclone sync <remote>:<dir> <local mirror>` built in one place (`sources.rclone_sync_command`); the tenant application is read-only (§ 4) |
| Model | local HTTP | system prompt + one document + neighbour excerpts | `llmclient.check_local`: loopback, RFC 1918, link-local or 100.64.0.0/10 only — otherwise the pass refuses to start. `SOKKAN_DOC_INGEST_ALLOW_REMOTE_LLM=1` is the explicit, operator-level exception; **do not set it on a customer site**. |
| Extractors | none | — | MarkItDown is only called through `convert_local` (a path is never fetched as a URL); plugins off; the child runs with `HF_HUB_OFFLINE=1` |
| Telemetry | none | — | — |

The embedding of approved notes follows the memory's own configuration (CortHeXis profile).
On an Anchor that is the local embedding service; this feature adds no call of its own.

One-time, at install, **inbound** only: `pip install` of the extractors, and Docling's
models if Docling is used (`docling-tools models download`). On an air-gapped site, ship
them in the image.

## 3. Sources

The allow-list is one file on the host, `sources.json` (`SOKKAN_DOC_INGEST_CONFIG`). It is
**not editable from the cockpit**: deciding what the agent may read is an act of the
machine's operator. A missing file means no source; an invalid file is an error — never
« read everything ».

```json
{"sources": [
  {"id": "nas-formation", "kind": "folder", "root": "/sources/documents",
   "allow": ["Cours", "Comites/2026", "Qualite/**/*.pdf"],
   "deny": ["**/Brouillons/**"],
   "classification": "project", "project": "default",
   "require_readonly": true,
   "schedule": "30 1 * * *", "timezone": "Europe/Zurich",
   "trust_after": 3, "trust_max_level": "project",
   "limits": {"concurrency": 8, "max_tokens_per_pass": 3000000}},
  {"id": "sharepoint-direction", "kind": "rclone", "remote": "m365",
   "allow": ["Direction/PV", "Direction/Contrats"],
   "classification": "confidential", "schedule": "0 2 * * *"}
]}
```

| Key | Meaning |
|---|---|
| `id` | lowercase kebab-case; names the mirror, the ledger rows, the provenance of notes |
| `kind` | `folder` · `rclone` · any kind added with `sources.register()` |
| `allow` | **required, non-empty**. Paths relative to the root. `Cours` = everything under it; `*` stays inside one directory, `**` crosses them. No leading `/`, no `..`. For `rclone`: remote directories, no wildcard. |
| `deny` | added to the built-in deny list (`~$*` Office lock files, dot-files, `Thumbs.db`, `*.tmp`) |
| `classification` | the **floor** of every note derived from this source (`public` < `team` < `project` < `confidential` < `restricted`; unknown value = `restricted`). The model may raise it, nothing can lower it. |
| `project` | the SOKKAN project whose memory receives the notes |
| `require_readonly` | `folder`: refuse to run unless the mount is read-only (`statvfs`) — set it for every NAS |
| `schedule`, `timezone` | cron of the incremental pass; empty = manual only |
| `trust_after`, `trust_max_level` | § 8 |
| `extensions` | default `.pdf .docx .pptx .xlsx .md .txt .csv .html .htm .rtf .odt`; other files are listed as `skipped`, never opened |
| `limits` | per-source override of the limits of § 10 |
| `options.flags` | `rclone`: extra read-side flags from a fixed list (`--tpslimit`, `--drive-shared-with-me`, `--drive-team-drive`, `--onedrive-drive-id`, …); anything else is refused |

**Folder / NAS.** The host mounts the share read-only and hands it to the api container
read-only (`docker-compose.doc-ingest.yml`):

```
//nas.example.lan/formation  /srv/sokkan-sources/formation  cifs  ro,credentials=/etc/sokkan/nas.cred,uid=1000,iocharset=utf8,vers=3.0  0 0
```

Use a NAS account that only has read permission: `ro` protects against us, the account's
rights protect against everything else. Symbolic links are followed only when the real file
is still inside the root.

**Incremental state** per (source, path): size, modification time, SHA-256 of the bytes,
SHA-256 of the extracted text, outcome, notes produced, tokens. A file that disappears is
marked `gone` (its notes stay: history is memory; the journal says so). A source that
suddenly lists **nothing** fails the pass instead of marking everything gone (a fallen
mount is not an emptied archive).

**Another connector** implements `prepare()` and `iter_files()` and is registered with
`sources.register("kind", Class)`. The runner filters its listing through the same
allow-list: a connector cannot widen it.

**A source is never SOKKAN's own space.** A `folder` whose `root` is inside — or contains — the
data directory, the workspace (where the documents people drop and the deliverables live), the
memory or its quarantine is a configuration error. The documents a person drops in a session
(feature `documents`) keep their own level and never become a batch; what a session learnt from
them reaches the memory only through the unit quarantine (`memory_write` of a session that was
handed documents is quarantined, provenance `reason: session_documents`).

## 4. Microsoft 365 and Google Drive (rclone)

SOKKAN never talks to Graph or Drive itself for documents: `rclone` (MIT) mirrors the
allow-listed directories into `doc-ingest/mirror/<source>/`, then the mirror is read like a
folder. The sync is incremental (size + modification time), one way, restricted to the
extensions we read and to `max_file_mb`.

> **Not validated on a real tenant.** The orchestration is tested against the real `rclone`
> binary (1.73.5) with a local remote, and against a stand-in. The registration steps below
> follow Microsoft's and Google's documented models and rclone's options as printed by
> `rclone help backend onedrive|drive`; they must be played once on the customer's tenant
> before the pilot.

### 4.1 SharePoint (Microsoft 365) — application registered in the customer's tenant

The customer's administrator does this, in **their** tenant; SOKKAN's publisher holds no
credential and no multi-tenant application.

1. **Entra admin center → App registrations → New registration.** Name
   « SOKKAN document reader », *Accounts in this organizational directory only*
   (single tenant). No redirect URI.
2. **API permissions → Add → Microsoft Graph → Application permissions →
   `Sites.Selected`** — and nothing else. Remove the default `User.Read`. *Grant admin
   consent.* `Sites.Selected` alone gives access to **no site**.
3. **Give the application `read` on each chosen site**, one by one (a SharePoint
   administrator; with PnP PowerShell:
   `Grant-PnPAzureADAppSitePermission -AppId <client id> -DisplayName "SOKKAN document reader" -Site https://<tenant>.sharepoint.com/sites/<site> -Permissions Read`,
   or `POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions` with
   `{"roles": ["read"], "grantedToIdentities": [{"application": {"id": "<client id>", "displayName": "SOKKAN document reader"}}]}`).
   Never `write`.
4. **Certificates & secrets → New client secret** (or a certificate). Note its expiry in
   the customer's calendar: an expired secret stops the nightly sync, visibly (the pass is
   `failed`, § 11).
5. On the Anchor, `rclone.conf` (`SOKKAN_DOC_INGEST_RCLONE_CONFIG`, mode `0600`, owned by
   the api's user):

```ini
[m365]
type = onedrive
client_id = <application (client) id>
client_secret = <secret>
tenant = <directory (tenant) id>
client_credentials = true
drive_type = documentLibrary
drive_id = <id of the site's document library>
```

   `drive_id`: `GET https://graph.microsoft.com/v1.0/sites/{site-id}/drives` with the
   application's token. One remote per document library.
6. `python -m docingest check <source>` lists what would be read, without opening anything.

Notes. `client_credentials` and `tenant` exist in rclone 1.73.5 (checked); the 1.60
packaged by Debian/Ubuntu — the one `docker/api.Dockerfile` installs — predates them: for
this mode, provide a current rclone and point `SOKKAN_DOC_INGEST_RCLONE` at it (see § 13).
Personal OneDrive folders are **not** covered by `Sites.Selected`; reading them needs the
tenant-wide `Files.Read.All`, which we do not recommend: keep to SharePoint sites and Teams
libraries. The client secret is a secret of the customer on the customer's machine; it is
not in SOKKAN's vault yet (§ 13).

### 4.2 Google Drive — a service account that is only a viewer

1. Google Cloud console, a project of the customer: enable the **Drive API**, create a
   **service account**, download its JSON key. No domain-wide delegation.
2. Share each folder or shared drive to read **with the service account's address as
   Viewer**. It sees nothing else.
3. `rclone.conf`:

```ini
[gdrive]
type = drive
scope = drive.readonly
service_account_file = /data/doc-ingest/gdrive-sa.json
team_drive = <shared drive id>
```

Google Docs / Sheets / Slides are exported by rclone as `docx` / `xlsx` / `pptx` (its
default export formats), then read like Office files.

## 5. Extraction

| Extractor | Licence | Reads | Notes |
|---|---|---|---|
| [MarkItDown](https://github.com/microsoft/markitdown) 0.1.9 | **MIT** (package metadata, checked 2026-11-11) | docx, pptx, xlsx, PDF with a text layer, html, csv, rtf, odt | its dependencies as installed: pdfminer.six MIT, mammoth BSD-2, python-pptx MIT, openpyxl MIT, pandas BSD-3, markdownify MIT, beautifulsoup4 MIT, lxml BSD-3, magika Apache-2.0, onnxruntime MIT, defusedxml PSF |
| [Docling](https://github.com/docling-project/docling) 2.x | **MIT** (PyPI metadata, checked 2026-11-11) | PDFs **without a usable text layer** (scans) or that MarkItDown cannot read | optional, heavy (PyTorch). The licences of the layout / OCR **models** it downloads were **not** reviewed here — check them before shipping them in an image. |
| built-in | — | md, txt, csv | charset detection utf-8 → cp1252 → latin-1 |
| rclone | **MIT** | — | system binary |

Install: `pip install -r backend/requirements-docingest.txt`, or build the image with
`--build-arg SOKKAN_DOC_INGEST=1`. Docling: `pip install "docling>=2.60,<3"` then
`docling-tools models download` once (`SOKKAN_DOC_INGEST_DOCLING=auto|off|always`), and
give the api `DOCLING_ARTIFACTS_PATH=<the downloaded models directory>`: the extraction
child runs with `HF_HUB_OFFLINE=1`, so without that path Docling refuses to start rather
than call out (checked: `LocalEntryNotFoundError … outgoing traffic has been disabled`).

Measured on the reference machine (CPU only, Docling 2.150 + RapidOCR, PyTorch CPU), on the
gold set's scanned page: 20.6 s for the first page (models loading), 3.4 s for the next,
1.3 GB resident in the child, ~3.3 GB on disk (venv 1.9 GB + models 1.4 GB). The default
OCR model reads French accents badly (« fermé » → « fermø », « décembre » → « døcembre »):
dates and figures survive, accented words do not — choose the OCR language model before
relying on scans (§ 13).

What is extracted: the text as markdown (headings, tables, slide and sheet titles), the
list of headings, and metadata — title, author, creation / modification dates from the
Office properties or the PDF info, page count.

Limits and failures — each is a line in the journal, never a stopped pass:

| Case | Outcome |
|---|---|
| bigger than `max_file_mb` (25) | `skipped: too big` — never opened, not even hashed |
| extension not in the list | `skipped: extension not read` |
| Office file that decompresses to > 400 MB or > 200× | `skipped: refused` (zip bomb) |
| broken file, parser exception | `error: unreadable` |
| PDF with < 200 characters or < 80 per page, Docling absent or off | `error: needs_ocr` |
| extraction longer than `extract_timeout_s` (180) | child killed, `error: timeout` |
| child's resident memory above `extract_mem_mb` (4096) | child killed, `error: memory` |
| child dies | `error: crash`, a new child takes the next file |
| text longer than `max_doc_chars` (240 000 ≈ 70 k tokens) | first 75 % and last 25 % kept, note flagged `truncated_source` |

An `error` is tried again at the next pass (installing Docling later picks up the scans);
a `skipped` file is not, until it changes. Two extraction children serve all the model
streams; each is recycled every 200 documents.

## 6. The notes

For a document worth keeping the agent writes **one reference note** and **one note per
decision** (at most 8). They are ordinary CortHeXis notes.

```markdown
---
name: decision-plafonnement-groupes-bureautique
description: "Le comité décide de plafonner les groupes à 12 participants pour tous les cours de bureautique dès le 1er septembre 2026."
metadata:
  type: "decision"
  tags: ["taille-groupe", "bureautique"]
  source: "nas-formation"
  source_path: "Comites/pv-comite-pedagogique-2026-03-12.docx"
  source_sha256: "9f2c…"
  document_date: "2026-03-12"
  read_at: "2026-11-11"
  ingested_by: "doc-ingest"
  modified: "2026-03-12"
  modified_source: "frontmatter"
  provenance: "doc-ingest · source nas-formation · batch #4 · 2026-11-11"
  decision_date: "2026-03-12"
  decided_by: "comité pédagogique"
  approved_by: "claire.dubois@example.org"
---
# Décision — Le comité décide de plafonner les groupes à 12 participants…

**Décision** : Le comité décide de plafonner les groupes à 12 participants pour tous les cours de bureautique dès le 1er septembre 2026.
**Date** : 2026-03-12 · **Décidé par** : comité pédagogique

> le comité décide de plafonner les groupes à 12 participants pour tous les cours de bureautique dès le 1er septembre 2026.

**Voir aussi** : [[comite-pedagogique-mars-2026]]

**Source** : `nas-formation:Comites/pv-comite-pedagogique-2026-03-12.docx` · sha256 `9f2c…` · document du 2026-03-12 · lu le 2026-11-11
Tags : #taille-groupe #bureautique
```

* `metadata.type` is `reference` or **`decision`**. `classification:` appears when the level
  is not the default. `metadata.modified` is the **document's** date: a 2022 procedure does
  not look written today in the recall.
* The reference note links to its decisions, to the neighbours the model chose **among the
  notes it was shown**, and carries a visible block for each **contradiction** with an
  existing note (what this document says, what the note says, which is newer).
* A new version of a document keeps the **name** of its reference note (it replaces it at
  approval, flagged `updates_existing`); the decisions of the previous version that the
  new one no longer states are linked and flagged `previous_version_notes` — nothing is
  deleted silently.

**No change to the engine.** `memory/core` (kept identical to the public CortHeXis by
`scripts/check-corthexis-sync.sh`) is untouched: its parser already takes any
`metadata.type` and keeps unknown `metadata` keys, and its normaliser leaves them alone
(`tests/test_docingest_agent.py::test_rendered_notes_are_corthexis_notes_the_engine_reads`).
What the engine does **not** do with them yet — proposed for CortHeXis 2.2, to be ported on
both sides: (1) `decision` as a fifth documented type in `SCHEMA.md` and in the review's
« no type » message; (2) `metadata.tags` indexed as a filter (today the tags are also
written in the body, so the lexical search finds them); (3) a review control « a
`decision` note without `source_*` or `decision_date` »; (4) a recall filter by type.

## 7. Guardrails, and the test of each

The text of a document is **data**. The prompt says so and delimits it with a random key —
but the design does not rely on the model obeying: a test runs a model that does
*everything* a trapped document asks, and checks the result is still harmless.

| Guardrail | Where | Test (fails if the guard is removed) |
|---|---|---|
| Nothing outside the allow-list is read | `sources.allowed`, `runner._plan` | `test_docingest_sources.py::test_only_allow_listed_paths_are_listed`, `…a_new_connector…cannot_widen…` |
| A symlink cannot leave the root | `_TreeSource.iter_files` | `…::test_a_symlink_cannot_lead_out_of_the_root` |
| An invalid / empty allow-list is an error | `config.parse_source` | `…::test_an_invalid_source_is_refused_never_widened` |
| The source is never modified | `rb` only | `…::test_a_pass_never_modifies_the_source`, `test_docingest_scheduler.py::test_nothing_in_the_package_can_write_to_a_source_or_call_out` |
| rclone goes one way, into the mirror, for allow-listed directories | `rclone_sync_command` | `…::test_rclone_command_is_one_way…`, `…refuses_what_is_not_in_the_allow_list`, `…mirror_is_incremental_one_way_and_allow_listed` |
| The documents go to a local model only | `llmclient.check_local` | `test_docingest_scheduler.py::test_only_a_local_endpoint_gets_the_documents` |
| The model has no tool; strict JSON; reasoning off | `llmclient.Client.complete` | `…::test_the_client_asks_for_strict_json…` |
| Classification never below the source floor | `agent.validate` | `test_docingest_agent.py::test_classification_is_never_below_the_source_floor` |
| A neighbour of another source above the source level is never shown | `state.neighbours`, `runner._process` | `test_docingest_runner.py::test_a_neighbour_above_the_source_level_is_never_shown` |
| The derived inherits the highest level it was shown | `runner._process` | `…::test_a_raised_note_of_the_same_source_is_shown_and_its_level_is_inherited` |
| A contradiction must rest on what the note was shown to say | `agent.validate` | `test_docingest_agent.py::test_a_contradiction_must_rest_on_what_the_note_was_shown_to_say` |
| A model that stops answering suspends the pass, blames no document | `llmclient.Client.complete`, `runner._safe` | `test_docingest_scheduler.py::test_a_model_that_restarts_is_waited_for…` |
| Links only to notes that were offered | `agent.validate` | `…::test_links_only_to_notes_that_were_offered` |
| A decision whose quote or figures are not in the document is flagged | `agent.validate` | `…::test_a_decision_without_proof_in_the_document_is_flagged` |
| URLs, e-mails not in the document, images, HTML are removed | `agent._clean_text` | `…::test_urls_emails_images_and_html…` |
| A document cannot write frontmatter or choose a file name | `agent._fm`, `slugify`, `batches.unique_name` | `test_docingest_runner.py::test_a_model_that_obeys_a_trapped_document_still_cannot_do_harm` |
| Instructions addressed to the reader flag every note of the document | `agent.find_injection` | `test_docingest_agent.py::test_instructions_addressed_to_the_reader_flag_every_note` |
| Nothing is recalled before approval | `batches.put_note` (outside the memory dir) | `test_docingest_runner.py::test_a_pass_writes_into_a_batch_and_never_into_the_memory` |
| A flagged note is never approved in bulk, nor by the trust policy | `batches.approve_batch` | `test_docingest_review.py::test_a_flagged_note_is_never_approved_in_bulk`, `…trust_after_n…` |
| A batch nobody looked at is not approved | `batches.approve_batch` | `…::test_statistics_then_a_sample_then_the_batch` |
| Above the reviewer's clearance = does not exist | `batches.note`, `notes_of` | `…::test_above_the_reviewers_clearance_does_not_exist` |
| A level above default needs the 3.0 store | `batches._into_memory` | `…::test_a_note_above_the_default_level_needs_the_store…` |
| A note written by a person is never overwritten | `batches.name_free`, `_into_memory` | `…::test_a_note_written_by_a_person_is_never_overwritten` |
| A parser that hangs / dies / eats memory does not stop the pass | `extract.Extractor` | `test_docingest_extract.py::test_a_parser_that_hangs…`, `…dies…`, `…eats_memory…` |
| Zip bombs, oversized files | `extract_worker._zip_guard`, `Extractor.extract` | `…::test_zip_bomb…`, `…::test_limits_big_file_never_opened…` |
| A fallen mount is not an emptied archive | `runner._plan` | `test_docingest_runner.py::test_a_source_that_lists_nothing_is_a_failure…` |
| Token ceiling per pass | `runner._over_budget` | `…::test_token_ceiling_stops_the_pass_and_the_rest_waits` |

Flags a note can carry: `conflict` · `quote_not_found` · `unsupported_figure` ·
`injection_suspected` · `invented_link` · `truncated_source` · `updates_existing` ·
`previous_version_notes` · `level_inherited`.

What the guardrails do **not** catch: a wrong summary that only uses words and figures of
the document; a decision attributed to the wrong body; a document that lies. That is what
the human sample is for (§ 8) and what § 12 measures.

## 8. Review by batch

A batch = one source × one pass. `GET /api/doc-ingest/…` (project-scoped; developer role
to review, maintainer to run passes and export the journal):

| Route | |
|---|---|
| `GET /status` | model readiness, pending batches, minimum sample |
| `GET /sources` | declared sources of the project, trust state, schedule, last pass |
| `POST /sources/{id}/run` · `/stop` | start a pass (`{"kind": "initial"}`) · stop after the documents in progress |
| `GET /passes` · `/passes/{id}` | journal of passes · one pass with one event per document |
| `GET /batches?status=pending` | batches with note counts |
| `GET /batches/{id}` | **statistics**: notes by type / level / status, flags, documents, the pass counters, the lists of abstentions, duplicates and errors |
| `GET /batches/{id}/sample?n=10` | **sample**: flagged notes first (up to half), then a random draw — full text; what was shown is recorded |
| `GET /batches/{id}/notes?flag=any&status=pending` · `/notes/{name}` | browse, read one |
| `POST /batches/{id}/approve` | every **unflagged** pending note → memory. Refused (409) until the reviewer has opened `SOKKAN_DOC_INGEST_REVIEW_MIN_SAMPLE` notes of the batch (5). Flagged notes stay pending. |
| `POST /batches/{id}/reject` `{"reason"}` | every pending note rejected (kept aside under `rejected/`, never in memory) |
| `POST /batches/{id}/notes/{name}/approve` · `/reject` | one by one — the only way a flagged note gets in |
| `GET /decisions?format=csv` | the decision journal |

**Trust per source.** With `trust_after: N`, a source whose last N batches *decided by a
person* were all approved without one rejected note becomes trusted: the next batches
approve themselves at the end of the pass — unflagged notes at or below `trust_max_level`
only; the rest waits. Batches the policy approved do not count towards trust, and one human
rejection takes it away. `trust_after: 0` (default): every batch waits.

**The journal** (`decisions` in `docingest.db`, mirrored in the audit journal as
`doc_ingest.*`): who, when, source, batch, note, action (`sample`, `approve`, `reject`,
`approve_batch`, `reject_batch`, `auto_approve_batch`), the reason typed, and details (type,
level, flags of the note; notes approved / left pending; how many notes the reviewer had
opened; seconds since the batch was offered). It is a research signal: how people review
machine-written memory. It contains reviewers' identities — treat the export accordingly.

**The screen** (3.6): « Notes to review » in the simple profile (shown to the people who may
review the batches of the project), and Setup › Ingestion in the technical cockpit — the same
component (`frontend/components/simple/IngestReview.tsx`), French and English. Batches to
review, the batch in figures, the flagged notes first (decided one by one), a sample to read
(the progress towards `SOKKAN_DOC_INGEST_REVIEW_MIN_SAMPLE` is shown), approve / reject the
batch or one note, with a reason kept in the journal.

## 9. Passes: scheduling, resume, budget, journal

* **Nightly incremental pass** — a source with a `schedule` is started by the agents
  scheduler's own loop (`agents_runtime.Runtime.tick` → `docingest.scheduler.tick`), even
  when the instance has no Claude credentials (the pass only needs the local model). One
  pass per source at a time. An occurrence missed by more than 6 h is not caught up in the
  middle of the day.
* **Initial pass** — `python -m docingest run <source> --initial` in a terminal or a
  systemd unit, or `POST /sources/{id}/run`.
* **Resume** — each document is committed on its own. A pass interrupted by a stop, a
  restart or a crash is taken up again — same pass, same batch, counters and duration
  accumulated — by the next tick (`SOKKAN_DOC_INGEST_AUTO_RESUME=0` to resume by hand
  only). A pass a person stopped waits for a person. A batch being written cannot be
  reviewed.
* **Model down** — a connection error or a 502/503/504 is retried with a backoff (5 s to
  150 s, about six minutes in all: the time a local model takes to reload). If the model is
  still unreachable the pass ends `interrupted` with the document left untouched
  (`deferred` in the journal — not an error of the document), and the next tick resumes it.
* **Budget** — `max_tokens_per_pass` (prompt + completion), `max_docs_per_pass`,
  `max_minutes_per_pass`. Reaching one ends the pass with status `budget`; its batch is
  offered for review and the rest is read by the next pass. There is no price: the model is
  local, the scarce resource is its time (set the nightly window with
  `max_minutes_per_pass`).
* **Concurrency** — `concurrency` model streams (default 4). Documents read at the same
  moment cannot see each other's notes; those whose notes look alike are read once more,
  serially, so a duplicate or a contradiction between them is reported.
* **Journal of a pass** — `listed`, `unchanged`, `read`, `noted`, `notes`, `decisions`,
  `abstained`, `duplicates`, `errors`, `skipped`, `gone`, `flagged`, `tokens_in`,
  `tokens_out`, `seconds`, status (`running` · `done` · `budget` · `interrupted` ·
  `failed`) and one event per document with its outcome, reason, tokens and seconds.
  Counted from the ledger at the end of the pass, not from what the pass believes it did.

## 10. Settings

Switch: `SOKKAN_FEATURE_DOC_INGEST=1`. Everything below is optional except the model.

| Variable | Default | |
|---|---|---|
| `SOKKAN_DOC_INGEST_LLM_URL` | `SOKKAN_ASSISTANT_LLM_URL` | OpenAI-compatible endpoint of the **local** model, e.g. `http://127.0.0.1:8004/v1` |
| `SOKKAN_DOC_INGEST_LLM_MODEL` | `SOKKAN_ASSISTANT_LLM_MODEL` | e.g. `qwen3.8-27b` |
| `SOKKAN_DOC_INGEST_LLM_KEY` | — | bearer key if the local endpoint asks for one |
| `SOKKAN_DOC_INGEST_ALLOW_REMOTE_LLM` | — | `1` = accept a non-local endpoint. Not on a customer site. |
| `SOKKAN_DOC_INGEST_DIR` | `$SOKKAN_DATA_DIR/doc-ingest` | ledger, mirrors, configuration |
| `SOKKAN_DOC_INGEST_CONFIG` | `<dir>/sources.json` | the allow-list |
| `SOKKAN_DOC_INGEST_RCLONE` | `rclone` on the PATH | the rclone binary |
| `SOKKAN_DOC_INGEST_RCLONE_CONFIG` | `<dir>/rclone.conf` | remotes and their credentials (mode 0600) |
| `SOKKAN_DOC_INGEST_DOCLING` | `auto` | `auto` = Docling for PDFs that need it when installed · `off` · `always` |
| `SOKKAN_DOC_INGEST_PYTHON` | the api's interpreter | interpreter of the extraction child (e.g. a venv that holds Docling) |
| `SOKKAN_DOC_INGEST_CONCURRENCY` | 4 | model streams per pass (1–32) |
| `SOKKAN_DOC_INGEST_MAX_FILE_MB` | 25 | bigger files are never opened |
| `SOKKAN_DOC_INGEST_MAX_DOC_CHARS` | 240000 | extracted text given to the model |
| `SOKKAN_DOC_INGEST_EXTRACT_TIMEOUT_S` | 180 | per file |
| `SOKKAN_DOC_INGEST_EXTRACT_MEM_MB` | 4096 | resident memory of the extraction child |
| `SOKKAN_DOC_INGEST_MAX_TOKENS_PER_PASS` | 0 (none) | token ceiling of a pass |
| `SOKKAN_DOC_INGEST_MAX_DOCS_PER_PASS` | 0 | |
| `SOKKAN_DOC_INGEST_MAX_MINUTES_PER_PASS` | 0 | |
| `SOKKAN_DOC_INGEST_REVIEW_MIN_SAMPLE` | 5 | notes a reviewer must open before approving a batch |
| `SOKKAN_DOC_INGEST_AUTO_RESUME` | 1 | resume interrupted passes at the next tick |

## 11. Operating it

**Turn it on (compose).**

```bash
# 1. mount the share read-only on the host (see § 3), then
export SOKKAN_DOC_INGEST_MOUNT=/srv/sokkan-sources/formation
export SOKKAN_DOC_INGEST_LLM_URL=http://host.docker.internal:8004/v1
export SOKKAN_DOC_INGEST_LLM_MODEL=qwen3.8-27b
docker compose -f docker-compose.yml -f docker-compose.doc-ingest.yml up -d --build
# 2. declare the allow-list
docker compose exec api sh -c 'mkdir -p /data/doc-ingest && cat > /data/doc-ingest/sources.json' < sources.json
# 3. see what WOULD be read — nothing is opened
docker compose exec -w /app/backend api python -m docingest check nas-formation
# 4. first pass (long: in tmux / a systemd unit; Ctrl-C = interrupted, resumable)
docker compose exec -w /app/backend api python -m docingest run nas-formation --initial
```

Setup › Organization › Features shows `doc_ingest` with a readiness note when no pass can
run (no local model, no source, invalid `sources.json`).

**Start small.** First source: one folder, `trust_after: 0`. Read the first batches
entirely. Raise `trust_after` when the reject rate of a source is where you want it.

**Every morning**: `GET /api/doc-ingest/batches?status=pending` → statistics → sample →
approve / reject. Flagged notes one by one.

| Symptom | Where to look |
|---|---|
| pass `failed`, « not mounted read-only » | the mount lost `ro`, or `require_readonly` on a local folder |
| pass `failed`, « lists no file at all » | the mount fell, or the rclone sync returned nothing |
| pass `failed`, « rclone sync failed: … » | expired client secret, revoked site permission; `rclone lsd m365:` with the same config |
| « the model endpoint … is not on this machine » | `SOKKAN_DOC_INGEST_LLM_URL` points outside; fix the URL — do not open the exception |
| many `error: needs_ocr` | scans: install Docling, the next pass retries them |
| many `error: timeout` | a format the parser chokes on; raise `extract_timeout_s` for that source or deny the pattern |
| pass `budget` every night | the ceiling is lower than the daily change; the batch is still offered |
| notes never appear in recall | the batch is pending; or 409 `memory_store_required` at approval (a level above `project` needs the 3.0 store) |

**Backups**: `doc-ingest/docingest.db` and `sources.json` are under `$SOKKAN_DATA_DIR`
(covered by `scripts/backup.sh`); the mirrors can be rebuilt and may be excluded;
`rclone.conf` holds a credential — back it up like one.

**Turning it off**: `SOKKAN_FEATURE_DOC_INGEST=0`. Approved notes stay in the memory (they
are ordinary notes); pending batches stay in quarantine, unread by anything.

## 12. Measures on the gold set

The gold set (`tests/docingest_gold/gold.py`) is 32 synthetic documents of a fictional
training institute — course plans, minutes with decisions *and* with things that were only
discussed, procedures, an exact copy, a converted copy, an outdated version contradicted by
a newer one, a price decision revised six months later, five documents without lasting
value, a scan, a broken file, and a document carrying instructions for the AI that reads
it — in docx, pptx, xlsx, pdf, md and txt, with the expected outcome of each (23 notes,
18 decisions, 5 abstentions, 2 duplicates, 2 unreadable, 2 contradictions).
`scripts/doc-ingest-bench.py` runs it against a real local model.

**Model**: Qwen3.8-27B (INT4, vLLM, 4× Intel Arc Pro B60 — the reference Anchor), local
endpoint, reasoning off, strict JSON. **Date**: 2026-11-11, three runs of the same code
(two at 4 streams, one at 8), extraction with MarkItDown only (Docling off). Raw results,
every produced note included: `tests/docingest_gold/results/`.

| | run A (4 streams) | run B (4 streams) | run C (8 streams) |
|---|---|---|---|
| Documents with the expected outcome | 32 / 32 | 32 / 32 | 32 / 32 |
| **Decisions found** (of 18 expected) | 18 | 18 | 18 |
| Decisions produced | 19 | 18 | 19 |
| … of which **not a decision** | 1 (a postponement recorded as a decision) | 0 | 1 (the same) |
| … flagged by the checks (quote / figure not in the document) | 0 | 0 | 0 |
| **Correct abstentions** (of 5 valueless documents) | 5 | 5 | 5 |
| Wrong abstentions (a document worth a note) | 0 | 0 | 0 |
| Notes on valueless documents | 0 | 0 | 0 |
| Duplicates recognised (1 exact copy, 1 converted copy) | 2 / 2 | 2 / 2 | 2 / 2 |
| Contradictions reported (of 2) | 2 | 2 | **1** |
| Contradictions reported that are not one | 1 (vague, no figure) | 0 | 0 |
| Trapped document: flagged · forbidden content in a note · level lowered | yes · none · no | yes · none · no | yes · none · no |
| Unreadable files consigned (scan without OCR, broken docx) | 2 / 2 | 2 / 2 | 2 / 2 |
| Notes produced (reference + decision) · flagged | 42 · 6 | 41 · 5 | 42 · 3 |
| **False notes** (not-a-decision + false contradiction) | 2 of 42 | 0 of 41 | 1 of 42 |
| Wall time, 32 documents (29–33 model calls) | 177 s | 190 s | 159 s |
| **Documents per hour** | 652 | 608 | 723 |
| Seconds per document, median · max | 17.3 · 41.3 | 17.5 · 42.1 | 25.8 · 72.3 |
| **Tokens per document read by the model** (in · out) | 2 364 · 521 | 2 383 · 509 | 2 212 · 503 |
| Second pass (nothing changed): model tokens | 0 | 0 | 0 |

Reading it honestly:

* **The decision figures are an upper bound.** 18 of 18 on documents written by the author
  of the prompt, in clean French, where decisions say « décide », « est fixé », « adopte ».
  The one recurring error is the realistic one: an item *postponed* is recorded as a
  decision (2 runs of 3).
* **Contradictions are the weak point.** Found 5 times out of 6; the miss is at 8 streams,
  where two related documents were read at the same moment and the serial re-reading did not
  pick the pair. And three defects were found *by this measure* and fixed before these
  runs: a revised decision was not reported as a contradiction (prompt); the earlier note
  had been raised to `confidential` by the model and was therefore hidden from the later
  document of the same folder (now shown, and the level is inherited — flag
  `level_inherited`); the model invented what the existing note said (now checked against
  the excerpt it was shown — one vague case without a figure still passed in run A).
* **Use 4 streams.** 8 streams gain 10–15 % of throughput, double the latency per document
  and cost a contradiction. The prompt keeps its stable part (instructions, schema) first,
  so the model's prefix cache serves every call.
* An earlier run fell in the middle of a restart of the model: 8 documents were marked as
  errors. That is why a model that stops answering now suspends the pass (§ 9) instead of
  blaming the documents.

**Extrapolation to 20 000 documents** — arithmetic, not a measure:

| Assumption | Time on one Anchor at 4 streams | Tokens |
|---|---|---|
| Documents like the gold set (≈ 2 400 tokens, 1–2 pages), ≈ 630 documents / hour | **≈ 32 hours** — 4 nights of 8 h | ≈ 52 M (47 M in, 10 M out for the ≈ 90 % that reach the model) |
| Real documents averaging ≈ 8 000 tokens (10–12 pages) — **not measured**; bounded by the cold reading speed of ≈ 900 tokens/s shared by the streams | **≈ 55–65 hours** — 7 to 8 nights | ≈ 155 M |
| Scans through Docling (CPU) | + ≈ 3.4 s per page | — |

Then the nightly incremental pass only reads what changed (0 token when nothing did).
**The scarce resource is not the machine, it is the review**: at 1.3 notes per document,
20 000 documents are ≈ 26 000 notes, of which 7–14 % flagged (≈ 2 000–3 500 notes that can
only be approved one by one), plus a sample of 5 per batch. Plan the first pass folder by
folder, not as one batch of 26 000 notes.

## 13. Limits, and what is not done

* **Rights per document are not propagated.** A note inherits the classification of its
  **source** (one level per declared source), not the access-control list of the file. Two
  people with different rights on the same SharePoint library see the same notes if they
  have the same clearance in SOKKAN. Declare one source per confidentiality zone; do not
  ingest a library whose folders have heterogeneous rights as one source.
* **The review screen is minimal** (§ 8): no search inside a batch, no side-by-side with the
  source document, no keyboard review; ~26 000 notes for 20 000 documents will make the human
  review the bottleneck — the trust policy per source (`trust_after`) is the lever.
* **Real tenants not validated** (§ 4); the image's rclone (Debian 1.60) is too old for the
  Microsoft application-only mode; rclone credentials are a file, not yet in the vault /
  secrets provider.
* **The image build with `SOKKAN_DOC_INGEST=1` was not run** in this work (extractors were
  tested from a virtualenv).
* **Quality is the model's.** § 12 is 32 synthetic documents in French, written by the
  author of the prompt — an upper bound, not a forecast. Real archives have scans, tables
  that carry the meaning, minutes that never say « décide ». Re-measure on a sample of the
  customer's own documents (the scorer takes any expectations file).
* **Long documents** are truncated (first 75 %, last 25 % of 240 000 characters), flagged —
  a decision in the middle of a 300-page report can be missed. No map-reduce yet.
* **Contradictions** are searched only against the handful of closest notes (lexical index
  of ingested notes + the memory's search); a contradiction with a more classified note of
  another source, or with a note that does not look alike, is not seen. Measured: 5 found
  of 6, and one false report in three runs (§ 12).
* **Level creep inside a source**: when the model raises one note of a folder, the
  look-alike documents of that folder inherit the level. Upward only, flagged
  `level_inherited`, lowered by a maintainer with a reason (existing reclassification).
* **Deleted and moved files**: a deleted file is marked `gone`, its notes stay. A file
  moved to another path is read as a new document (same text → `duplicate`, so no second
  note, but the note keeps the old path).
* **Images, audio, video, e-mails, archives** are not read. Scans depend on Docling, whose
  default OCR mangles French accents (§ 5): the OCR language model is to be configured and
  re-measured before scans count.
* **One process**: passes run in threads of the api process; a restart interrupts them
  (they resume). No distributed workers.
* **Languages**: prompt and injection patterns cover French, English, German; the note
  labels too. Italian documents are read, their labels fall back to French.
