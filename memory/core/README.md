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

## Servers and chain

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
| harrier-oss-v1-270m | MIT (model card) | fallback when the terms are declined or Gemma cannot be fetched |
| multilingual-e5-base | MIT | alternative fallback (`CORTHEXIS_EMBED_FALLBACK=multilingual-e5-base-q8`) |
| Qwen3-Reranker-0.6B, bge-reranker-v2-m3 | Apache-2.0 | rerankers |

**Open question on harrier-oss-v1-270m.** Its configuration is the one of
Gemma 3 270m (Gemma3TextModel, 18 layers, hidden size 640, 262 144-token
vocabulary). If it was fine-tuned from Gemma 3 weights, the Gemma Terms define
it as a *Model Derivative* and their use restrictions would follow it despite
the MIT tag. Until that is settled, an operator who declines the Gemma terms on
principle should pick `multilingual-e5-base-q8` (XLM-RoBERTa lineage, MIT;
MRR 0.74 against 0.77 for harrier).

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
| `EMBED_FALLBACK` | `harrier-oss-v1-270m-q8` | non-Gemma fallback |
| `MODEL_BASE_URL` | `https://huggingface.co` | mirror for the downloads |
