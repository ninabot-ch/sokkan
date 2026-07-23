# Example workspace — fastapi-notes

A tiny FastAPI service **plus the project memory that makes SOKKAN click**: eight
notes capturing facts that exist nowhere in the code comments — the non-obvious
port, the health-endpoint convention, the error envelope, past decisions, and a
replayable runbook.

Spawn a card against this workspace and watch the session recall those facts
*before* reading the code. That's the whole point.

## Try it (2 minutes on a running SOKKAN)

From your SOKKAN checkout:

```bash
# 1. point your instance at this example
sed -i 's|^SOKKAN_WORKSPACE=.*|SOKKAN_WORKSPACE=./examples/fastapi-notes|' .env
docker compose up -d

# 2. seed the memory notes + three board cards
./examples/fastapi-notes/seed.sh
```

Open the cockpit → **Board** → pick « Add DELETE /notes/{id} » → **▶ spawn**.
The session will search the memory, recall that the service runs on port **8734**
(not 8000), that errors must use the envelope, that tests are integration-style —
then propose a plan and wait for your go.

Check **Memory/KB** to browse the notes and play with the semantic search — the
playground shows exactly what a session would recall for any query.

## What's in here

```
app/            the FastAPI service (deliberately small)
tests/          integration tests (the convention one note documents)
memory/         the project memory notes seed.sh installs
seed.sh         copies the notes into the instance + creates 3 board cards
```

## The facts hidden in memory (not in code comments)

| Note | The fact a fresh session couldn't guess |
|---|---|
| `run-and-port` | dev server runs on **8734** — 8000 collides with the team's other stack |
| `health-endpoint-convention` | `/healthz` (not `/health`), must return `{"ok": true, "rev": …}` |
| `error-envelope` | every 4xx/5xx body is `{"error": {"code", "message"}}` — clients depend on it |
| `decision-sqlite-storage` | SQLite on purpose; do **not** propose Postgres |
| `testing-conventions` | integration tests against a temp DB, no mocking of storage |
| `api-auth-header` | mutating endpoints require `X-Notes-Token` |
| `runbook-release` | the release procedure, replayable from the Operate tab |

One note (`error-envelope`) is marked `priority: high` — priority facts get a
ranking boost so they surface even for loosely related tasks.
