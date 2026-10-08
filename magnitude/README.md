# Magnitude — SOKKAN host agent

Measure what your machine can really run. Locally. Privately.

Magnitude is the host-side agent of SOKKAN's **Magnitude** feature. It profiles
the hardware of the machine it runs on (GPU, VRAM, CPU, RAM), benchmarks local
GGUF models with a prebuilt [llama.cpp](https://github.com/ggml-org/llama.cpp),
serves one with `llama-server`, and exposes it to the SOKKAN cockpit through an
Anthropic-compatible shim — so agent sessions run on your own hardware, zero
cloud, zero cost per token.

## Install

None. The package is **pure Python stdlib** (Python ≥ 3.9) — no `pip install`,
no virtualenv. Copy (or clone) the `magnitude/` directory onto the GPU machine
and run it with `python3 -m magnitude`.

## Usage

Pair from the cockpit: open the **Magnitude** tab, click *Pair a machine*, and
run the one-liner it shows on the host:

```sh
curl -fsSL "https://your-cockpit.example/api/magnitude/install.sh?token=<token>" | sh
```

The bootstrap lays down a standalone Python if the machine has none (macOS
without Command Line Tools — no `sudo`, no system changes, everything under
`~/.sokkan`), fetches this agent, and pairs. Works on Linux, macOS and Windows
(Git Bash/WSL). The manual form is equivalent when you already have Python 3.9+:

```sh
python3 -m magnitude --cockpit=https://your-cockpit.example --token=<pairing-token>
```

The agent then polls the cockpit every 2 s (outbound HTTP only — it never
accepts connections from the cockpit, so it works behind NAT/firewalls) and
executes commands you trigger from the UI: benchmark a model, run it, stop it.
Downloads (llama.cpp runtime + model weights) are cached under
`~/.sokkan/magnitude/`.

One-shot hardware profile (no cockpit needed):

```sh
python3 -m magnitude --profile
```

## Platforms

- **Linux x86_64** — Vulkan prebuilt (works on NVIDIA/AMD/Intel); on NVIDIA,
  `nvidia-smi` is used for VRAM detection and power sampling during benchmarks
  (€/M tokens estimate at 0.30 €/kWh).
- **macOS Apple Silicon** — Metal prebuilt; usable VRAM is estimated at 75 % of
  unified memory. No power sampling.
- **Windows x86_64** — Vulkan prebuilt (best effort).
- **Intel discrete GPUs** (Arc, Arc Pro B60, Data Center Max) — found through Level Zero /
  OpenCL (`xpu-smi`, then `clinfo`, `sycl-ls`, PCI ids), VRAM per card. Several cards are
  several devices of **one** node: the class is computed on the total VRAM and per card.
  Magnitude's own models use them only through a Vulkan driver (Mesa `intel_icd`); without
  it they run on CPU.
- No GPU? CPU inference is still offered when the machine has ≥ 8 GB RAM.

### Engines already running

The agent also scans the node for OpenAI-compatible servers it did not start (vLLM,
llama.cpp, ollama…) every 30 s: `GET /v1/models` on `MAGNITUDE_DISCOVER_PORTS` (default
`8000-8010,11434,8790`, 127.0.0.1). Each model found is shown under the node with its
engine, port, context and GPU cards (read from the container's `ZE_AFFINITY_MASK` /
`ONEAPI_DEVICE_SELECTOR` / `CUDA_VISIBLE_DEVICES`, or `MAGNITUDE_ENGINE_CARDS='{"8003":"1"}'`).
**Use for sessions** puts the shim (Anthropic ⇄ OpenAI) in front of that engine — nothing
is downloaded or restarted — and **Connect to SOKKAN** then routes new sessions to it.
**Detach** removes the shim; the engine keeps running. A Claude Code session opens at
~41k tokens of prompt: an engine with a shorter context is flagged.

llama.cpp: `MAGNITUDE_LLAMA_TAG=<tag>` pins a release; otherwise a build already under
`~/.sokkan/magnitude/bin` is reused, else the newest release (pre-releases included — upstream
publishes its `bNNNNN` builds that way) that ships a build for the platform.

**Runtime per backend** (`MAGNITUDE_RUNTIME=prebuilt|docker` forces it): Intel discrete cards
with a card allowed and a Docker daemon → `ghcr.io/ggml-org/llama.cpp:server-intel` (SYCL) on the
allowed cards only; otherwise Vulkan prebuilt (NVIDIA, AMD, Intel without Docker), Metal
(Apple), CPU (no GPU, or `MAGNITUDE_GPU_DEVICES=none`).

**Live load** (agent ≥ 0.3): every 3 s, per card busy %, VRAM used/total, temperature, power,
plus CPU/RAM — shown per card in the cockpit and exported by `GET /metrics` (`sokkan_magnitude_*`,
Bearer `SOKKAN_METRICS_TOKEN` or direct loopback). Intel VRAM figures need root (debugfs) or
read access to the engines' `/proc/<pid>/fdinfo`.

## Tuning (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `MAGNITUDE_CTX` | `16384` | `llama-server` context size. A Claude Code session opens at **~40k prompt tokens** (measured) — use `65536` for real agent sessions |
| `MAGNITUDE_KV` | *(f16)* | Quantized KV cache, e.g. `q8_0` — halves KV VRAM at 64k (~4.7 GB → ~2.4 GB on an 8B), forces flash attention |
| `MAGNITUDE_SERVER_ARGS` | *(empty)* | Extra `llama-server` flags, space-separated. Gotcha: llama-server caps per-request context at the model's training window even with YaRN — unlock with `--override-kv <arch>.context_length=int:65536` (e.g. `qwen3.context_length`) |
| `MAGNITUDE_LLAMA_TAG` | *(local build, else newest with a build)* | Pin the llama.cpp release |
| `MAGNITUDE_VISION` | `0` | `1` = the served model reads images: the shim forwards Anthropic `image` blocks (including those inside a `tool_result`, e.g. Claude Code reading a `.png`) as OpenAI `image_url` data URLs. Requires a vision model served with its `--mmproj` (pass it via `MAGNITUDE_SERVER_ARGS`). Off: each image is replaced by an explicit "image omitted" note |
| `MAGNITUDE_DISCOVER_PORTS` | `8000-8010,11434,8790` | Ports scanned for engines already running (ranges and lists) |
| `MAGNITUDE_ENGINE_CARDS` | *(empty)* | JSON port → GPU indices, when the container env does not say it |
| `MAGNITUDE_GPU_DEVICES` | *(all)* | Cards Magnitude's own Run/Benchmark may use (the numbers the cockpit shows), or `none` = CPU only (`--device none`). Shown in the cockpit as « Cards for Run »; fit is computed on their free memory |
| `MAGNITUDE_CPU_THREADS` | cores/4 (2–8) | Threads of a CPU-only run |
| `MAGNITUDE_RUNTIME` | `auto` | `prebuilt` or `docker` (Intel SYCL image) |
| `MAGNITUDE_DOCKER_IMAGE` / `_MEMORY` / `_CPUS` | `…:server-intel` / `16g` / `8` | Docker runtime image and caps |
| `MAGNITUDE_CACHE_RAM` | `2048` | `llama-server --cache-ram` (MiB of host RAM for the prompt cache; upstream default 8192) |
| `MAGNITUDE_HOME` | `~/.sokkan/magnitude` | Cache directory (runtimes, GGUF weights, logs) |

Rule of thumb for coding sessions: weights + KV must fit — an 8B Q4 at 64k/q8
needs ~8 GB; a 24 GB class-L GPU (or 32 GB Apple Silicon) runs the 30B-A3B MoE
coder comfortably. Class-M cards (11–16 GB) are fine for chat and batch, tight
for long agent sessions.

## Security

- The **pairing token** is shown once in the cockpit UI; the backend only
  stores its SHA-256. It is sent as the `x-magnitude-token` header on each sync.
- `llama-server` binds `127.0.0.1:8791` only — never exposed.
- The shim binds `0.0.0.0:8790` so the cockpit container can reach it, but
  refuses every request without the per-serve **serve token** (generated fresh
  each time a model is started, sent as `x-api-key` or `Authorization: Bearer`).
- The agent opens no other port and makes outbound requests only (cockpit sync,
  GitHub releases, Hugging Face downloads).

## Credits

This feature is named in homage to
[Magnitude](https://github.com/magnitudedev/magnitude) by Tom Greenwald and
Anders Lie (YC S25), whose hardware-profiling onboarding — profile the machine,
recommend what it can actually run, show the value immediately — is the pattern
this implementation follows. No code is shared: this is an independent,
from-scratch implementation in pure-stdlib Python. Not affiliated with or
endorsed by Magnitude.
