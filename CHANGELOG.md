# Changelog

Notable changes, newest first. Versions: semver + release hash (see
`https://sokkan.ch/dist/VERSION`); dates are release days.

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
