---
name: api-auth-header
description: Mutating endpoints (POST/PATCH/DELETE) require header X-Notes-Token matching the NOTES_TOKEN env var (default dev-token in dev). Reads are open. Use require_token() from app/main.py.
metadata:
  type: project
---

Auth model (deliberately minimal):

- **Reads are open** (`GET /notes`, `GET /notes/{id}`, `/healthz`)
- **Mutations require `X-Notes-Token`** matching the `NOTES_TOKEN` env var
  (defaults to `dev-token` in dev)

Any new mutating endpoint must call `require_token()` (in `app/main.py`) first —
a missing/invalid token is a 401 in the [[error-envelope]] shape.
