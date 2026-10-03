# SOKKAN memory — local RAG over markdown notes

The moat: every session starts with the project's memory. Notes are plain
markdown files with YAML frontmatter; an incremental indexer embeds them
locally; an MCP server exposes semantic search to every session.

## Note format

```markdown
---
name: short-kebab-slug
description: one-line summary (also prepended to embeddings — write it well)
priority: high            # optional — boosted at recall, ★ in MEMORY.md
metadata:
  type: project           # project | feedback | reference | user
---

The fact itself. Link related notes with [[other-note-name]].
```

One fact per file. `notes.name` must be unique — two files with the same
`name:` overwrite each other in the index.

## Components

| File | Role |
|---|---|
| `index_memory.py` | incremental indexer + `MEMORY.md` generation (24 KB budget, priority-first) |
| `memory_search_server.py` | MCP stdio server `sokkan-memory` — tools `memory_search`, `memory_get` |
| `embeddings.py` | local fastembed/ONNX by default (multilingual MiniLM, ~120 MB cached); set `ML_SERVICE_URL` for an explicit remote embedding service |

The backend runs the indexer **in-process**: a daemon thread re-checks the
corpus signature every `SOKKAN_REINDEX_S` seconds (default 120) and reindexes
only changed notes. No cron, no systemd unit — write a note, it is searchable
within ~2 minutes.

Search ranking = 0.75 dense cosine + 0.25 lexical overlap, `priority` notes
boosted. If the embedding backend is down, search degrades to lexical-only
(results carry `degraded: true`) instead of failing.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `SOKKAN_MEMORY_DIR` | set by the container | where the `.md` notes live |
| `SOKKAN_MEMORY_DB` | `$SOKKAN_DATA_DIR/memory.db` | sqlite index (vectors included) |
| `SOKKAN_EMBED_MODEL` | multilingual MiniLM | fastembed model id |
| `ML_SERVICE_URL` | unset (local) | explicit remote embedding endpoint |
| `SOKKAN_REINDEX_S` | 120 | reindex tick, seconds |

Manual reindex: `python memory/index_memory.py` (`--rebuild` to start over).

## 3.0 memory store (`memory/core/`, CortHeXis engine)

Self-contained package (no SOKKAN import; it moves to the `corthexis` package): the
indexer writes the notes into **Postgres + pgvector** (`db` service), the search runs
there. Off by default in this branch: `CORTHEXIS_MEMORY_BACKEND=postgres` makes
`memory_search` / `memory_get` read the store (`memory/store_backend.py`).

| File | Role |
|---|---|
| `core/schema.sql` | schema v1; later changes in `core/migrations/NNNN_name.sql`, applied by `Store.migrate()` |
| `core/store.py` | `Store`: psycopg 3 pool, generations, notes/chunks/links, date history, search |
| `core/search.py` | the ranking (pure Python): IDF head/body lexical, blend or RRF, reranker |
| `core/bench_store.py` | load test (25k / 100k / 250k synthetic chunks) |

**Index generations.** One generation = one embedding model (identity + dimension).
`chunks` is partitioned by generation, each partition with its own dimension (CHECK) and
its own HNSW index on `(embedding::halfvec(dim))`: a new model is indexed in the
background while the active generation keeps serving, the switch is atomic
(`activate_generation`), the old one stays searchable 7 days (`purge_retired`), and
dropping it is a DETACH + DROP of its partition.

**Search, in two stages.** (1) one SQL statement: candidates = notes of the 100 nearest
chunks (HNSW, `ef_search` 100) + the 50 best notes on the lexical score (GIN), each
re-scored exactly (best chunk cosine; IDF overlap with the head = name + description and
with the body); below 20 000 chunks every note is scored exactly. (2) blend
`(1 - w) · cosine + w · lexical` (`w` = 0.30 for EmbeddingGemma) or RRF, priority boost,
then the reranker (if given) reorders the top 10. Embedding down: lexical-only, flagged
`degraded`. Every hit carries `age_days`, `date_source` and `generation`.

| Env (`CORTHEXIS_*`, `SOKKAN_*` also read) | Default | |
|---|---|---|
| `DATABASE_URL` | — | `postgresql://user:pass@db:5432/sokkan` |
| `MEMORY_BACKEND` | `sqlite` | `postgres` = search through the store |
| `LEXICAL_WEIGHT` / `HEAD_SHARE` | 0.30 / 0.5 | lexical share of the score / head share of the lexical |
| `SEARCH_FUSION` / `RRF_K` | `linear` / 60 | `rrf` = reciprocal rank fusion |
| `PRIORITY_BOOST` | 0 (0.08 through `store_backend`) | multiplicative boost of `priority: high` |
| `RERANK_TOP` | 10 | notes the reranker reorders |
| `HNSW_EF_SEARCH` | 100 | HNSW search breadth |
| `SEARCH_DENSE_CANDIDATES` / `SEARCH_LEXICAL_CANDIDATES` | 100 / 50 | candidate pool |
| `SEARCH_EXACT_MAX_CHUNKS` | 20000 | below: exact scan, no HNSW |

Tests against a throw-away Postgres:

```bash
docker run -d --name sokkan-pg-test --memory 1g --shm-size 512m -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=sokkan -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
SOKKAN_TEST_PG_DSN=postgresql://sokkan:test@127.0.0.1:55432/postgres pytest
cd memory && python -m core.bench_store --dsn postgresql://sokkan:test@127.0.0.1:55432/postgres \
  --container sokkan-pg-test
```
