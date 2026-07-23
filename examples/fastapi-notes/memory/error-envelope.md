---
name: error-envelope
description: Every 4xx/5xx response body MUST be {"error": {"code": <status>, "message": <detail>}} — mobile clients hard-depend on this envelope. Implemented as a FastAPI exception handler in app/main.py.
priority: high
metadata:
  type: project
---

**Every error response** (4xx/5xx) uses the envelope:

```json
{"error": {"code": 404, "message": "note 7 not found"}}
```

Mobile clients parse `error.code`/`error.message` and crash on FastAPI's default
`{"detail": ...}` shape. The envelope is enforced by the `HTTPException` handler
in `app/main.py` — new endpoints just `raise HTTPException(status, detail)` and
inherit it. Never return a bare error dict from a route.
