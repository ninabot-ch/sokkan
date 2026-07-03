<p align="center">
  <img src="frontend/public/sokkan-icon.svg" width="96" alt="SOKKAN" />
</p>

<h1 align="center">SOKKAN</h1>
<p align="center><em>The helm, not the autopilot.</em></p>

SOKKAN is a self-hosted web cockpit for running **multiple Claude Code sessions in parallel** — with the one thing no orchestrator gives you: **your project memory, automatically injected into every session at spawn**.

Spawning a session *is* the "check your memory" ritual: the task description seeds a semantic search over your accumulated project notes (RAG over the memory files Claude Code already writes), so every session starts already knowing what previous sessions learned. Nothing goes to Done without a human at the helm.

<p align="center">
  <img src="docs/shots/session.png" width="820" alt="A session spawned with project memory auto-injected: the agent recalls the deployment context and proposes a plan, waiting for a human go" />
</p>
<p align="center">
  <img src="docs/shots/board.png" width="820" alt="The board: kanban cards with priorities, due dates and checklists — ▶ spawn turns a card into a pre-seeded session" />
</p>

## Features

- **Sessions** — a rail of live sessions and a multi-pane chat grid (built on the official Claude Agent SDK: tool calls, permission prompts and multiple-choice questions render as native web widgets, not scraped terminal output)
- **Board** — a kanban where cards spawn pre-seeded sessions (`▶ spawn` → the card's description becomes the task, memory context loads first, the agent proposes a plan and waits for your go)
- **Memory/KB** — inspect the RAG store: notes, links, backlinks, and a search playground showing exactly what a session would recall
- **Costs** — per-day / per-session token usage and estimated API cost, aggregated from the transcripts
- **Journal** — an audit trail of every action (who spawned, moved, deleted what — the basis for reverting)
- Sessions can talk back: bundled MCP servers let any session **search the memory**, **create/move board cards**, and **push a preview** of what it changed

## Requirements

- **Linux** x86_64/arm64 — or macOS with [Docker Desktop](https://docs.docker.com/desktop/)/OrbStack
- **Docker Engine 24+ with Compose v2** — no Docker? **the installer offers to
  install it for you** (official `get.docker.com` script). On Ubuntu, note the
  apt package is `docker.io`, not `docker` — or just let the installer handle it.
- ~4 GB free RAM (local embedding model + first build), ~3 GB disk
- An Anthropic API key, or a Claude Pro/Max subscription (`claude setup-token`)

## Quickstart

```bash
curl -fsSL https://sokkan.ch/install.sh | sh
```

— downloads the latest release from sokkan.ch (no GitHub dependency), generates an access token, and tells you what to fill in. Or the manual way:

```bash
git clone https://github.com/ninabot-ch/sokkan && cd sokkan
cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY (or CLAUDE_CODE_OAUTH_TOKEN),
# SOKKAN_WORKSPACE (your project path), SOKKAN_LOCAL_TOKEN (openssl rand -hex 24)
docker compose up -d --build
```

Open `http://localhost:3009`, enter your token, hit **+ session** — the first run downloads the local embedding model (~120 MB, cached in the data volume).

Write memory notes as markdown files (one fact per file, with a `description:` frontmatter) — Claude Code sessions write them natively under the workspace's memory directory, and SOKKAN indexes them within ~2 minutes. From then on, every new session starts with that context.

### Using a Claude subscription instead of an API key

If you use Claude Code with a Pro/Max subscription rather than an API key, generate a long-lived token once on your desktop and put it in `.env`:

```bash
claude setup-token          # one-time browser login
# → paste the token into .env as CLAUDE_CODE_OAUTH_TOKEN
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | BYOK key the `claude` CLI uses |
| `SOKKAN_WORKSPACE` | `./workspace` | Host path mounted at `/workspace` (the project sessions work on) |
| `SOKKAN_LOCAL_TOKEN` | *(empty)* | Login token; empty = open access (trusted networks only) |
| `SOKKAN_OWNER_EMAIL` / `_NAME` | `owner@localhost` | Identity in UI + audit journal |
| `SOKKAN_PORT` | `3009` | Web UI port |
| `ML_SERVICE_URL` | *(empty)* | Optional remote embedding endpoint; empty = local ONNX (multilingual MiniLM) |
| `SOKKAN_AUTH_MODE` | `local` | `local` · `oidc` (Authentik/Keycloak/…) · `cf-access` |
| `SOKKAN_FEATURE_PREVIEW` / `_TMUX` | `0` in container | Extra tabs for bare-metal installs (dev-server previews, tmux terminal mode) |

OIDC single sign-on (`SOKKAN_AUTH_MODE=oidc` + `SOKKAN_OIDC_*`) and multi-user roles (viewer/dev/admin/owner) are built in — see `backend/auth.py`.

## Architecture

```
browser ── Next.js (web) ──/api──► FastAPI (api) ──► claude CLI (Agent SDK, stream-json)
                                      │                   │
                                      │                   └─ MCP: sokkan-memory · sokkan-board
                                      ├─ SQLite: board · audit · usage · iam
                                      └─ memory indexer (fastembed ONNX ⟷ optional remote)
```

Everything stays on your machine: SQLite state in a Docker volume, transcripts written by the `claude` CLI itself, LLM calls straight from your container to Anthropic with your key.

## Security notes

- Set `SOKKAN_LOCAL_TOKEN` unless the instance is unreachable from anything you don't trust.
- Sessions execute tools **inside the api container** against `/workspace` — mount only what they should touch. Mutating tools require your click-through approval in the chat pane (reads are auto-allowed).
- The audit journal records actions, not conversation content.

## Status

Early. Born as the internal cockpit running [ninjob.ch](https://ninjob.ch) and its sibling products (≈30 commits/week across 9 parallel sessions); extracted and open-sourced because thin wrappers die and memory is the part that compounds. Roadmap: multi-provider sessions (Codex, …), project scoping, one-command cloud deploy.

## License

[Apache-2.0](LICENSE) — the code is free, self-hosted, BYOK. A managed Swiss-hosted cloud is planned; that operation is the business, not withheld features.
