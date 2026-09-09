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
