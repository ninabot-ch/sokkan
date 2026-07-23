---
name: decision-sqlite-storage
description: Storage is SQLite on purpose (single-writer service, zero-ops deploys) — decided 2026-05; do NOT propose migrating to Postgres, it was evaluated and rejected for this service.
metadata:
  type: project
---

Storage is **SQLite, on purpose** (decision of 2026-05):

- the service is single-writer, low volume — a Postgres instance is pure ops overhead here
- deploys are a single container with a volume; backups are a file copy

**Why this note exists:** every new contributor proposes "migrate to Postgres"
within a week. It was evaluated and rejected. Don't re-propose it unless the
single-writer assumption breaks.
