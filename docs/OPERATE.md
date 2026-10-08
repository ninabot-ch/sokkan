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
