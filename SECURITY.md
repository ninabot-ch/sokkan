# Security policy

## Reporting a vulnerability

Email **security@sokkan.ch** (or hello@sokkan.ch) with details and, ideally, a
proof of concept. We aim to acknowledge within 3 business days. Please give us a
reasonable window to ship a fix before public disclosure. We're a small team —
good-faith reports are genuinely appreciated.

## Scope

- **This repo** (`ninabot-ch/sokkan`) — the self-hosted cockpit: backend, frontend,
  the bundled MCP servers, the installer.
- The managed cloud control plane (provisioning, billing) is a separate,
  closed component; report issues there to the same address.

## Security model (summary)

The full model is in the [README](README.md#security-model). Key points:

- **The boundary is the container.** Agent sessions execute tools inside the
  `api` container as an unprivileged user against `/workspace`; mutating tools
  require click-through approval. The container never gets the Docker socket.
- **Auth**: `local` (single-user token, rate-limited per real client IP),
  `oidc`, or `cf-access`. In `cf-access`, a request without a valid Access JWT
  is only accepted from a genuine loopback source — never from a remote peer.
- **Roles** `viewer < dev < admin < owner`. Session spawn/mutation needs `dev`;
  member management, the fleet actions and the maintenance terminal need
  `admin`. Feature flags are enforced server-side (a disabled feature `404`s),
  not just hidden in the UI.
- **Data locality**: SQLite state, the memory index and transcripts stay in your
  volumes. The only outbound traffic is your prompts to Anthropic, with your key.

## Supported versions

We ship fixes against the latest release. Upgrade with the installer (see
[`docs/UPGRADE.md`](docs/UPGRADE.md)); the cockpit tells you in **Profile** when
a new release is out.
