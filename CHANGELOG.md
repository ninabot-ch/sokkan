# Changelog

Notable changes, newest first. Versions are release hashes (see
`https://sokkan.ch/dist/VERSION`); dates are release days.

## 2026-07-22
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
