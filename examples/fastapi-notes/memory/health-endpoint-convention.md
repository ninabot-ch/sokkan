---
name: health-endpoint-convention
description: Health endpoint is /healthz (not /health), must return {"ok": true, "rev": <git rev>} — the deploy tooling greps for "ok" and "rev". Any new service in this team follows the same convention.
metadata:
  type: project
---

The health endpoint is **`/healthz`** — the team standard, matching the other
internal services. It must return:

```json
{"ok": true, "rev": "<git short rev>"}
```

`rev` comes from the `GIT_REV` env var (set at deploy time). The deploy tooling
polls `/healthz` and greps for both keys — renaming either breaks the rollout
gate. Related: [[runbook-release]].
