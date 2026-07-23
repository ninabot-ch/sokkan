---
name: run-and-port
description: Dev server runs on port 8734 (NOT the FastAPI default 8000 — that port collides with the team's other stack). Command uvicorn app.main:app --port 8734 --reload.
metadata:
  type: project
---

Run the dev server with:

```bash
uvicorn app.main:app --port 8734 --reload
```

**Port is 8734, not 8000.** 8000 is taken by the team's other stack on shared
dev machines — anything you start on 8000 will collide. Health check:
`curl -s localhost:8734/healthz` (see [[health-endpoint-convention]]).
