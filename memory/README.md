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
| `core/` | **3.0 engine (CortHeXis memory-core)**: profiles `leger/standard/gpu`, llama.cpp servers, model licence and download — see [`core/README.md`](core/README.md) |

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
chunks (HNSW, `ef_search` 200) + the 50 best notes on the lexical score (GIN), each
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
| `HNSW_EF_SEARCH` | 200 | HNSW search breadth |
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

### Load test (03.10.2026, `core/bench_store.py`)

Postgres in a container capped at **1 GB RAM** (`shared_buffers` 256 MB,
`maintenance_work_mem` 512 MB, 2 parallel maintenance workers) on an 8-core host shared
with other workloads; 768-d unit vectors clustered in 2 000 topics, 10 chunks of ~90
words per note; queries = a stored chunk + noise. HNSW `m` 16, `ef_construction` 64,
`ef_search` 200, 100 dense + 50 lexical candidates, no reranker.

| chunks | bulk insert | HNSW build | DB / HNSW size | RAM (container) | search p50 / p95 | top-1 = exact | top-8 overlap with exact | `upsert_note` (10 chunks) p50 |
|---|---|---|---|---|---|---|---|---|
| 25 000 | 1 600 chunks/s | 23 s | 155 / 49 MB | 330 MB | 16-33 / 84-96 ms | 100 % | 0.85 | 80 ms |
| 100 000 | 1 600 chunks/s | 87 s | 557 / 195 MB | 320 MB | 14-23 / 27-49 ms | 100 % | 0.94 | 65 ms |
| 250 000 | 1 470 chunks/s | 224 s | 1 365 / 488 MB | 350-590 MB | 19-69 / 52-115 ms | 100 % | 0.98-0.99 | 96-124 ms |

Ranges = repeated runs (the host load varied). Below 20 000 chunks the search is exact.

HNSW recall@10 against an exact scan, 250 000 chunks (`m` 16, `ef_construction` 64):

| corpus | `ef_search` 40 | 100 | 200 | 400 |
|---|---|---|---|---|
| topics well separated (spread 1.0) | 0.84-0.90 | 0.96-0.98 | **1.00** | 1.00 |
| topics closer (spread 1.5) | 0.81 | 0.93 | **0.99** | 0.99 |
| near-uniform noise (spread 3.0, pathological) | 0.09 | 0.16 | 0.23 | 0.29 |

The last line is the worst case of random vectors, where the 10 "true" neighbours are
barely closer than the rest of the corpus; real embeddings are clustered.

## Running the 3.0 engine in SOKKAN (store wiring, recall at every turn)

**Backend.** With a database configured (`CORTHEXIS_DATABASE_URL`, set by the compose
file) the store is the backend: `memory_search`, `memory_get` (prefixed with the note's
age and the provenance of its date), `memory_links`, the CortHeXis tab and the recall
hooks read Postgres. `CORTHEXIS_MEMORY_BACKEND=sqlite` keeps the 2.x `memory.db`.
Queries are embedded by the embedder that serves the **active generation**
(`core.switch.serving_embedder` when present, i.e. the profile chosen by the operator);
if none matches the active index (a model change in progress), the search runs
lexical-only and says so — never with vectors of another model. Reranker per
`embed.rerank_policy()`: `interactive` (GPU) = top 10 of every `memory_search`;
`async` (standard CPU, ~4.5 s) = only for a deep search (`/api/memory/search?deep=1`,
`sokkan memory search --deep`); `off` (light) = never.

**Indexer.** The backend runs `core.indexer.IndexRunner` in a thread: one pass at start,
then a pass when the notes folder changes (signature polled every `CORTHEXIS_WATCH_S`,
3 s, debounced 1 s: a new note is searchable ~6 s after it is written) and every
`CORTHEXIS_REINDEX_S` (900 s: dates, normalisation grace). Every pass normalises the
corpus (`core.normalize`) and rewrites `MEMORY.md`. It writes with the serving embedder
and only ever activates the **first** generation; a model change goes through the gated
switch (`core.switch`, bench before switch). By hand: `python -m core.indexer [--watch]
[--rebuild] [--no-normalize]` (from `memory/`), or `sokkan memory index|search|get|status`
through the cockpit API.

**Recall at every turn** (`core/recall.py`). SOKKAN installs two hooks in every session it
starts: in-process callbacks for chat sessions (`backend/memrecall.py`, Agent SDK
`hooks=`), command hooks through `claude --settings` for terminal sessions
(`memory/recall_hook.py`, which posts to the warm backend, `POST /api/memory/hook`,
token file `data/claude-hooks/recall-token`, and falls back to an in-process recall).

- `UserPromptSubmit`: top 4 notes above the model's threshold, or whose name is quoted in
  the message; notes already injected in the session (spawn pre-recall included) are not
  injected again; the block is framed as data, not instructions.
- `PreToolUse` on `Task|Agent`: the recall of the sub-agent's task (with an excerpt of each
  note) is appended to its prompt through `updatedInput` — verified end to end on the CLI
  bundled with claude-agent-sdk 0.2.163 (Claude Code 2.1.286) and 2.1.259
  (`tests/test_recall_e2e_cli.py`, against a scripted API: no credit spent). No
  `permissionDecision` is needed, and `can_use_tool` still sees the call.
- Reranker on the top 3 only where it is interactive (GPU); budget ~1.5 s; any failure =
  nothing injected. `CORTHEXIS_RECALL=0` turns it off; `CORTHEXIS_RECALL_TOPK`,
  `_THRESHOLD`, `_BUDGET_S`, `_DEBUG`.
- Every recall is recorded: injected notes in `recall_log` (session, sub-agent, rank,
  score, rerank, version of the note, generation), every attempt in `recall_turns`
  (migration 0010: injected or not, why skipped, latency, profile).
  `GET /api/memory/recall-log?session=&note=` returns both (entries + summary).

**Threshold, measured (03.10.2026).** EmbeddingGemma, real 415-note corpus, 300 memory
questions against 36 generic coding prompts, top 4:

| threshold | expected note injected | generic prompts with a recall |
|---|---|---|
| 0.50 (2.x value) | 22 % | 0 % |
| 0.40 | 63 % | 6 % |
| **0.35 (default)** | **77 %** | 19 % |
| 0.30 | 87 % | 39 % |
| GPU: 0.40, or ≥ 0.25 with rerank ≥ 0.5 (default) | **87 %** | 17 % |

Several "generic" prompts that get a recall are in fact related to the corpus (a release
note prompt recalls the release procedure). e5 (fallback model): 0.80, estimated, not
measured — run `core.bench_recall` before relying on it.

**"Ignored facts" bench** (`python -m core.bench_recall --dsn … --load`): 12 fictional
scenarios whose answer is only in a note, among 16 look-alike notes, posed at turn 1, at
turn 5 (after 4 coding messages) and as a sub-agent prompt; the bench reads what is
injected, no model answers.

| profile | turn 1 / turn 5 / sub-agent | coding messages with a recall | latency p50 / p95 (in process) |
|---|---|---|---|
| light (CPU embedding, no reranker) | 9/12 each | 0/48 | 48 / 63 ms |
| GPU (embedding + reranker top 3) | 10/12 each | 0/48 | 158 / 178 ms |

On the 415-note corpus the GPU recall takes 650 ms p50 / 0.9-1 s p95 (reranking longer
notes). Terminal-session hook, end to end (process start included): 190-310 ms through
the backend, 1.0-1.4 s when it has to open its own store connection (psycopg + numpy
imports ~0.75 s). Measured from a host to model servers on another machine of the LAN.
