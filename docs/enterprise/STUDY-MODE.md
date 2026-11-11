# Study mode — audited use under a research protocol

Two things, delivered together and switched separately:

| | Switch | What it is |
|---|---|---|
| **Journal of human validations** | none — always on | every approval a session asks for and every decision, durable |
| **Study mode** | feature `study_mode`, off by default | tags, pseudonyms, consent, two questions, researcher's export |

Study mode is for an organisation whose volunteers use SOKKAN while a school observes how
agents are supervised at work. The instance stays the customer's: what leaves it is one
export package, with no name in it.

## 1. The journal of human validations

Table `hitl` of `audit.db` (same file, same backup as the action journal). One row per
request:

| Column | Meaning |
|---|---|
| `kind` | `permission` (a tool call), `question` (the agent asked something), `mode` (approval mode changed) |
| `session_id`, `project`, `owner` | where, and who opened the session |
| `tool`, `title`, `input_summary`, `input_sha256` | what was asked — the input as JSON, **secrets masked** (the vault's values and their encoded forms, then credential patterns), cut at 2 000 characters. Never a tool's output |
| `requested_at`, `decided_at`, `delay_ms` | when it was asked, when it was decided, how long the person took |
| `decision` | `allow`, `deny`, `edit` (allowed with a modified input: `edited_summary` holds what ran), `answer`, `mode_change`, `auto_allow`, `auto_deny`, `cancelled`, `interrupted`, `pending` |
| `actor`, `origin` | who decided, from where: `owner`, `project_member` (another dev of the project in the cockpit), `share` (delegated review), `teams` (Microsoft Teams), `system` |
| `auto`, `auto_reason` | `1` when **nobody clicked**: `mode:bypassPermissions`, `mode:acceptEdits`, `policy:…` (an agent's allowed tools), `sandbox` |
| `message` | the reason of a refusal, or the answers to a question (masked) |

A request is written when it is asked (`pending`) and completed when decided. A request
still pending when the API stops is closed as `interrupted` at the next start.

Not in the journal: the tools Claude Code approves without asking SOKKAN (the read-only
list of `agentchat.SAFE_TOOLS`, an agent's `auto_approve` rules). They are not approvals
anyone could have refused in the cockpit.

**Export** — instance admins (every project), a project's admins (their project):

```
GET /api/hitl/journal?project=<slug>&actor=<e-mail>&since=2027-01-12&until=2027-02-01&format=csv
```

`format=json` (default) or `csv`; `actor` = who decided or whose session it was; `since` is
inclusive, `until` exclusive (ISO date or epoch seconds); also `session`, `decision`. This
export carries e-mail addresses: it is the instance's own audit, not the researcher's.
Owner decisions also appear in Operate › Journal (`hitl.allow`, `hitl.deny`, `hitl.edit`,
`hitl.answer`, `hitl.mode_change`); delegated ones keep their `share.hitl.*` / Teams lines.

## 2. Turning study mode on

```
SOKKAN_FEATURE_STUDY_MODE=1
```

Whether a study is **active** is not decided on the instance: it is read from the inference
gateway with the `GET /usage` call the instance already makes, at most once a minute.

```json
{"study":  {"active": true, "study_id": "study-2027-a", "expires_at": "2027-04-30T23:59:59Z",
            "audit_required": true, "expired": false, "days_left": 170, "notice": false},
 "free":   true,
 "limits": {"tokens_day": 6000000, "tokens_month": 120000000, "deep_per_user_day": 20, "user_tokens_day": 0},
 "used":   {"tokens_day": 4960, "tokens_month": 4960, "user_tokens_day": 4960, "deep_user_day": 2}}
```

Every field is optional (an older gateway omits them). No `study` block = no study
(ceilings then come from `daily_quota_tokens` / `used_today` …, as before). A limit of 0 =
no ceiling. A gateway that does not answer keeps the last known state, end date included.
Past `expires_at`, or with `expired: true`, the study is over whatever `active` says.
`notice: true` (the end of the free period is near) shows « ends in N days » in the banner.
`used.user_tokens_day` / `used.deep_user_day` are the person's own figures: the instance
asks for them with that person's pseudonym and shows them in Setup › Study.

In return every inference call of the instance (sessions and Nina) carries
`x-sokkan-study: <study_id>` and `x-sokkan-user: <pseudonym>` — never the e-mail. With the
Claude CLI they go through `ANTHROPIC_CUSTOM_HEADERS`, one per line.

When the gateway refuses, the session says it in words, once per turn: in the technical
cockpit one sentence in English and in French next to the raw error; in the simple profile
the notice of that refusal in the person's own language (`no.<code>` of
`frontend/lib/simple/dict.ts`), the raw error folded under « technical detail ». No status
code, no variable, no header name in what the person reads — and never « send your request
again »:

| Gateway | The person reads |
|---|---|
| 410 `study_expired` | the free period of the study has ended; ask the administrator to extend it or switch to a paid plan |
| 412 `study_mode_required` | free use is tied to a study and this workspace is not in study mode; only the administrator can turn it back on, sending again will not help |
| 429 `user_daily_cap` | your personal ceiling for today is reached; it resets at midnight UTC, colleagues are not affected. **On an appliance** (sessions on the model of the box, the gateway asked for Deep only) the ceiling closes the stronger model alone: the sentence says to carry on right away with the usual model, without asking for the stronger one, which opens again tomorrow (`no.user_daily_cap_local` in the simple profile) |
| 429 `user_limit_reached` | the number of people who can use the assistant today is reached (`max_users_day` on the gateway) and you are not one of them yet; people already seen today are not affected; ask the administrator to raise the limit, sending again today will not help |

A 429 of the tenant itself (day or month ceiling, no code) keeps the generic message of a
429. On an appliance the local router adds `x-should-retry: false` to the four study refusals,
so the CLI asks once. The gateway sends that header itself from `sokkan-cloud` `study-no-retry`
(293573d) on; the one in production before it (bd8a8bc) does not, and **an instance whose
sessions call such a gateway directly (no local router) has no guard** for the two 429: the
CLI retries them by itself before the sentence appears (177 s of spinner on the real gateway,
11.11.2026; 1.4 s and no retry with the header, real CLI against the gateway's app). The
pilot's box goes through the router. The contract these sentences are tested against:
`tests/fixtures/gateway_contract/contract.json` (what the gateway of `study-no-retry` answered,
written by `regenerate.py` next to it), `tests/test_gateway_refusals.py`.

The CLI's own side calls (the title of a session, summaries — sent by the CLI, no tool
declared) never leave for Deep on an appliance: the title call carries the person's first
message, and with « /deep » in it one question used to count two escalations of the person's
day (`deep_per_user_day`).

An instance that is not on the gateway can declare a study itself:

| Variable | |
|---|---|
| `SOKKAN_STUDY_ID` | the study (letters, digits, `. _ -`) |
| `SOKKAN_STUDY_EXPIRES_AT` | ISO date; empty = no end |
| `SOKKAN_STUDY_AUDIT_REQUIRED` | `1` (default): no session without consent; `0`: consent is optional |
| `SOKKAN_STUDY_DB` | path of `study.db` (default: the data directory) |

**Give each person their own account first.** Local sign-in with the owner's token alone is
one identity: every volunteer would share a single pseudonym (Setup › Organization › Features
and Setup › Study say so). On an appliance, give each person a personal access code
(`scripts/appliance/accounts.sh add <e-mail> --name … --project …`, docs/appliance/README.md
§ 3): the warning goes away with the first code, and three people are P1, P2, P3.

**On an appliance** (sessions go through the local router): the study is read from the Deep
gateway (`SOKKAN_DEEP_URL` + `SOKKAN_DEEP_TOKEN`, `GET /usage`), the sessions send
`x-sokkan-study` and the pseudonym, and the router hands both to the gateway — an e-mail
address never leaves the box. The gateway's refusals (410 `study_expired`, 412
`study_mode_required`, 429 `user_daily_cap`, 429 `user_limit_reached`) come back as it said
them, in words, with `x-should-retry: false`.

## 3. What a participant sees

* A **banner** on every screen while the study is active: the study, its end date, « your
  actions are recorded under the pseudonym P2 », a link to what is recorded. It cannot be
  closed.
* **Setup › Study**, in French and in English:
  * *What is recorded*, in three columns — always / only if you tick the scenario / never;
  * *Your consent*: explicit, per person, before the first session (with `audit_required`,
    a turn is refused until then); the scenarios whose **content** the person agrees to
    share are ticked there and can be changed at any time; *Withdraw* stops the collection
    and takes everything collected under that consent out of the exports;
  * *My tasks*: for each session, the scenario (S1…S6), the reference time (how long the
    task takes without an agent), and two questions when it is done — was the agent useful
    (1 to 5), how many minutes were spent checking its work.

A session is tagged by the first request of a consenting participant; the person then
picks the scenario. A card can be tagged too (`PUT /api/board/card/{id}/study`): the
session opened from it starts with the card's scenario and reference time.

**In the simple profile** (feature `simple_profile`) the same things are said in its own
screens: a band above the content (study, end date, pseudonym), the dialog « What is
recorded » with the consent (opened from the band, and by itself when a first work is refused
for a missing consent — no work is created before the consent), the scenario under the title
of a work, and the two questions once its deliverable is validated. Each click on the approval
card (allow, refuse, correct then allow) is a row of the journal of validations.

## 4. What is collected, and what is not

| Collected (study.db) | Under | Never collected by the study |
|---|---|---|
| study, scenario, reference time of a session | pseudonym | the person's name or address outside the correspondence table |
| each request: start, duration, model that served it, token counts, **size** of the text and a keyed hash | pseudonym | the text of a request |
| the two answers | pseudonym | free comments |
| consent: version of the text, date, scenarios whose content is shared | pseudonym | |

`participants` (e-mail ↔ pseudonym) is the only table that holds an address. It is shown to
the instance's admins (each read is journaled) and is in no export. The validations
journal, the action journal, the memory's recall log and the transcripts exist with or
without a study; the export reads them at export time.

## 5. The researcher's package

`GET /api/study/export?format=zip|json&since=…&until=…`

| File | One line per | Holds |
|---|---|---|
| `manifest.json` | — | study, period, counts, mode (`metadata` or `metadata+content`), conventions |
| `participants.csv` | participant | pseudonym, consent date and version, scenarios whose content is shared |
| `sessions.csv` | tagged session | scenario, reference time, agent time, elapsed time, requests and follow-ups, tokens, tiers served, validations (asked / allowed / denied / edited / automatic, decision delay), corrections (= denied + edited), the two answers |
| `turns.csv` | request | start, duration, tier, tokens, error |
| `validations.csv` | approval | tool, decision, automatic or not and why, origin, who (pseudonym), asked at, decided at, delay |
| `sources.csv` | note recalled | when, through which path, the note's age in days when recalled, level, rank and score |
| `events.csv` | action of the journal about the session | action, who (pseudonym), when |
| `feedback.csv` | answered task | usefulness, checking time, reference time |
| `content/` | consented session | its exchanges (see below) |

Rules, enforced when the package is written:

1. **Pseudonyms only.** A participant is `P<n>`; anybody else is `non-participant`.
   Sessions, projects and notes are keyed hashes (HMAC with a secret of the instance):
   stable inside the study, not reversible by trying candidates.
2. **Metadata by default.** Every field that carries what a person typed or what a tool
   received — a session's title and first request, a tool call's title and input, a
   refusal's reason, a question's answers, a recall query, a note's name, an action's
   detail — leaves as `{chars, hmac}`: its size and a keyed hash.
3. **Content only where the person agreed.** For a session whose scenario its participant
   ticked: the requests, the agent's answers and the tool calls it proposed, with secrets
   masked and e-mail addresses replaced (a participant's by their pseudonym, any other by
   `[email]`). What a tool **returned** — the text of a file it read — is never exported:
   its size only.
4. **Only holding consents.** Sessions of a person whose consent holds, tagged since it
   was given. A withdrawal empties the next package of that person.
5. **A guard.** The finished package is searched for anything that looks like an e-mail
   address and for every person the instance knows; a hit refuses the export (HTTP 500,
   journaled `study.export.refused`).

Example (metadata mode, one validation and the session it belongs to):

```json
{"session": "s_acd52a2e866a", "participant": "P1", "scenario": "S2", "kind": "permission",
 "tool": "Write", "decision": "allow", "auto": false, "origin": "owner", "actor": "P1",
 "requested_at": "2027-01-14T09:11:37.489Z", "decided_at": "2027-01-14T09:11:38.704Z",
 "delay_ms": 1215, "edited": false,
 "title": {"chars": 25, "hmac": "30487b233a71b82e"},
 "input": {"chars": 58, "hmac": "56bc3fb896506800"}, "message": {"chars": 0, "hmac": ""}}

{"session": "s_acd52a2e866a", "participant": "P1", "scenario": "S2", "reference_minutes": 90,
 "turns": 2, "followup_prompts": 1, "tokens_in": 1800, "tokens_out": 400,
 "tiers_served": {"sokkan-ship": 2},
 "validations": {"asked": 1, "allowed": 1, "denied": 0, "edited": 0, "auto_allowed": 0,
                 "decision_delay": {"count": 1, "median_ms": 1215}},
 "corrections": 0, "feedback": {"useful": 4, "verify_minutes": 25}, "content_shared": false}
```

## 6. Procedure for the researcher

1. An instance admin opens Setup › Study › *Read-only export tokens*, creates a token
   (« researcher, university », 30 days) and sends it through a private channel. The token is shown
   once; the instance keeps its hash.
2. The researcher downloads the package — the token opens this route and nothing else, in
   read only:

   ```
   curl -H "Authorization: Bearer skx_…" "https://<instance>/api/study/export?format=zip" -o sokkan-study.zip
   curl -H "Authorization: Bearer skx_…" "https://<instance>/api/study/export?format=zip&since=2027-01-12&until=2027-01-19" -o week-03.zip
   ```
3. Each export is journaled (`study.export`, with its counts and the SHA-256 of its
   payload) and listed in Setup › Study. The admin revokes the token at the end of the
   study.

To link an interview to the traces, the participant tells the researcher their own
pseudonym (it is on their banner). Nobody else can make that link from the package.

## 7. Limits

* The gateway side (`study`, `limits`, `used`, the refusals) is a separate delivery
  (sokkan-cloud, `infer/pilote-etude.md`): the instance is tested against its documented
  answers and against a local HTTP gateway, not yet against the production one.
* Ceilings are shown here and enforced by the gateway.
* Tier served = the model name the Claude CLI reports for the turn. What the gateway sends
  back about the routing — headers `x-sokkan-route`, `x-sokkan-tier`, `x-sokkan-escalation`,
  `x-sokkan-judge`, the `sokkan` field of the answer — is **not captured**: the Claude CLI
  that makes the call drops them. The reason of an escalation and the judge's opinion are
  in the gateway's own study export (one line per request); joining the two exports needs
  the gateway to key its lines on the identifier it received (the instance's `P<n>`).
* `sources.csv` needs the memory store (Postgres); with the 2.x SQLite index there is no
  recall log and the manifest says so.
* An unattended agent run is not gated by consent and its turns are not study turns; its
  approvals are in the validations journal like any other.
* Retention of the study tables after the study is the protocol's decision: nothing is
  purged automatically.
