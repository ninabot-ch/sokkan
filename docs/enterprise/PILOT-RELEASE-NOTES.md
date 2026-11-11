# Pilot candidate — release notes (branch `pilot-integration`)

For whoever decides to cut the release of the HEG-Genève × eSkills pilot, and for whoever
installs it. **Nothing is released**: this branch is a candidate — no tag, not merged in `main`,
not on GitHub, deployed nowhere. The version is set on the branch: **3.6.0 « Icebreaker »**
(a minor: new variables, changed defaults), commit `11182ac` (`VERSION`, dated CHANGELOG, Chart
`appVersion`).

State of this page: 11.11.2026. The whole battery was replayed on **`11182ac`** — the commit that
sets the version: the suite in one pass, `run.sh all` (T0-T14), the upgrade phase from 3.5.1,
`tsc`, `next build`, ruff and the four guards (§ 4, first lines of each row). The journeys (local
model, real gateway) and the mutations are those of earlier commits and were not replayed. Every
figure below was measured on this branch; where a thing was not played, the page says so.

## 1. What the candidate holds

`main` (v3.5.1, `6f7fee5`) plus four lots, merged in this order, plus what holds between them.

| Lot (branch) | What it brings | Its own page |
|---|---|---|
| `onprem-appliance` | SOKKAN on a box at a site with no engineer: pinned and signed release, one upgrade path, **named accounts in local sign-in** (a personal code per person, a project per person), the local Ship → Deep router, HTTPS on the LAN, encrypted off-site backup, support tunnel, heartbeat, the guard « every variable read is declared » | `docs/appliance/README.md` |
| `study-mode` | the **journal of human validations** (always on: table `hitl` of `audit.db`), study mode (pseudonyms, consent, tags, two end-of-task questions, researcher's package) | `docs/enterprise/STUDY-MODE.md` |
| `doc-ingest` | a memory agent reads the organisation's documents with a **local** model and proposes notes, quarantined **by batch** | `docs/enterprise/DOC-INGEST.md` |
| `simple-profile` | document drop, the simple profile (home, plain-language thread, approval card, deliverable), French / English | `docs/enterprise/SIMPLE-PROFILE.md` |

**Between the lots** (this branch — each with a test in `tests/test_pilot_integration.py`):

| | What was missing | Now |
|---|---|---|
| a | A click on the simple profile's approval card left no trace | Allow / Refuse / Correct-then-allow each write one row of the journal (asked at, decided at, delay, who, edit marked); the shell request a simple work refuses by itself is journaled as automatic |
| b | The study hooks of the simple profile had never met `study_mode`; the band's slot was empty; a person had no way to consent from the simple profile | Band, « What is recorded » and the consent in the simple profile; no work is created before the consent; scenario and the two questions follow the tag without a reload; silent without the feature |
| c | Study mode warned « one identity » for any local sign-in | Named accounts are distinct participants: three people = P1, P2, P3, three actors in the journal and the audit, one project each; no address, name or project in the researcher's package |
| d | Nothing kept a dropped document out of the memory, nor the ingestion out of the instance's own files | A session that was handed documents has its memory writes quarantined (and its file tools refused on the memory directories); an ingestion source cannot be the data directory, the workspace, the memory or the quarantine |
| e | The review of the batches was an API with no screen | « Notes to review » (simple profile) and Setup › Ingestion (technical cockpit): one screen, French / English, every decision journaled |
| f | The appliance profile knew none of the three other lots; five variables were read and never declared; the local engine failed with a 400 on the CLI's default reasoning effort | The profile turns the four features on with their needs, `SOKKAN_PROFILE_DEFAULT=simple`, `CLAUDE_CODE_EFFORT_LEVEL=medium`; the guard passes; the release images carry the document extractors |
| g | **On an appliance the study was never read and never announced**: sessions go through the local router, which sent neither `x-sokkan-study` nor the pseudonym, and `GET /usage` was only asked in « included » mode — the gateway would have answered 412 to every Deep request of a study tenant | The api reads `GET /usage` on the Deep gateway; sessions send the study and the pseudonym; the router forwards both (never an address) and hands the refusals 410 / 412 / 429-per-person back as the gateway worded them, with `x-should-retry: false` |
| h | The instance knew the gateway of `study-quotas` 664a47f; its head f260ada changed the text of the 412 and added a 429 `user_limit_reached` (too many distinct people today, `max_users_day`) that the local router turned into « wait a minute and send again ». In the simple profile every refusal read « a technical problem, you can send your request again » | The four study refusals are handed back once, never retried; the session says one sentence with the gateway's code (no variable name in it, no generic 429 sentence on top); the simple profile has a notice per refusal, French and English. Tests: `tests/test_gateway_refusals.py`, written from what the gateway **answered** (its app played on a throw-away database), not from the instance's code |
| i | Since 3.2 an agent run (Crew) could not write a file in the work folder of a project other than `default` (`agentchat._in_memory_dirs` matched the whole `<data>/projects` tree) | It writes in `<data>/projects/<its project>/work`; the memory directories, the quarantine and the other projects stay closed (`tests/test_agent_run_project_work.py`) |
| j | In the simple profile a later message that begins with « / » (« /deep Propose-moi… », the escalation keyword first) was a slash command for the CLI: the person read « Unknown command: /deep » and nothing went to a model (found on the journey against the real gateway) | The brief comes first for such a message; the person's words stay whole and the router finds the keyword (`tests/test_simple_profile.py`) |
| k | The CLI's session-title call carries the person's first message: with the escalation keyword in it (« … /deep ») the local router sent it to Deep too, and ONE question counted TWO Deep escalations of the person's day on the gateway (`deep_user_day` 2, found on the journey against the real gateway) | A call of the CLI that declares no tool (title of the session, summaries) is a side call: it stays on the local model whatever it carries or asks for, and is not sent to Deep when the local model is down (`backend/shiprouter.side_call`; `tests/test_shiprouter.py`, input = the call the bundled CLI really sent, `tests/fixtures/cli_calls/`). Replayed with the real CLI through the router: 2 Deep calls before, 1 after |
| l | On an appliance the notice of a person's ceiling (429 `user_daily_cap`) said « tomorrow », although only the stronger model is closed and the model of the box remains | Its own sentence on an appliance, French and English (`no.user_daily_cap_local`; `study.local_model_remains`): carry on right away with the usual model, the stronger one opens again tomorrow (`tests/test_gateway_refusals.py`) |

## 2. Features and what each needs

| Feature | Status | Default | Requires | Switch |
|---|---|---|---|---|
| `documents` | beta | off | — | `SOKKAN_FEATURE_DOCUMENTS` |
| `simple_profile` | beta | off | `documents` | `SOKKAN_FEATURE_SIMPLE_PROFILE` (+ `SOKKAN_PROFILE_DEFAULT`, `SOKKAN_PROFILE_LOCKED`) |
| `study_mode` | beta | off | — ; the journal of validations it exports is always on | `SOKKAN_FEATURE_STUDY_MODE` |
| `doc_ingest` | experimental | off | `agents`, `memory_quarantine` (an invariant), a **local** model endpoint, the extractors in the image | `SOKKAN_FEATURE_DOC_INGEST` |
| named local accounts | — (not a feature: local sign-in) | — | `SOKKAN_AUTH_MODE=local`; one project per person needs `multi_project` and `SOKKAN_DEFAULT_ROLE=none` | `scripts/appliance/accounts.sh` |
| local router Ship → Deep | — (compose profile `appliance`) | — | `SOKKAN_SHIP_URL`; Deep needs `SOKKAN_DEEP_URL` + `SOKKAN_DEEP_TOKEN` | `COMPOSE_PROFILES=appliance` |

A switch asked for without its dependency is reported in Setup › Organization › Features
(`problem`), never half on. The generated reference: `docs/enterprise/FEATURES.md`,
`docs/ENVIRONMENT.md`.

## 3. Settings of the pilot

The appliance profile (`scripts/install.sh --profile appliance --version <version+commit>`)
writes them; the template is `deploy/appliance/env.appliance.example`.

```sh
# who
SOKKAN_AUTH_MODE=local            # + one personal code per person (accounts.sh), a project each
SOKKAN_DEFAULT_ROLE=none
SOKKAN_FEATURE_MULTI_PROJECT=1
# the cockpit of the people who work there
SOKKAN_FEATURE_DOCUMENTS=1
SOKKAN_FEATURE_SIMPLE_PROFILE=1
SOKKAN_PROFILE_DEFAULT=simple
SOKKAN_FEATURE_STUDY_MODE=1       # inert until a study is active
SOKKAN_FEATURE_DOC_INGEST=1       # reads nothing until a source is declared on the host
SOKKAN_DOC_INGEST_LLM_URL=http://host.docker.internal:8004/v1
SOKKAN_DOC_INGEST_LLM_MODEL=qwen3.8-27b
SOKKAN_DOC_INGEST_CONCURRENCY=4   # 5/6 contradictions found at 4 streams, fewer at 8 (gold set)
# inference
SOKKAN_SHIP_URL=http://host.docker.internal:8004     # the model of the box
SOKKAN_DEEP_URL=https://infer.sokkan.ch              # + SOKKAN_DEEP_TOKEN: the study tenant's token
CLAUDE_CODE_EFFORT_LEVEL=medium
```

**The study** is set on the gateway, not on the box (`sokkan-cloud`, `infer/pilote-etude.md`):
tenant in study, `audit_required`, end date 30.04.2027, 6 M tokens a day, 120 M a month, 20 Deep
escalations per person and per day (starting values). The box reads it (`GET /usage`) and shows
it. `SOKKAN_STUDY_ID` on the box declares a study only when no gateway does.

**People**: three accounts, `accounts.sh add <e-mail> --name … --project …`. The first consent
allocates P1, the next P2… (order of consent, never reused). The correspondence table stays in
`study.db` on the box; Setup › Study shows it to the instance's admins only.

**Ingestion**: sources in `/data/doc-ingest/sources.json` on the host (never in the cockpit);
mount the documents read-only (`docker-compose.doc-ingest.yml`); review in « Notes to review ».

## 4. What was verified, and how

| | Result |
|---|---|
| Test suite, one file per process, Postgres pgvector (throw-away) | **1 595 passed, 0 failed, 67 skipped** (118 files, 810 s) on **`11182ac`** (`VERSION` 3.6.0), in ONE pass of ONE new container (`python:3.12-slim` + Postgres 16 client, curl, Node 22, docker CLI; pgvector pg16 next to it; `SOKKAN_CLOUD_SRC` = `sokkan-cloud` 293573d, so the comparison of the frozen contract ran). Same count as on `9212f9a`. Skipped: Helm chart (no `helm` binary), OpenBao (no test server), embedding / rerank service, bubblewrap, runners (no Docker daemon in the container), CLI end-to-end, rclone, Prometheus backtest, root ignoring directory permissions. **History**: 1 574 passed, 0 failed, 67 skipped on `1d5813e` (a total put together from two environments, see the end of this cell) — it was 1 542 / 0 / 68 (116 files) on `8257d42`: + 31 tests (the gateway's refusals 25 + 1, the agent-run work folder 5), and the comparison of the frozen contract with the gateway's checkout now **runs** (it was skipped: no checkout on the build host — played against the checkout it was red, the contract being that of 664a47f). `main` in the first environment: 1 152 passed, 0 failed, 66 skipped (95 files). Skipped: Helm chart (26, no `helm` binary), OpenBao (18, no test server), embedding service (9), sandbox / bubblewrap (5), runners (3, no Docker daemon), CLI end-to-end (2), rclone (2), 2 single ones (root ignores directory permissions; no Prometheus to backtest on). The Postgres files ran. **How the figure was obtained**: a new `python:3.12-slim` container (the first environment was gone): a first pass gave 1 546 passed, 1 failed, 74 skipped — the failure (`test_backup_restore`, `pg_restore` 17 of the container against the Postgres 16 server: « unrecognized configuration parameter transaction_timeout ») and 7 of the skips (no `curl`, no `docker` CLI, Node 20 instead of 22) came from that container; the four files concerned were replayed with a Postgres 16 client, curl, the docker CLI and Node 22: all green, which gives the total. Not one process from a single environment, then |
| ruff · tsc · `next build` | **On `11182ac`**: ruff clean on `backend tests memory scripts` (in the suite's container), `tsc --noEmit` clean and `next build` OK in a new `node:22-bookworm` container on the build host (`npm ci` + both: 62 s; first load of `/`: 323 kB). **Before**: ruff clean on `backend tests memory scripts` (one F401 in `deploy/helm/sokkan/ci/runner-smoke-driver.py`, already on `main`, outside that scope) · `tsc --noEmit` clean — both replayed on `1d5813e` · `next build` OK on `8257d42` (first load of `/`: 323 kB; the review screen is a lazy chunk), **not** replayed since (two front files changed: a list of codes in `Thread.tsx`, eight sentences in `dict.ts`) |
| `gen-features-doc --check` · `gen-env-doc --check` · `check-simple-tokens.py` · `check-corthexis-sync.sh` | all four pass on **`11182ac`** (`memory/core` matches CortHeXis v2.1.2, compared with an archive of that tag; 30 tokens × 2 themes, 334 sentences × 2 languages) |
| Mutations of the integration tests (one guard removed at a time) | 22 of 22 killed: each of the guards a-g removed in turn makes `tests/test_pilot_integration.py` fail (on `8257d42`, not replayed). h: with the six changed files put back as they were, 19 of the 25 tests of `tests/test_gateway_refusals.py` fail. i: the refusal was reproduced on the unchanged code before the fix (a `Write` in `<data>/projects/formation/work` refused as « memory file ») |
| Appliance bench, Docker-in-Docker, mock model and gateway | **On `3.6.0+11182ac`, `run.sh all` in one run (461 s, a blank machine): T0, T1, T2, T2b, T3-T7, T11-T14 and T8, T9, T10 from 3.4.6 — 16 records, all PASS** (build and sign 49 s with a warm build cache, 208-216 s cold; install 18 s; 570 MB after install, 2.2 GB with 3 simultaneous sessions; first answer 2.7 s; reboot → healthy 19 s; T14: 58 variables; T8 installer 18 s, `/api/health` away 4.1 s; T10 rollback 17 s, 3.1 s away; T9 `scripts/upgrade.sh` 4 s, 4.1 s away, 826 files, same tree). **Then `run.sh upgrade` from 3.5.1** (`E2E_OLD=3.5.1+6f7fee5`, another blank machine, 453 s): T0, T8 (18 s, 4.4 s away), T10 (17 s, 3.1 s), T9 (4 s, 3.0 s away, 826 files) all PASS. **The version number now tells old from new**: `/api/version.version` is asserted 3.4.6 / 3.5.1 before the upgrade and after the rollback, 3.6.0 (`dist` `3.6.0+11182ac`) after the upgrade. Machine peak of the full run: 6 026 MiB — **the 6 GB cap is reached** at the end of the appliance phase (16 sessions left open), host pressure peak 10.10 for a watchdog threshold of 10. A first attempt of `run.sh all`, played while the test suite ran on the same host, was stopped by that watchdog after T12 (host pressure 11; T0-T7 and T12 had passed): replayed alone, it is the run above. Results: `tests/e2e_appliance/results/3.6.0_11182ac*.json`. **Earlier runs**: T0, T1, T2, T2b, T3, T4-T7, T11, T12, T13, T14 **all PASS** on `3.5.1+8257d42` (build and sign 189 s; install 37 s — 18 s before the extractors were in the image; 585 MB after install, 2.2 GB with 3 simultaneous sessions; first answer 2.9 s; reboot → healthy 19 s; T14: 58 variables of the profile checked, the 9 new ones included). Machine peak 5.85 GB of the 6 GB cap (during the image build). **Upgrade phase, replayed on `3.5.1+1d5813e`** (`run.sh upgrade`, a blank machine each time): from **3.4.6** T8 PASS (installer: 17 s, `/api/health` away 3.1 s at most, 538 MB after), T10 PASS (rollback 15 s, 4.1 s away), T9 PASS (`scripts/upgrade.sh`: 3 s, 3.1 s away, 805 files, same tree as the installer's); from **3.5.1** (`E2E_OLD=3.5.1+6f7fee5`, a switch added to the bench for this) T8 PASS (20 s, 4.1 s away), T10 PASS (15 s, 3.1 s), T9 PASS (4 s, 5.1 s away, same tree). Machine peak 1.2 GB. Both versions carry the number 3.5.1: on the second run old and new are told apart by `/api/version.dist`, `SOKKAN_COMMIT`, `.sokkan-previous` and the files of the newer release, not by the version. The data checked across an upgrade: the access token, one board card, the length of the journal — an instance with months of sessions and a memory was not played. The appliance phase (T1-T7, T11-T14) was **not** replayed on `1d5813e` |
| One journey of Camille on a throw-away instance, real local model (`qwen3.8-27b`, vLLM, 4× B60), study mode on | played once on the fixed code — see below |

**The journey** (11.11.2026, the model otherwise idle, one stream): sign-in with a personal
code → the first « Lancer » is refused and opens « What is recorded » → consent → 3 documents
(docx, pptx, pdf) + the template « Plan de formation » → **first draft 46 s after « Lancer »**
(approval card, the 3 sources named with their age; a draft of 7 290 characters) → the draft
corrected, « Autoriser avec mes corrections » → deliverable ready at 61 s → validated → the two
questions answered. One turn of 55 s, 104 509 tokens in, 3 599 out. Afterwards, in the
databases: one row of the journal (`Write`, decision `edit`, edited, actor Camille, origin
`owner`, her project, asked 03:41:40, decided 03:41:42); `study.db`: P1, consent without content,
tag S1, one turn (`qwen3.8-27b`), feedback 4 / 6 min; the researcher's package read with an
export token: P1, scenario, durations, the validation with its delay — no address, no name, no
project name, title and request as `{chars, hmac}`, `content: []`.

**What the first attempt found.** The journey was first played on the code as merged: at 145 s
the first card was not a draft but « the agent wants to keep … in the project's memory ». The
new rule « a session with documents does not write memory files » matched the whole
`<data>/projects` tree, so in a project other than `default` the save of the deliverable
itself was refused, and the model fell back on the memory. Fixed (the rule matches the memory
directories and the quarantine only), with a test that fails on the old code; the figures
above are the replay. The same over-broad match existed **for agent runs** since 3.2
(`agentchat._in_memory_dirs`): a Crew agent could not write a file in the work folder of a
project other than `default`. Reproduced and fixed on this branch (§ 1, i): only the run's own
work folder opens; not played with a real Crew agent on a real model.

**A batch review, for real** (same instance, the memory agent on the same model, 2 streams):
14 documents → 23 notes, 2 flagged, 5 documents without a note, 2 unreadable, in 63 s; 8
documents → 7 notes, 3 flagged, 1 duplicate, in 41 s. One flagged note refused with a reason,
one batch refused, one batch approved (4 notes in the memory, 2 flagged left): each decision
is a line of the journal under the reviewer's name. Captures: `docs/ui/pilot-integration/`.

## 5. Known limits

Of the integration:

* **Played once against the production gateway** (11.11.2026, test tenant, `b2ce8bd`): the study
  read from `GET /usage`, a Deep escalation answered, 412, 429 `user_daily_cap` and 429
  `user_limit_reached` seen on screen in French and English, handed back once by the local router
  — captures, exact texts, request counts and the defects found:
  `docs/ui/pilot-integration/passerelle-reelle/README.md`. 410 `study_expired` was not played.
  Before that journey: the contract (`GET /usage`, headers, 410 /
  412 / the two 429) is tested against the gateway's own documentation, its source text and
  the answers of its app played in-process on a throw-away database, head `study-no-retry` 293573d
  (`tests/fixtures/gateway_contract/`, compared with a `sokkan-cloud` checkout when one is at
  hand — the comparison is **skipped** where there is none) and against a fake gateway on a
  real socket. (`study-quotas` itself is deployed on the gateway since 11.11.2026, `sokkan-cloud` bd8a8bc.)
* **No retry guard without the local router — fixed and deployed on the gateway.** `sokkan-cloud`
  main 293573d sends `x-should-retry: false` on the four study refusals and on them only; in
  production on the gateway since 11.11.2026 (checked there: a 412 of the test tenant carries the
  header, a 401 does not). Before it, an instance whose sessions called the gateway directly let
  the CLI retry the two 429: 177 s of spinner before the notice, played once on the real gateway
  (real CLI against the gateway's app: 189 s and 22 requests before, 1.4 s and 2 after). The frozen
  contract here is regenerated from that code (`tests/fixtures/gateway_contract/regenerate.py`).
  Not replayed in a browser against the real gateway since the fix.
* **Three of the four refusal notices of the simple profile were looked at in a browser**, on
  the real gateway (see above); the fourth (`study_expired`) has tests only.
* **A person's token ceiling must be sized on a whole work**: one request of a session is
  ≈ 29 000 tokens and a short work 200 000; with `user_tokens_day` 20 000 the gateway serves one
  request per person and per day.
* **Side calls of the CLI are recognised by their shape** (sent by the CLI, no tool declared).
  Only the session-title call was captured and replayed; a side call that would declare tools
  (none seen) would still follow the ordinary rules. On a box WITHOUT local model (not the
  appliance profile) a side call still goes to Deep: there is nowhere else to send it.
* **After `user_daily_cap` on an appliance an escalation the router decides by itself is still
  refused** (repeated tool failures, prompt size, local model down): the router does not fall
  back to the local model on that refusal. The notice tells the person to send the request
  without asking for the stronger model.
* **After a restart of the api a work reopens with an empty thread** (the deliverable and its
  validation are kept; seen on the throw-away instance, not investigated).
* **Other inference errors in the simple profile** (402, tenant-wide 429, unreachable) still
  read « a technical problem, you can send your request again », the readable sentence being
  under « technical detail ». Unchanged here.
* **The tier and the reason of an escalation are not in the box's journal** (the CLI does not
  expose them): join with the gateway's export on the pseudonym and the time.
* **The journal's own export** (`/api/hitl/journal`, instance admins) names people: it is the
  instance's audit, not the researcher's package. Only the package is pseudonymous.
* **Memory writes of a session with documents are quarantined per session, not per document**:
  one document attached quarantines every later `memory_write` of that session, including what
  has nothing to do with it. A shell command (technical profile) could still write a file in
  the memory directory after the person approved it — the simple profile has no shell.
* **The review screen is minimal**: no search in a batch, no side-by-side with the source, one
  note at a time for flagged notes. Reviewing is the bottleneck of a large archive
  (≈ 26 000 notes for 20 000 documents).
* **The local router has no sentinel and no judge** (they are in the hosted gateway only).
* **`study_mode` on with no study** is silent, but the sub-tab Setup › Study is there for everyone.
* Setup gained a sub-tab « Ingestion » and the simple drawer an entry « Notes to review »: the
  navigation validated on 08.10.2026 did not have them — to be confirmed.
* The sign-in screen is the cockpit's (English).
* **A project's memory needs the Postgres store** (as since 3.4.2): on an instance with the
  SQLite index, approving a note of a project other than `default` is refused — the review
  screen says so on the note. The appliance profile installs the Postgres store.
* **The model may ask to « keep » something in the memory** in the middle of a work with
  documents: the card says so, and what is allowed waits in the unit quarantine (CortHeXis),
  which has no screen in the simple profile.

Inherited from the lots (unchanged, see their pages): the approvals the CLI grants by itself are
not journaled; no screen to edit an input before allowing in the technical profile;
`sources.csv` of the researcher's package needs the Postgres store; rclone sources not validated
on a real Microsoft 365 / Google tenant; rights per document are not propagated (one source per
confidentiality zone); OCR of scanned PDFs; sessions are never closed (≈ 0.3-0.4 GB each);
systemd units, a real Tailscale tunnel and the GPU memory profile were not played in a container.

## 6. Before cutting — check-list

Decisions (Nick):

- [ ] Name of the release (§ 7).
- [x] Release signing key: generated 11.11.2026 (cosign v2.4.1). Private key and passphrase in Vaultwarden,
      entry `sokkan/release-signing-key`; public key in `deploy/appliance/cosign.pub` (to install as
      `/etc/sokkan/cosign.pub` on each box). Round trip checked: the key read back from the vault signs,
      the public key verifies.
- [ ] Registry of the prebuilt images the client's box can reach.
- [ ] `sokkan-cloud` branch `study-quotas` reviewed, merged and deployed on the gateway
      (`infer/pilote-etude.md` § 8), then the study tenant created — a write on production.
- [ ] The two navigation additions (Setup › Ingestion, « Notes to review »).
- [ ] Retention of `study.db` and of the journal after the study (the protocol must say it).

Technical:

- [ ] `main` has not moved since `6f7fee5` (else: merge `main` into the branch, replay § 4).
      Checked again on 11.11.2026 after the battery, against the forge: `main` is still `6f7fee5`
      — to check once more at the moment of the cut.
- [x] The suite of § 4 green on the commit that sets the version, with Postgres, in one pass:
      1 595 passed, 0 failed, 67 skipped on `11182ac` (`VERSION` 3.6.0). Holds for the tag as long
      as only documentation follows `11182ac` and the merge is a fast-forward (§ 7).
- [x] The upgrade phase T8-T10 from 3.4.6 and from 3.5.1: all PASS on `3.6.0+11182ac`
      (11.11.2026; `tests/e2e_appliance/results/3.6.0_11182ac*.json`); the version number tells
      old from new.
- [x] `tests/e2e_appliance/run.sh all` (T0-T14 in one run) with `VERSION` at 3.6.0: 16 records,
      all PASS on `3.6.0+11182ac`. The machine reaches its 6 GB cap at the end of the appliance
      phase (sessions are never closed — § 5).
- [x] `tsc --noEmit`, `next build`, ruff and the four guards (`gen-features-doc --check`,
      `gen-env-doc --check`, `check-simple-tokens.py`, `check-corthexis-sync.sh`) on `11182ac`.
- [x] The instance knows the gateway's 429 `user_limit_reached` and the new text of the 412
      (`study-quotas` f260ada), in the session and in the simple profile, French and English
      (§ 1, h). To redo if `study-quotas` moves again before it is merged: with the checkout at
      hand `tests/test_pilot_integration.py::test_the_frozen_contract_is_the_gateways_own_text`
      goes red.
- [x] The agent-run defect `agentchat._in_memory_dirs` (§ 1, i): reproduced, fixed, tested.
- [ ] One journey on the delivered box, with the real gateway and a test tenant: a Deep
      escalation answered, then the three refusals seen in words. Played on 11.11.2026 on a
      throw-away instance of `b2ce8bd` (not the delivered box, no installer, no HTTPS):
      `docs/ui/pilot-integration/passerelle-reelle/README.md`.
- [ ] `restore.sh` of the instances already running (internal, demo): they still have the
      script that breaks on a started machine — fixed in this release, to roll out.
- [ ] `sokkan-cloud/site/site/install.sh` replaced by `scripts/install.sh` of this release.
- [ ] `SOKKAN_FEATURE_MISSIONS_LINK` is now off unless asked: set it on the public demo if the
      link must stay.

## 7. Cutting the release

Version: **3.6.0 « Icebreaker »** (name chosen by Nick, 11.11.2026 — the ship that opens the way for
the others). Series so far: « Captains » (3.2), « Helm » (3.3), « Bridge » (3.4), « Lookout » (3.5).

Image registry for `release-artifacts.sh --images`: the OVH managed registry (Harbor, plan SMALL, 200 GB)
that already exists in the `nokker-beta` Public Cloud project, in a dedicated Harbor project `sokkan`.
Exoscale has no native registry (only a third-party Marketplace listing, billed on top of the ISV credit).

```sh
# 1. in /root/sokkan (on main), once the check-list is done. FAST-FORWARD: main is an ancestor of
#    the branch, so main lands on the very commits that were verified (a --no-ff merge would tag
#    a commit nobody tested). If it refuses, main has moved: merge main into the branch, replay § 4.
git fetch origin && git merge --ff-only origin/pilot-integration
# 2. nothing to do: VERSION, the dated CHANGELOG section and the Chart appVersion are on the branch
#    since 11182ac. If the cut is not made on 2026-11-11, fix the date of the section first
#    (cut-release.sh commits VERSION and CHANGELOG.md if they differ, then tags).
# 3. from ninabot-pro — a minor needs the two banners
infra/sokkan/cut-release.sh 3.6.0 --dry-run
infra/sokkan/cut-release.sh 3.6.0 --pastille-fr "3.6 « Icebreaker » — …" --pastille-en "3.6 “Icebreaker” — …"
# 4. the appliance artefacts (signed tarball + images), with the signing key
scripts/release-artifacts.sh <dist>/sokkan-3.6.0+<commit>.tar.gz --images <registry>
```

`cut-release.sh` pushes the tag, publishes the GitHub release, the dist, and **rolls the managed
fleet out** (the public demo included): the release changes defaults (`missions_link` off; tmux
and Preview off without their binary) — read the upgrade notes of the CHANGELOG first
(`make-release.sh <src> --no-rollout` publishes a dist without touching the fleet).
