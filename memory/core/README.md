# memory-core — the CortHeXis memory engine (SOKKAN 3.0)

Self-contained package (no import from the SOKKAN backend or Magnitude),
configured with `CORTHEXIS_*` variables — the `SOKKAN_*` names and
`ML_SERVICE_URL` of SOKKAN are read as a fallback. It will move to the public
`ninabot-ch/corthexis` repository; SOKKAN depends on it.

| File | Role | Dependencies |
|---|---|---|
| `embed.py` | embedding client: `identity()`, `embed_docs()`, `embed_query()`, `rerank()` | httpx (fastembed for the legacy profile) |
| `models.py` | model registry, Gemma licence gate, first-run download + SHA-256 | stdlib |
| `profiles.py` | profiles `leger / standard / gpu`, costs, hardware detection and recommendation | stdlib |
| `eval.py` | recall bench on the client's own notes: questions (transcripts, written, generated), runs, comparison, regression findings, nightly | psycopg (store) |
| `switch.py` | profile / model change gated by the bench: background build, atomic switch, approve / cancel, 7-day rollback | store, embed, indexer |

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
compose profile `rerank`) and the one-shot `corthexis-embed-fetch`. GPU:
`compose.sycl.yml` (Intel, SYCL) or `compose.cuda.yml` add `corthexis-embed-gpu`
in front of the CPU server. Apple GPU: Docker has no Metal — run
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
