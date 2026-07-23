---
name: testing-conventions
description: Tests are integration-style — TestClient against real storage on a temp SQLite DB (NOTES_DB env), no mocking of the storage layer. Run with pytest -q. Every new endpoint ships with tests for the happy path AND the error envelope.
metadata:
  type: project
---

Testing rules for this repo:

- **Integration over mocks**: tests hit the app through `TestClient` with real
  storage on a temp DB (`NOTES_DB` env var, see the `client` fixture in
  `tests/test_api.py`). Never mock `app/storage.py`.
- Run: `pytest -q`
- Every new endpoint ships with at least: one happy-path test, one test that the
  error case uses the [[error-envelope]], and (if mutating) one 401 test for
  [[api-auth-header]].
