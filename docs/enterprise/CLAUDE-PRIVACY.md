# Claude and confidentiality

« Write with Claude » (feature `claude_tier`) lets a person of the Atelier have one piece of work
written by Claude, through the Anthropic API and **the organisation's own API key**. Everything
else runs on the model of the machine. This page says what may leave, under which contract, and
what SOKKAN does to the personal data in it (the « PII sandwich », 4.0.1).

## 1. What may go to Claude, and what never does

| Rule | Where it is enforced |
|---|---|
| Only on an explicit gesture: the person picks « Write with Claude » when sending the work. Never by keyword, size, failure of the local model or fall-back. | instance (`claudetier.gate`) and router (`x-sokkan-tier: claude`) |
| **D9 — a confidential project never goes to Claude.** A project classified `confidential` or `restricted` is refused, whatever `SOKKAN_CLAUDE_MAX_LEVEL` says (hard ceiling `HARD_MAX` = `project`). By default only `public` and `team` projects may use Claude. | instance (`409 claude_level` before anything starts) and router (asks the api at every request; a project reclassified during a work is refused at its next request) |
| A document or a memory note classified above what Claude may receive is not sent with the work. | instance (`409 claude_document`, recall capped) |
| **No job is refused Claude any more** (4.0.1, decision of 18.11.2026). The `409 claude_role` of the job kits (HR, legal, finance) is gone: the kits of HR and legal set up *confidential* projects, so D9 keeps them on the machine; any other work is decided by its project's level and by the person, after the PII check below. | — |
| Personal data found in the request or its attachments is shown to the person **before anything leaves**; without their agreement the work stays on the local model. | instance (`409 claude_pii`) |
| Every request of a Claude work is pseudonymised before it leaves, and nothing is sent when that cannot be done. | router + api (`/api/internal/claude-pii`) |

## 2. The « PII sandwich »

```
person ──► SOKKAN (machine) ──────────────────► Anthropic API
           1. find personal data                 sees « [PERSONNE_1] », « [IBAN_1] » …
           2. replace by stable pseudonyms  ───►  writes from the pseudonyms
           3. put the values back  ◄──────────────  answer, tool inputs (files to write)
person ◄── answer and files with the real values
```

**What is found** (deterministic rules, no model, on the machine — `backend/pii.py`):

Every Unicode blank (no-break space, thin space of Word and PDF) and dash counts as a plain one.

| Category | Pseudonym | Rule |
|---|---|---|
| names of people | `[PERSONNE_n]` | the people of the organisation profile (directory, signatories — Settings › Organisation) and the name the person at work is shown under, in any case; a name after a civility (« Mme », « M. », « Herr », « Dr », also « madame dupont » in lower case) or after a field label (« Nom : DUPONT Jean » — the line up to its separator); every cell of a column whose header names a person (CSV, Excel as text, Markdown table: « Nom », « Prénom », « Name »…); each word of a name met that way (surname, first name) is then found alone too |
| IBAN | `[IBAN_n]` | an IBAN of any country, at its ISO length, mod-97 checked, any case, separated by spaces, no-break spaces or dashes |
| AVS/AHV numbers | `[AVS_n]` | `756` + 10 digits, separated by dots, spaces or dashes — even with a wrong check digit (a typo is still somebody's number) |
| e-mail addresses | `[EMAIL_n]` | |
| phone numbers | `[TELEPHONE_n]` | national (`079 123 45 67`, `079 123 4567`, `079/123 45 67`, `022 / 345 67 89`, `06 12 34 56 78`) and international (`+41`, `0041`, `+41 (0)79 …`, `+33`, `+49`, `+39`, `+43`, `+423`) |
| dates of birth | `[DATE_NAISSANCE_n]` | a date after « né(e) le », « date de naissance », « born », « date of birth », « geboren », « Geburtsdatum » …, on the same line or the next, two- or four-digit year; every cell of a « Naissance » column |
| postal addresses | `[ADRESSE_n]` | a street with its number (`Rue de la Gare 12`, `12, rue des Alpes`, `Musterstrasse 5`), with « NPA ville » when it follows; every cell of an « Adresse » column |

Two different values never share a pseudonym: a national number keeps the shape of its groups
(`061 234 56 78` in Basel is not `06 12 34 56 78` in Lyon), an international one its country. One
value written twice the same way, or an IBAN with and without spaces, keeps one pseudonym.

## Limits — what this does NOT protect

* **Detection is deterministic, not a named-entity model.** A name written alone in running text
  that the directory does not know (« Rédige un avertissement pour Sophie … »), a company name, a
  salary, a diagnosis, a disciplinary fact or any other sensitive detail in free text is **not**
  replaced. The card says what WAS found, never that the text is « protected ».
* **A work about people asks anyway.** For the templates of a job marked `people` in its kit (HR,
  executive assistant, customer support, sales) and in a project set up for such a job, the card
  is shown even when nothing was spotted — « rien de repéré ne veut pas dire rien de personnel » —
  with the same two choices.
* **What the agent reads during the work is pseudonymised if it is detected, WITHOUT a new card.**
  The card covers the request and its attachments at the moment of sending; a document, a note or
  a tool result read later goes through the same detection, and what it does not detect leaves.
* **An administrator who lowers a project's level removes D9 for it.** An HR project reclassified
  `team` has nothing left but this detection between its data and Claude; lowering a level asks
  for a reason and is journaled (`project.level`), but it is not refused.
* **A document's own level is checked when it is ATTACHED, not when it is READ (F5, known).** A
  `confidential` document stored in a `team` project is refused as an attachment of a Claude work
  (`claude_document`), but the agent can still open it by its path during the work, and then only
  the detection above applies. Keep confidential documents in confidential projects; a check at
  read time (the file tools' hook) is planned.

**The card.** When the person chooses « Write with Claude » and the request or an attachment
holds personal data, `POST /api/simple/start` answers `409 claude_pii` and creates nothing:

```json
{"code": "claude_pii", "message": "Votre demande ou vos pièces jointes contiennent des données personnelles. …",
 "pii": {"total": 3, "step": "start", "choices": ["anonymize", "local"],
         "categories": [{"category": "person", "label": "noms de personnes", "count": 2, "examples": ["C. E.", "P. F."]},
                        {"category": "iban", "label": "IBAN", "count": 1, "examples": ["CH93•••57"]}]}}
```

Categories, counts and a masked hint — never a value. The Atelier shows it as a card with two
choices, in French or English:

* **« Anonymiser et envoyer à Claude »** → the same start with `"pii": "anonymize"`;
* **« Rester sur le modèle local »** → the same start without Claude, with `"pii": "local"`.

A follow-up message of a Claude work is checked the same way until the person has agreed once
for that work (`"pii": "anonymize"` on `POST …/message`); its second choice is « Ne pas envoyer »
(`POST /api/simple/sessions/{sid}/pii-decline`).

**The journal.** Every choice is a row of the journal of validations (kind `pii`, decision
`anonymize` | `local` | `declined`, who, when, which categories and how many — never a value) and
an action of the journal (`claude.pii.anonymize|local|declined`).

**Where the sandwich is made.** Not only around the person's text: on **every request** of a
Claude work, in the router. The router hands the request to the api
(`POST /api/internal/claude-pii`, the router's shared secret, 404 for anything else and for a
request that came through the reverse proxy), which pseudonymises the system prompt, the
messages, the tools' results and inputs with the work's mapping, and returns the copy with the
table to restore the answer. So what the agent reads by itself during the work (a document, a
memory note, the organisation profile) is covered too. The router then puts the values back in
the answer — streamed or not, text and tool inputs, a pseudonym cut between two pieces of the
stream included — so the person reads, and the agent writes into files, the real values.

* The mapping belongs to the work and is stable: the same value keeps its pseudonym for the
  whole conversation (Anthropic's prompt cache keeps working). It is kept in the instance's
  database (`simple.db`, table `pii_maps`), **encrypted with the instance's data key** (the vault's),
  deleted with the work (`simple.forget`) and purged `SOKKAN_CLAUDE_PII_RETENTION_DAYS` days
  (default 30; `0` = kept until the work is forgotten) after its last use; it never leaves the
  machine.
* Only the session of the work can ask for its sandwich: the session carries
  `x-sokkan-work-token` = HMAC(router secret, work | project), checked by the api — an agent of
  another work naming this one gets a 404.
* A pseudonym already written in what comes in (a third party's mail that says « [IBAN_1] ») is
  defused before it goes (`[IBAN-1]`): only the pseudonyms the work's table issued are ever
  restored.
* An image or a PDF sent as bytes cannot be checked: it is replaced by a sentence saying it was
  not sent. Claude's thinking blocks are left as Claude wrote them (signed, written from
  pseudonyms).
* The api cannot be asked, the work is unknown, the answer is malformed → the router refuses
  (`claude_unavailable`, `x-should-retry: false`). A request is never sent without its sandwich.

## 3. The contract with Anthropic (public documentation, read on 18.11.2026)

SOKKAN calls the Anthropic API with an API key of the organisation, created in the Anthropic
Console. What the public legal pages say about that use:

* **Commercial Terms of Service** — <https://www.anthropic.com/legal/commercial-terms>
  (page « Effective Jun 17, 2025 »), opening paragraph:
  > « They govern Customer’s use of Anthropic API keys and any other Anthropic offerings that
  > references these Terms, as well as all related Anthropic tools, documentation and services
  > (the “Services”). »

  The same paragraph: « “Anthropic” means Anthropic Ireland, Limited if Customer resides in the
  European Economic Area (“EEA”), Switzerland or UK, and Anthropic, PBC if Customer resides
  anywhere else. » And: « Our consumer offerings (e.g., Claude.ai) are governed by our Consumer
  Terms of Service instead. »
* **The DPA is part of those terms** — same page, **section C « Data Privacy »**:
  > « Data submitted through the Services will be processed in accordance with the Anthropic Data
  > Processing Addendum (“DPA”), which is incorporated into these Terms by reference. »

  Section **M.9 « Integration »** lists the DPA among the documents incorporated by reference.
  Section **B « Customer Content »**: « Anthropic may not train models on Customer Content from
  Services. »
* **Data Processing Addendum** — <https://www.anthropic.com/legal/data-processing-addendum>
  (page « Effective Feb 24, 2025 »), first paragraph:
  > « This Data Processing Addendum (“DPA”) is incorporated into and forms part of the Anthropic
  > Commercial Terms of Service or other agreement between Customer and Anthropic that references
  > this DPA and governs Customer’s use of the Services (the “Agreement”), and applies to
  > Anthropic’s processing of Customer Data (defined below). »

  Swiss data: **Schedule 3 « International Data Transfers », section C « Swiss Addendum »**:
  « This Swiss Addendum will apply to any processing of Customer Personal Data that is subject to
  Swiss Data Protection Laws (as defined below) or to both Swiss Data Protection Laws and the
  GDPR. »

What SOKKAN does not claim: these quotes are the public pages on the day above, not the
customer's own agreement. Whether a given organisation's account is under them, which entity it
contracted with, and any retention or zero-retention arrangement are facts of that account.

**To confirm by the administrator, in the Anthropic Console** (the organisation that owns the key
entered in Settings › Claude): in the organisation's settings (Settings › Organization, or its
legal / terms page), that the organisation accepted the Commercial Terms of Service (not a
consumer account), the country of the organisation (which decides the Anthropic entity: Ireland
for Switzerland and the EEA), and any separate agreement (a signed DPA or a data-retention
arrangement) that would replace the online terms.

## 4. Reference

| | |
|---|---|
| `POST /api/simple/start` | `tier: "claude"`, `pii: "" \| "anonymize" \| "local"` → `409 claude_pii` (card) · `409 claude_level` (D9) · `409 claude_document` |
| `POST /api/simple/sessions/{sid}/message` | `pii: "" \| "anonymize"` → `409 claude_pii` on a Claude work not yet agreed |
| `POST /api/simple/sessions/{sid}/pii-decline` | `{counts}` — journal of « Ne pas envoyer » |
| `POST /api/internal/claude-pii` | the router only: `{work, project, token, payload}` → `{payload, restore, found}` |
| headers `x-sokkan-work`, `x-sokkan-work-token` | set on a Claude work's session; the router strips every `x-sokkan-*` header before Anthropic |
| `SOKKAN_CLAUDE_PII_RETENTION_DAYS` | days a work's mapping is kept after its last use (default 30, `0` = until the work is forgotten) |
| journal | kind `engine` (the choice of Claude), kind `pii` (the choice about personal data) |
| code | `backend/pii.py` (detection, mapping, stream), `backend/claudepii.py` (card, mapping of a work), `backend/shiprouter.py` (`_claude`), `frontend/components/simple/PiiCard.tsx` |

See also: [the appliance's « Write with Claude »](../appliance/README.md), [job kits](ROLES.md).
