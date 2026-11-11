# Pilot candidate against the REAL gateway — journey and refusals seen on screen (11.11.2026)

> The journey below was played on `b2ce8bd` and is left as it was seen. Defects 2, 3 and 5 were
> worked on afterwards (same day): their state is under « Defects found ».

One journey played in a real browser (Chrome, 1919 × 992) on a throw-away instance of
`pilot-integration` (`b2ce8bd`, then `b2ce8bd` + the fix below), plugged on the production
gateway `https://infer.sokkan.ch` with the token of the test tenant `pilote-essai-20261111`
(study `essai-icebreaker`, audited use required, 20 000 tokens per person and per day, 2 Deep
escalations per person and per day, 3 distinct people per day). People, organisation and
document are fictional. The instance was removed afterwards; nothing was changed on the gateway.

**Setting** — the pilot's: appliance profile, named accounts in local sign-in, simple profile,
study mode on, sessions → local router → the box's model (`qwen3.8-27b`, vLLM) and Deep →
the gateway. The study (name, end date, ceilings) was read from the gateway (`GET /usage`), and
the gateway was reached by its public URL from the router and from the api, without any trick.
A second, shorter pass was played with the sessions calling the gateway directly (« included »
inference, tier Ship).

**Why the whole journey did not go through the gateway's Ship tier**: one request of a session
is ≈ 29 000 tokens (the journey below: 7 requests, 202 757 tokens in). With 20 000 tokens per
person and per day the gateway serves ONE request per person, then refuses. A work of one
person needs a ceiling well above 200 000 tokens a day (starting value proposed for the pilot:
no token ceiling per person, 6 M per day for the tenant).

## What was played

| | Seen on screen | Capture |
|---|---|---|
| Study announced from the gateway, consent, pseudonym P1 | yes — « Étude essai-icebreaker en cours jusqu'au 31 décembre 2026 » | `01-…` (English, before the language was chosen) |
| Nominal journey (Camille, P1): a follow-up e-mail from a short quote (1 txt document) → approval card → deliverable → validated | yes. **First draft 60.3 s after « Lancer »**; deliverable ready 10 s later (the time to read and click « Autoriser »); turn ended at 88 s. Served by the **Ship** tier: 7 requests `ship:default`, all 200, in the router; `qwen3.8-27b` in the session and in `study.db`; journal: `Write`, `allow`, actor Camille, 10.3 s to decide | `02-…`, `03-…`, `04-…` |
| A Deep escalation answered by the real gateway (same person, « … /deep ») | yes — approval card of a « v2 » written by `sokkan-deep`; on the gateway P1 went from 221 to 35 350 tokens, `deep_user_day` 1 | `06-…` |
| **429 `user_daily_cap`** (her next request, after « Autoriser ») | yes, 0.3 s after the click, **1 request, no retry** | `07-…` (fr), `08-…` (en, technical detail open) |
| A third identity admitted (Marc, P3 — P1 and P2 were the two identities already used on the tenant: the gateway ignores case, the instance's pseudonyms `P1`, `P2` ARE its `p1`, `p2`) | yes — answered by Deep | `09-…` |
| **429 `user_limit_reached`** (Inès, P4, the fourth identity) | yes, 2.4 s after « Lancer », **2 requests** (the CLI's session-title call and the request itself — two different calls, one error line, no retry); sent again once: 2 requests again, a second notice | `10-…` (fr), `11-…` (en) |
| **412 `study_mode_required`** (same instance restarted with `SOKKAN_FEATURE_STUDY_MODE=0`) | yes, 3.6 s after « Lancer », **2 requests** (same two calls), no retry | `12-…` (fr), `13-…` (en) |
| Direct inference (sessions → gateway, no local router), Léa, P2: same request as Camille | first request served by the gateway's **Ship** tier (`sokkan/sokkan-ship` in the session, 45 s for the first tool call); the second is refused (`user_daily_cap`) and the person watches a spinner for **177 s** before the notice (224.7 s after « Lancer »): the CLI retries a 429 by itself, the gateway sends no `x-should-retry` | `14-…` |
| **410 `study_expired`** | **not played** (would need to change the tenant) | — |

Exact texts shown (French / English):

* `user_daily_cap` — « Vous avez atteint votre limite d'utilisation pour aujourd'hui. Votre travail
  est conservé et vous pourrez continuer demain (la limite repart à minuit, heure universelle). Vos
  collègues ne sont pas concernés. » / « You have reached your usage limit for today. Your work is
  kept and you can carry on tomorrow (the limit resets at midnight, universal time). Your colleagues
  are not affected. » — technical detail: `API Error: Request rejected (429) · personal daily
  inference cap reached for this user in this environment — resets at midnight UTC (other users
  are not affected)`
* `user_limit_reached` — « Le nombre de personnes qui peuvent utiliser l'agent aujourd'hui est
  atteint, et vous n'en faites pas encore partie. Votre travail est conservé. Renvoyer la demande
  aujourd'hui ne changera rien : demandez à la personne qui administre SOKKAN de relever cette
  limite. » / « The number of people who can use the agent today is reached, and you are not one of
  them yet. Your work is kept. Sending the request again today will not help: ask the person who
  administers SOKKAN to raise this limit. » — technical detail: `API Error: Request rejected (429) ·
  too many distinct users today for this environment (limit: 3 per day) — users already seen today
  are not affected; ask your SOKKAN administrator to raise the limit. Nothing was sent to a model.`
* `study_mode_required` — « L'agent ne peut pas répondre pour l'instant : son usage gratuit est lié
  à l'étude, et cet espace n'est pas en mode étude. Votre travail est conservé. Renvoyer la demande
  ne changera rien : prévenez la personne qui administre SOKKAN. » / « The agent cannot answer for
  now: its free use is tied to the study, and this workspace is not in study mode. Your work is
  kept. Sending the request again will not help: tell the person who administers SOKKAN. » —
  technical detail: `API Error: 412 Free inference for this environment is tied to a study and is
  only served while the instance runs in study mode (audited). Turn study mode on in the instance
  settings, or contact your SOKKAN administrator. Nothing was sent to a model.`

Requests counted in the local router (its access log and `/stats`): 7 `ship:default` (200) and
11 `deep:keyword` — 3 answered (200), 6 refused 429, 2 refused 412. On the gateway
at the end: 111 810 of the tenant's 300 000 tokens of the day.

## Defects found

1. **Fixed** — a later message that BEGINS with the escalation keyword (« /deep Propose-moi… »)
   reached the CLI as a slash command: the person read « Unknown command: /deep », in English,
   and nothing went to a model (`05-…`). The first message of a work was not concerned (the
   brief comes first). Now the brief comes first for any later message that begins with « / »
   (`backend/simple.py`, test in `tests/test_simple_profile.py`, red on the old code). Replayed
   on the instance: the same message reaches the gateway (`15-…`).
2. **Fixed on a branch of the gateway, not deployed** — without the local router a 429 of the
   study is retried by the CLI: 177 s of spinner before the notice (`14-…`). Not the pilot's
   setting. `sokkan-cloud` `study-no-retry` (293573d): `x-should-retry: false` on the four study
   refusals, the other 429 unchanged. Real CLI against the gateway's app on a throw-away
   database: 189 s and 22 requests before (11 tries of each of the two calls), 1.4 s and the
   two calls, no retry, after. Not replayed on
   `infer.sokkan.ch` (production still runs bd8a8bc).
3. **Fixed** — the CLI's session-title call carries the person's words: with « /deep » in
   the first message it was escalated too. One question of Marc counted **2** Deep escalations
   (his whole day, `deep_per_user_day` 2) and 39 113 tokens. The local router now keeps the
   CLI's side calls (no tool declared) on the local model (`backend/shiprouter.side_call`,
   `tests/test_shiprouter.py` on the call the CLI really sent). Real CLI through the router,
   two recording upstreams: 2 calls to Deep before, 1 after. Not replayed on the real gateway.
4. **Not fixed** — after a restart of the api, a work reopens with an empty thread (the
   deliverable and its validation are kept; a page reload alone keeps the thread and its notice).
5. **Wording fixed, the rest not** — on an appliance `user_daily_cap` only closes Deep: the
   person can go on with the box's model at once, the notice said « tomorrow ». On an appliance
   the notice is now `no.user_daily_cap_local` (« … vous pouvez continuer tout de suite avec le
   modèle habituel … » / « … you can carry on right away with the usual model … »); tests only,
   not looked at in a browser. Still there: the header of the work stays « Livrable prêt » after
   a refusal, and the entry under « Récents » kept « Unknown command: /deep ».
6. **Not fixed, study data** — a turn that ends on a refusal is recorded with the model
   `<synthetic>` in `study.db`, even when Deep answered first (turn 3 of Camille); the
   « Unknown command » exchange is a turn too.
7. Seen, not investigated: the band said « 1 travail attend vos deux réponses » to Léa, who had
   no validated work; the model answered Marc in English (his interface was in English); a typo
   of the local model in a French sentence (« téléport »).

## Not played

410 `study_expired`; the tenant-wide 429 and the 402; a Deep escalation triggered by repeated
tool failures or by the local model being down; the judge and the `nokker` mode (off on the
gateway); HTTPS on the LAN and the installer (the instance was a plain compose project on the
build host, port bound to the tailnet); the number of retries of the direct pass (no router
between the session and the gateway to count them: only the duration was measured).
