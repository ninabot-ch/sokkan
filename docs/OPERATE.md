# Operate your production with agents

SOKKAN doesn't stop at `git push`. The **Operate** tab and the surrounding
plumbing close the loop: **idea → prod → observation → idea**, run by agents that
share your project memory, with you at the helm on every transition. This is what
an editor-only tool can't do — it has neither your production nor your
operational memory.

Everything here is opt-in and degrades gracefully: on self-hosted you wire the
URLs yourself; on managed cloud a single fleet resource provisions the stack.

---

## Observability (the Operate tab)

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

1. records an **incident** (visible in the Operate tab),
2. **spawns a diagnosis session** pre-seeded with the metric, the labels, and the
   instruction to search memory, investigate, and propose a fix — **waiting for
   your go-ahead before touching anything**,
3. pings you (see *HITL push*).

Resolve the incident when done; ask the agent to write a short post-mortem note
to memory, and the next occurrence starts smarter.

---

## Secrets (Profile → Secrets)

Operating prod means keys. The vault stores secrets **encrypted at rest**
(per-instance Fernet key) and injects them into every session as environment
variables. Your agent uses `$STRIPE_KEY` to deploy or call an API — but the value
never appears in the UI, the audit log, or the prompt sent to the model
(CI/CD-style). Secrets never leave the instance. Admin-only; names are shown,
values never returned.

---

## HITL push (Profile → Notifications)

You launch nine sessions and step away; one hits a permission gate. Instead of
blocking silently, SOKKAN pings you (Telegram or a generic webhook) after the
request stays pending for ~25s, with a link to come click. Answer in time and no
ping is sent. This is also the channel production alerts fan out to. Configure
and test it in *Profile → Notifications*.

---

## Runbooks

A runbook is a memory note named `runbook-*` — your agents write them as they
operate, same pipeline as the rest of memory. In the Operate tab, **Run** spawns
a session guided by the runbook, with the project memory, executing step by step
and stopping for your approval on anything irreversible. Ops becomes reproducible
and supervised, not tribal knowledge.

---

## Deploy & rollback (managed cloud)

Deploy a Docker image to one of your fleet workers and roll back to the previous
tag in one click — the same versioned, human-gated pattern SOKKAN uses to update
itself, applied to your apps. From the fleet view (admin).
