---
name: runbook-release
description: Release runbook for fastapi-notes — run tests, bump rev, build image, deploy, verify /healthz rev matches. Replayable from the SOKKAN Operate tab (runbook- prefix).
metadata:
  type: project
---

Release procedure — execute in order, stop for approval before step 4.

1. **Tests green**: `pytest -q` — abort on any failure.
2. **Rev**: `GIT_REV=$(git rev-parse --short HEAD)` — this is what `/healthz`
   must report after deploy ([[health-endpoint-convention]]).
3. **Build**: `docker build -t fastapi-notes:$GIT_REV .`
4. **Deploy** (irreversible — ask for the go): restart the container with the
   new tag and `GIT_REV` in the environment.
5. **Verify**: `curl -s localhost:8734/healthz` → `rev` must equal `$GIT_REV`
   and `ok` must be `true`. If not, roll back to the previous tag.
