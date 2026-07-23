---
name: project-overview
description: fastapi-notes — tiny team-notes API (FastAPI + SQLite), example workspace for SOKKAN. Entry point app/main.py, storage app/storage.py, integration tests in tests/.
metadata:
  type: project
---

fastapi-notes is a small team-notes REST API used to demo SOKKAN's memory recall.

- `app/main.py` — FastAPI app: `/healthz`, `GET/POST /notes`, `GET /notes/{id}`
- `app/storage.py` — SQLite storage (see [[decision-sqlite-storage]])
- `tests/test_api.py` — integration tests (see [[testing-conventions]])

Key conventions live in dedicated notes: [[run-and-port]], [[health-endpoint-convention]],
[[error-envelope]], [[api-auth-header]].
