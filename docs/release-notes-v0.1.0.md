First tagged release of the public SOKKAN container (v0).

**SOKKAN** is a self-hosted web cockpit for running multiple Claude Code sessions in parallel, with your project memory automatically injected into every session at spawn. *The helm, not the autopilot.*

## Highlights

- **Memory auto-injection at spawn** — the task description seeds a semantic search (local ONNX embeddings, SQLite — no external service, no telemetry) over the memory notes your sessions write
- **Sessions** — multi-pane chat grid on the official Claude Agent SDK; permission prompts and questions render as buttons — nothing irreversible without a click
- **Board** — kanban cards spawn pre-seeded sessions (priorities, due dates, checklists)
- **Costs** — per-day / per-session token usage and estimated API cost from the transcripts
- **Journal** — audit trail of every action
- **Bundled MCP servers** — sessions can search the memory and create/move board cards themselves
- Works with an Anthropic API key **or a Claude Pro/Max subscription** (`claude setup-token`)

## Install

```bash
curl -fsSL https://sokkan.ch/install.sh | sh
```

(sovereign distribution from sokkan.ch — no GitHub dependency; or clone this repo and `docker compose up -d --build`, see the README)

## Requirements

Linux or macOS (Docker Desktop/OrbStack), Docker Engine 24+ with Compose v2 (the installer can install it), ~4 GB RAM, ~3 GB disk.

Apache-2.0 · self-hosted · BYOK — free without limits.
A managed Swiss sovereign cloud offering is in preparation.
