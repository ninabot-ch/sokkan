# Operate your production with agents

SOKKAN doesn't stop at `git push`. The **Operate** tab and the surrounding
plumbing close the loop: **idea → prod → observation → idea**, run by agents that
share your project memory, with you at the helm on every transition. This is what
an editor-only tool can't do — it has neither your production nor your
operational memory.

Everything here is opt-in and degrades gracefully: on self-hosted you wire the
URLs yourself; on managed cloud a single fleet resource provisions the stack.

---

## Observability (Operate › Incidents)

Connect a Prometheus + Grafana + Loki stack and the **Operate** tab appears:
your dashboards, a live incident feed, and your runbooks.

- **Managed cloud**: add the **Observability** resource in *Infra → My fleet*. A
  dedicated VM boots the stack in your private network, Grafana is exposed at
  `grafana-<you>.sokkan.ch`, and your cockpit is wired automatically.
- **Self-hosted**: point the cockpit at your own stack —
  `SOKKAN_PROM`, `SOKKAN_LOKI`, `SOKKAN_GRAFANA_URL`, `SOKKAN_GRAFANA_PUBLIC_URL`,
  `SOKKAN_GRAFANA_USER`/`SOKKAN_GRAFANA_PASSWORD`.

### Sessions read and write it

The bundled `sokkan-observability` MCP server lets any session:

- `query_metrics(promql)` / `query_logs(logql)` — investigate with the project
  memory as context (auto-approved, read-only);
- `create_dashboard(title, panels)` — « build me a dashboard for API p95 latency
  and 5xx rate » → the agent knows your topology and composes it (gated by an
  approval click, since it writes).

### Alerts become supervised incidents

Point your Grafana alerting contact point at
`POST /api/observability/alert` (Bearer `SOKKAN_OBS_ALERT_TOKEN`). When an alert
fires, SOKKAN:

1. records an **incident** (visible in Operate › Incidents),
2. **spawns a diagnosis session** pre-seeded with the metric, the labels, and the
   instruction to search memory, investigate, and propose a fix — **waiting for
   your go-ahead before touching anything**,
3. pings you (see *HITL push*).

Resolve the incident when done; ask the agent to write a short post-mortem note
to memory, and the next occurrence starts smarter.

Since 3.5 these external alerts also appear in **Operate › Alerts**, filed under a read-only
rule per `alertname` (source « External alerts »): firing, then resolved when Grafana says so.

---

## Alerts (Operate › Alerts, 3.5)

SOKKAN evaluates alert rules itself — in the spirit of ElastAlert, without YAML: you build the
rule in a form, **see on a graph what it would have done over the last day or week**, then turn
it on. Feature `alerting` (on by default in both editions; it does nothing until a rule exists).

### Sources

| Source | Where it comes from | Rule types |
|---|---|---|
| Prometheus | `SOKKAN_PROM` (built in), or added in Alerts › Sources | threshold, spike, absence, anomaly |
| Loki | `SOKKAN_LOKI` (built in), or added | threshold (count per window), any, frequency, spike, flatline, change, new term, cardinality |
| Elasticsearch / OpenSearch | added in Sources: URL, index pattern, time field, auth none / basic / API key / bearer | same as Loki |
| SOKKAN events | always there: the project's audit journal (`audit action=…`), failed agent runs (`agents.failed`) | same as Loki |
| External alerts | Grafana or any webhook posting to `/api/observability/alert` | read-only |

A source's secret (password, API key) is encrypted with the vault data key and never shown
again. « Test » says what answered (« Prometheus 2.55.1 — 1 717 metrics », « elasticsearch 8.15 —
4 210 documents in logs-* ») or why it did not. A source that fails puts its rules in `error`
(visible, journaled as `alerting.rule.error`); the other rules keep running.

### Writing a rule without writing a query

The builder turns a form into the query: Prometheus — metric (autocomplete), label filters,
aggregation (rate, increase, avg, max, min, sum, count, last, p95), range, « by », optionally
« as a share of » another metric (a percentage, e.g. 5xx / all requests); Loki — label filters,
« contains », level; Elasticsearch — field filters (=, ≠, exists, contains) and free text; SOKKAN
— stream and field filters. Experts switch to **raw** (PromQL, LogQL, Lucene query_string, or a
JSON query DSL starting with `{`). Every rule reads back as a sentence: « Alert when the rate of
http_requests_total (status=~5..) as a share of http_requests_total (%) is above 2, by job, for
5 minutes ».

### Rule types

| Type | Fires when | Parameters |
|---|---|---|
| threshold | the value (or the number of events in `window`) crosses a limit | `op` (> ≥ < ≤ =), `value`, `reduce` over `window` (last, avg, max, min, sum) |
| any | any matching event | — |
| frequency | at least `count` events within `window` | `count`, `window` |
| flatline | fewer than `count` events within `window` (« logs stopped ») | `count`, `window` |
| spike | the last `window` is `ratio`× the level of the `reference` before it (up, down or both), above `min_count` | `ratio`, `direction`, `window`, `reference`, `min_count` |
| change | a field changes value for the same key (e.g. `status` per `host`) | `field`, `key_field` |
| new_term | a field takes a value never seen in `lookback` (a new error type) | `field`, `lookback` |
| cardinality | the number of distinct values of a field crosses a limit | `field`, `op`, `value`, `window` |
| absence | a metric sends no data for `window` (an exporter died) | `window` |
| anomaly | the value is more than `z` standard deviations from its usual level | `z`, `lookback` |

Common settings: `every` (how often, ≥ 15 s), `for` (how long the condition must hold before it
fires — a pending alert that clears never rings), `group_by` (one alert per host, job…),
`realert` (remind every… while it fires; 0 = once), quiet hours (time zone, days), severity,
labels, runbook link, classification level (3.4: a rule above a person's clearance does not
exist for them).

### The backtest

`POST /api/alerting/preview` evaluates an unsaved rule over 1 h, 6 h, 24 h or 7 days with **the
same code as the live loop** and returns the series, the threshold (or the spike reference /
anomaly band), the intervals where it would have fired once `for` is applied, and sample events.
The cockpit draws it; the person moves the threshold and sees « it would have rung 3 times ».

### Templates

16 ready-made rules, each saying whether this instance has what it needs: 5xx share, slow
responses (p95), host CPU / memory / disk (node_exporter), service down (`up`), a metric that
stopped, TLS certificate expiry (blackbox_exporter), errors in the logs, logs stopped, burst of
logs, a new kind of error, repeated failed logins, a failed agent run, refused sign-ins, project
budget reached. Unavailable ones say why (« No HTTP metrics found (needs http_requests_total) »).

### States, notifications, silences

`ok → pending (condition true, less than for) → firing → resolved`. A firing alert notifies
the rule's channels once, again every `realert` until someone **acks** it, and once when it
resolves. **Silences** (a rule and/or label matchers, a time window) and **quiet hours** mute
notifications, never the state: the cockpit always shows what is true.

Channels (Alerts › Channels, maintainer): **Telegram**, **Microsoft Teams** (a channel mapped
to the project; the card has Ack, Silence 1 h and Open incident — the person acts as
themselves, with their role in the project), **Slack** (incoming webhook), **e-mail** (the
instance's SMTP: `SOKKAN_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM`), **webhook** (JSON
`{event, alert, rule, link}`, header `X-Sokkan-Signature: sha256=<HMAC of the body>` when a
secret is set), **PagerDuty** (Events API v2, resolve included). « Instance notifications »
(Setup › Notifications) is available as a channel too. « Test » sends a « [TEST] » message.

### Actions — event-driven, human-gated

When a rule fires it can: open an **incident** (Operate › Incidents); start a **diagnosis
session** that searches the memory, investigates and waits for a go-ahead before applying
anything; start the run of an **alert agent** (an agent approved in Crew with an « alert »
trigger — its writes are held for approval). Agents subscribed to `alert:<rule name>` run too.
Nothing else executes on its own.

### Who does what

viewer: reads rules, alerts, history · dev: writes their rules, acks, silences their rules,
proposes an agent run · maintainer / admin: every rule of the project, sources, channels,
project-wide silences. Every write is in the journal (`alerting.*`). A session proposes a rule
through MCP (`alerting_propose_rule`): saved disabled, a person enables it after the backtest;
`alerting_list_rules`, `alerting_list_alerts` and `alerting_preview` are read-only.

### Running it

The loop runs in the api (`SOKKAN_ALERTING_TICK_S`, 15 s); one evaluator per data directory
holds a lease in `alerting.db`, so several api replicas never ring twice
(`SOKKAN_ALERTING_EVALUATOR=0` keeps a process out of it). Source calls time out after
`SOKKAN_ALERTING_SOURCE_TIMEOUT_S` (10 s). Everything lives in `$SOKKAN_DATA_DIR/alerting.db`
(backed up with the data directory).

API: `GET/POST /api/alerting/sources`, `…/sources/{id}/test`, `…/sources/{id}/suggest`,
`GET /api/alerting/templates`, `POST …/templates/{id}/apply`, `GET/POST /api/alerting/rules`,
`GET/PUT/DELETE …/rules/{id}`, `…/enable`, `…/disable`, `…/evaluate`, `…/test-notify`,
`…/history`, `POST /api/alerting/preview`, `GET /api/alerting/alerts`, `…/alerts/{id}/ack`,
`…/silence`, `…/open-incident`, `…/propose-agent`, `GET/POST/DELETE /api/alerting/silences`,
`GET/POST/PUT/DELETE /api/alerting/channels`, `…/channels/{id}/test`, `GET /api/alerting/status`.
The project is the header `x-sokkan-project` (or `?project=`), like every project route.

---

## Secrets (Setup › Secrets)

Operating prod means keys. The vault stores secrets **encrypted at rest**
(per-instance Fernet key) and injects them into every session as environment
variables. Your agent uses `$STRIPE_KEY` to deploy or call an API — but the value
never appears in the UI, the audit log, or the prompt sent to the model
(CI/CD-style). Secrets never leave the instance. Admin-only; names are shown,
values never returned.

---

## HITL push (Setup › Notifications)

You launch nine sessions and step away; one hits a permission gate. Instead of
blocking silently, SOKKAN pings you (Telegram or a generic webhook) after the
request stays pending for ~25s, with a link to come click. Answer in time and no
ping is sent. This is also the channel production alerts fan out to. Configure
and test it in *Setup › Notifications*.

---

## Runbooks

A runbook is a memory note named `runbook-*` — your agents write them as they
operate, same pipeline as the rest of memory. In Operate › Incidents, **Run** spawns
a session guided by the runbook, with the project memory, executing step by step
and stopping for your approval on anything irreversible. Ops becomes reproducible
and supervised, not tribal knowledge.

---

## Deploy & rollback (managed cloud)

Deploy a Docker image to one of your fleet workers and roll back to the previous
tag in one click — the same versioned, human-gated pattern SOKKAN uses to update
itself, applied to your apps. From the fleet view (admin).

---

## Costs — what is billed, and how it is computed (3.2.3)

Operate › Costs reads the Claude Code transcripts of the instance and says, for every
figure, **which billing basis** it is on and **how** it was computed:

| Basis | When | Billed |
|---|---|---|
| Claude API (API key) | an API key in Setup › Engines or `ANTHROPIC_API_KEY` | tokens × the public price of the model |
| Claude subscription | a Pro/Max login (CLI login, setup-token) | nothing per token — the API-equivalent cost is shown apart, labelled |
| SOKKAN Inference | `sokkan-*` tiers through the gateway | tokens × the tier price; the gateway ledger (CHF) when it answers |
| Local engines | Magnitude, or an engine served on your hardware | 0 — tokens only |
| Other endpoint | any other Anthropic-compatible endpoint | `SOKKAN_MODEL_PRICES`, else tokens only |

- **Prices** come from `backend/model_prices.json`, a versioned copy of the public Claude
  price list (input, cache write ×1.25 for 5 min / ×2 for 1 h, cache read, output — per
  model). A model that is not in the table is counted in tokens and flagged; it is never
  priced at a guessed tariff. Add or override models with `SOKKAN_CLAUDE_PRICES` (a JSON
  file of the same shape).
- **One API message is counted once.** Claude Code writes one transcript line per content
  block (thinking, text, tool call) and repeats the usage of the whole message on each;
  SOKKAN deduplicates them by message id. Sub-agent transcripts count in their session.
- **Only SOKKAN sessions are counted.** The workspace folder can also hold transcripts of
  Claude Code used directly in the same directory; they are reported on one line ("not
  counted") — `SOKKAN_USAGE_EXTERNAL=include` counts them.
- `SOKKAN_BILLING_BASIS=api|subscription` forces the Claude basis when the instance cannot
  tell (the basis applies to the whole history: a transcript does not record how it was
  authenticated).
- **Budgets** (per project, daily notice) count the billed cost, and on a subscription the
  API-equivalent — a usage brake, since nothing is billed per token there.
