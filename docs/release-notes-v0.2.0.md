Hardening release — security, robustness and open-source hygiene ahead of wider use. 15 atomic commits, full details in #1.

## ⚠️ Upgrading from v0.1.0

The api container now runs as a **non-root user (uid 1000)**. Data volumes created by v0.1.0 are owned by root — fix once before starting:

```bash
docker compose run --rm --user root api chown -R 1000:1000 /data
docker compose up -d --build
```

(A fresh install needs nothing.)

## Security

- **SSRF guard on preview screenshots** — targets are resolved before Chromium runs; private, loopback, link-local and cloud-metadata addresses are refused (`SOKKAN_PREVIEW_ALLOW_PRIVATE=1` to opt out)
- **Feature flags enforced server-side** — preview/tmux endpoints 404 when disabled; `/api/features` is only a UI hint
- **Rate-limited local login** — 5 failures/min per client IP
- **WebSocket Origin verification** + removal of the CORS wildcard
- **Non-root api container** + `no-new-privileges` on both services + healthcheck
- **`SOKKAN_DEFAULT_ROLE`** — set to `none` to reject authenticated-but-unknown emails with 403

## Robustness & performance

- Memory reindexing runs **in-process** (warm embedding model, corpus change detection) — the API serves immediately on boot instead of blocking on the first index
- SDK session history **rehydrates from the persisted transcript** on refresh (no longer capped by the WS ring buffer)
- DB schemas initialize once per process; fire-and-forget asyncio tasks hold strong references
- **Standalone Next.js image** (smaller, runs as `node`)

## Open source

- CI (lint, tests, docker smoke), 19 unit tests, `CONTRIBUTING.md`, issue templates
- **Security model** section in the README — container boundary, roles, SSRF policy, no telemetry
- All user-facing API messages in English

Install / upgrade: `curl -fsSL https://sokkan.ch/install.sh | sh` · Apache-2.0 · self-hosted · BYOK
