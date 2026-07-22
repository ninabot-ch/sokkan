# Changelog

Notable changes, newest first. Versions: semver + release hash (see
`https://sokkan.ch/dist/VERSION`); dates are release days.

## 1.1.0 — 2026-07-22 — "Operate"
The loop doesn't stop at deploy. New **Operate** capabilities — run your
production from the cockpit, with agents that share the project memory:
- **Observability**: a Prometheus + Grafana + Loki stack your sessions read and
  write via the `sokkan-observability` MCP (« build a dashboard for my p95 »).
  Managed cloud provisions it as a fleet resource; self-hosted wires its own.
- **Alerts → supervised incidents**: a production alert becomes an incident *and*
  spawns a pre-seeded diagnosis session (metric + context + memory) that waits
  for your go-ahead. Post-mortem goes back to memory.
- **Secrets vault**: encrypted at rest, injected into sessions as env vars, never
  shown to the UI or the model.
- **HITL push**: get pinged (Telegram/webhook) when a session waits on your
  approval and you've stepped away.
- **Runbooks**: replay a `runbook-*` memory note as a guided, supervised session.
- **Deploy & rollback** a Docker image to a fleet worker (managed cloud).

See [`docs/OPERATE.md`](docs/OPERATE.md).

## 1.0.0 — 2026-07-22
First stable release.
- **English UI** throughout (the cockpit was previously part French).
- **Security hardening** (pre-1.0 review): cf-access mode no longer falls back
  to owner off the loopback path; the ttyd terminal requires admin + the tmux
  feature flag; route hostnames/ports are validated before the edge Caddyfile;
  local-login rate-limiting keys on the real client IP. Control plane: closed a
  cross-tenant `*.sokkan.ch` namespace collision, restricted the fleet SSH-key
  comment, and made the self-service plan change CSRF-proof (session cookie +
  token).
- **Upgrade & rollback** documented end to end (`docs/UPGRADE.md`), self-hosted
  and managed; `SECURITY.md` added.
- Everything in 0.9.0 below (fleet web exposure, one-click/managed upgrades).

## 0.9.0 — 2026-07-22
- **Self-hosted upgrade path**: re-running the installer
  (`curl -fsSL https://sokkan.ch/install.sh | sh`) now upgrades an existing
  install in place — `.env` and data volumes preserved, short rebuild. The
  daily update check is now surfaced in **Profile** with the exact command.
- **Managed**: one-click cockpit update from the fleet tab; new releases roll
  out to the managed fleet automatically.

## 2026-07-17
- **Fleet web exposure** (managed): publish fleet services on
  `<name>-<tenant>.sokkan.ch` subdomains (via the tenant tunnel) or on your
  own domain (one CNAME, automatic Let's Encrypt TLS), managed from the
  fleet tab. Free, admin-gated, audited.

Older changes: see the git history.
