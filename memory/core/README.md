# memory-core — the CortHeXis memory engine (SOKKAN 3.0)

Self-contained package (no import from the SOKKAN backend or Magnitude),
configured with `CORTHEXIS_*` variables — the `SOKKAN_*` names and
`ML_SERVICE_URL` of SOKKAN are read as a fallback. It will move to the public
`ninabot-ch/corthexis` repository; SOKKAN depends on it.

| File | Role | Dependencies |
|---|---|---|
| `embed.py` | embedding client: `identity()`, `embed_docs()`, `embed_query()`, `rerank()` | httpx (fastembed for the legacy profile) |
| `models.py` | model registry, Gemma licence gate, first-run download + SHA-256 | stdlib |
| `recall.py` | recall at every message (`UserPromptSubmit`) and for sub-agents (`PreToolUse` Task/Agent): selection, dedup, logging, command-hook entry `python -m core.recall` | store, embed |
| `bench_recall.py` | "ignored facts" bench of the recall (fictional corpus, no model call) | store, embed, indexer |
| `profiles.py` | profiles `leger / standard / gpu`, costs, hardware detection and recommendation | stdlib |
| `eval.py` | recall bench on the client's own notes: questions (transcripts, written, generated), runs, comparison, regression findings, nightly | psycopg (store) |
| `switch.py` | profile / model change gated by the bench: background build, atomic switch, approve / cancel, 7-day rollback | store, embed, indexer |
| `review.py` | the memory reads itself back: health score and history, findings with their remedy, chain checks, alert policy (`python -m core.review`) | store (optional), numpy |
| `repair.py` | one-click repairs as proposals with a diff (relink, merge, rename, close), applied all-or-nothing | stdlib |

## Profiles

`python3 memory/core/profiles.py` (or, in SOKKAN, `python3 -m magnitude --memory-profile`)
recommends a profile from cores, RAM and GPU. Figures: memory bench of 03.10.2026
(300 questions fr/en/de, hybrid search; GPU pass on an Intel Arc Pro B60).

| Profile | Embedding | Reranker | Recommended when | Model servers | Query p50 | 250 k chunks import | MRR |
|---|---|---|---|---|---|---|---|
| `leger` | EmbeddingGemma-300m Q8, CPU | — | default (4 cores, 4 GB minimum) | ~1.2 GB RAM | 53 ms | ~39 h | 0.82 |
| `standard` | EmbeddingGemma-300m Q8, CPU | bge-reranker-v2-m3, background only (~4.5 s top 10) | ≥ 8 cores and ≥ 16 GB | ~3.5 GB RAM | 35 ms | ~23 h | 0.82 (0.84 reranked) |
| `gpu` | EmbeddingGemma-300m, GPU | Qwen3-Reranker-0.6B, top 10 in every search | GPU with ≥ 4 GB (NVIDIA, Intel Arc, Apple) | ~3.5 GB VRAM | 13 ms | ~1.4 h | 0.88 |

`legacy` / `remote` = the SOKKAN 2.x embeddings (fastembed MiniLM, or the API of
`ML_SERVICE_URL`), kept so a 2.x index keeps serving while the corpus is
re-encoded. `embed.rerank_policy()` tells a caller whether it may rerank inline
(`interactive`), only in background work (`async`) or not at all (`off`).

## Recall bench and profile changes

`eval.py` answers "does the memory find the right note, here?" with the questions of the
instance itself (tables of `migrations/0006_eval.sql`):

- **harvested** from the session transcripts — a person's prompt followed, in the same
  session, by `memory_get(note)` is a question and its expected note (weight 1);
- **written** by a person with the expected note (weight 1);
- **generated** from a note's description by the instance's LLM (weight 0.4,
  `CORTHEXIS_EVAL_GENERATED_WEIGHT`: the LLM just read the note, it flatters the engine).

Metrics as in the reference bench: hit@1, hit@5, MRR, nDCG@10, weighted, per source and per
index generation; every run and per-question rank is stored. `check_regression` compares a
run with the previous one of the same generation and profile on their shared questions (MRR
drop > `CORTHEXIS_EVAL_MAX_DROP`, default 0.02, on ≥ 5 questions) and `findings()` turns it
into a review finding. `python -m core.eval run | harvest | add | list | report | compare`.

`switch.py` changes profile, model or servers as a job: a target that embeds with another
model builds a new generation in the background (the indexer, `auto_activate=False`) while
the current one serves; the same model elsewhere (CPU → GPU, reranker added) reuses the
active generation. The bench then runs the same questions on both setups; the switch is
atomic and only happens when recall does not drop, otherwise it waits for a person
(`approve` / `cancel`). A preflight refuses servers that serve another model file than the
one claimed (two 768-d models would otherwise mix silently). The previous setup can be
restored for 7 days (`rollback`); `Store.purge_retired` drops it afterwards. Query side:
`switch.serving_embedder(store)` gives the embedder matching the active generation.


`docker/embed/compose.yml` runs llama.cpp (`ghcr.io/ggml-org/llama.cpp`, pinned
build): `corthexis-embed` (`--embedding`), `corthexis-rerank` (`--reranking`,
compose profile `rerank`) and the one-shot `corthexis-embed-fetch`. SOKKAN carries the
same services in its own `docker-compose.yml` (no `include:`: any Compose v2 from 2.12
runs it). GPU: `compose.sycl.yml` (Intel, SYCL) or `compose.cuda.yml`, overrides of the
SOKKAN compose (`-f`, or `COMPOSE_FILE` in `.env` as memory-setup.sh writes it), add
`corthexis-embed-gpu` in front of the CPU server. Apple GPU: Docker has no Metal — run
`docker/embed/run.sh` natively (`LLAMA_SERVER=llama-server`).

`CORTHEXIS_EMBED_URLS` lists servers of the **same model**, tried in order (GPU
then CPU, a Magnitude node then the local container): the first ones get a 2 s
timeout, a failed server is skipped for 30 s. A server answering another
dimension is refused (`DimensionMismatch`) instead of polluting an index.
`identity()` (`llamacpp:embeddinggemma-300m-q8@768`) is stored with each index
generation; a different identity forces a re-index.

Mandatory server settings: `--cache-ram 0` (the prompt cache defaults to 8 GiB
and grows until the OOM killer), `-ub` ≤ 2048, documents truncated to 500 tokens
by the client (`/tokenize`), task prefixes from the registry (EmbeddingGemma:
query `task: search result | query: `, document `title: none | text: `).

## Review and repairs

`review.run_review(config, source, chain=chain_checks(...))` reads the notes (the files
are the truth) and an index (`PgSource` for the 3.0 store, `SqliteSource` for a 2.x
`memory.db`, `MemorySource` for the reference store) and returns a report: score 0-100
(100 − Σ weight(severity) × (1 + ln(1 + count) / 3), weights 18 / 5 / 1), findings
grouped by theme, each with the notes concerned, one row per problem and the remedy.

| Theme | Checks |
|---|---|
| chain | memory server declared, answers a real MCP handshake (started as a session starts it), recall hook installed, embedding / reranking servers up, index readable, in sync, not lagging |
| structure | header unreadable, no description, no type, name / file outside the convention, very long notes |
| graph | `[[links]]` to missing notes (code ignored; a replacement is suggested from renames and close names), isolated notes |
| dates | notes without a date, reconstructed dates, whole corpus rewritten at once |
| drift | dormant projects still open, description corrected but not the body (`drift.py`), cited repository files gone (only with `DEAD_PATH_ROOTS`), near duplicates |
| security | key-shaped strings (the value is never echoed — kind and line only), text that gives orders to the agent |
| recall | findings of the bench (`eval.findings`), through `external_findings()` |

Near duplicates: below 3 000 notes the exact all-pairs product of the notes' mean
vectors; above, the k nearest chunks of each centroid through the generation's HNSW index
then the exact cosine of those pairs only (10 000 notes / 40 000 chunks: 21 s, constant
memory, same pairs as the exact product — which needs 400 MB there and n² beyond).

History (`PgHistory`, migration `0007_review`; `SqliteHistory` otherwise): every run,
every (check, note) problem with its first and last sighting and when it went away —
hence "open for more than 7 days" and the mean time to fix. `alert_decision()`: one
digest a day when the findings changed, at once on a new critical (cooldown 6 h).

`repair.py` computes the complete change (before → after, unified diff) of a relink, a
merge, a rename or the closing of a dormant project; `apply()` writes it only if no file
changed in between, keeps a copy of what it overwrites, writes before it deletes.

False positives, measured: fictional corpus of `tests/fixtures/review_corpus` (15
labelled problems among traps: links in code, link variants, closed projects, placeholder
keys, quoted attacks, globs, reworded descriptions): 0 false positive, 0 miss. Real corpus
of 415 notes: 0 false positive on secrets, injection, dormant projects, duplicates and
orphans after the fixes of 03.10.2026 (5 of 16 before); cited files gone stays noisy
(~ 3 in 4 are paths of repositories that are not checked) — it only runs when the roots
are configured, at the "to watch" level.

| Variable (`CORTHEXIS_…`) | Default | Purpose |
|---|---|---|
| `REVIEW_EVERY_S` | 3600 | period of the review (SOKKAN backend), 0 = off |
| `REVIEW_DIGEST_AT` / `REVIEW_TZ` | 08:20 / `TZ` | daily digest time |
| `REVIEW_CRIT_COOLDOWN_H` | 6 | minimum gap between two critical alerts |
| `REVIEW_STALE_DAYS` / `_GIANT_WORDS` / `_DUP_COSINE` / `_UNDATED_TOLERATED` | 60 / 3500 / 0.86 / 7 | thresholds |
| `DEAD_PATH_ROOTS` / `DEAD_PATH_PREFIXES` | — | folders (`:`) and prefixes (`,`) of the cited-files check |
| `REVIEW_EXPECT_HOOK` / `REVIEW_HOOK_PATTERN` | off / — | check the recall hook in the sessions' settings |
| `REVIEW_MCP_CONFIGS` | — | session configuration files that must declare the server |

## Licences

The code is Apache-2.0 and ships **no weights**. On first run
`models.py setup` shows the summary of the Gemma Terms of Use (English and
French), records the decision in `licence.json` (decision, version of the terms,
date, who, how — with history) and downloads the GGUF pinned to a repository
commit, checked against its SHA-256. Unattended installs answer with
`CORTHEXIS_ACCEPT_GEMMA_TERMS=1|0`; without an answer nothing is recorded on the
operator's behalf and the fallback model runs until someone accepts. A GGUF
copied by hand into `gguf/` (offline machine) is used if its hash matches.

| Model | Licence | Role |
|---|---|---|
| EmbeddingGemma-300m | Gemma Terms of Use (not open source; use policy, notice on redistribution) | default |
| multilingual-e5-base | MIT (XLM-RoBERTa lineage) | **fallback** when the terms are declined, undecided or Gemma cannot be fetched |
| harrier-oss-v1-270m | **subject to the Gemma Terms** (see below) | registry only, never a fallback; selectable explicitly with `CORTHEXIS_EMBED_MODEL_ID` |
| Qwen3-Reranker-0.6B, bge-reranker-v2-m3 | Apache-2.0 | rerankers |

The fallback, multilingual-e5-base (`dinab/multilingual-e5-base-Q8_0-GGUF`,
pinned commit + SHA-256), runs in llama.cpp with mean pooling, prefixes
`query: ` / `passage: `, 768 dimensions, trained context 512 tokens (documents
are cut at 500). Bench: MRR 0.74 with a lexical weight of 0.1 (0.62 with the
0.3 that suits Gemma — `embed.lexical_weight()` gives the right one), against
0.82 for EmbeddingGemma and 0.55 for the 2.x MiniLM.

**Why not harrier-oss-v1-270m.** Its model card says MIT, but its configuration
is the one of Gemma 3 270m (Gemma3TextModel, 18 layers, hidden size 640,
262 144-token vocabulary): it is very likely fine-tuned from Gemma 3 weights,
which the Gemma Terms define as a *Model Derivative* bound by the same use
restrictions. Offering it to someone who declined those terms would defeat the
point of the choice; `fallback_key()` refuses any model labelled `gemma`.

## Configuration

| Variable (`CORTHEXIS_…`, else `SOKKAN_…`) | Default | Purpose |
|---|---|---|
| `MEMORY_PROFILE` | `remote` if `ML_SERVICE_URL`, else `leger` | profile |
| `EMBED_URLS` | per profile (`http://corthexis-embed:8080`…) | embedding servers, in order |
| `RERANK_URL` | per profile; `off` disables | reranker |
| `RERANK_POLICY` | per profile | `off` / `async` / `interactive` |
| `MODELS_DIR` | `$DATA_DIR/memory-models`, else `~/.local/share/corthexis/models` | licence + models state |
| `EMBED_MODEL_ID` | from `active.json` | force a registry model |
| `EMBED_MAX_TOKENS` | 500 | document truncation |
| `ACCEPT_GEMMA_TERMS` | — | unattended licence answer |
| `EMBED_FALLBACK` | `multilingual-e5-base-q8` | non-Gemma fallback |
| `MODEL_BASE_URL` | `https://huggingface.co` | mirror for the downloads |

## Same engine as CortHeXis

This package is the engine of [CortHeXis](https://github.com/ninabot-ch/corthexis) (package
`corthexis`), embedded here under the module name `core`. `CORTHEXIS_VERSION` names the
CortHeXis release it matches; the CI checks it with `scripts/check-corthexis-sync.sh`
(code compared without docstrings, module paths and command names). A fix lands in both,
or the CI says which file drifted.
