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

Pair from the cockpit: open the **Magnitude** tab, click *Pair this machine*,
and run the command it shows on the host:

```sh
python3 -m magnitude --cockpit https://your-cockpit.example --token <pairing-token>
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
- No GPU? CPU inference is still offered when the machine has ≥ 8 GB RAM.

Pin the llama.cpp release with `MAGNITUDE_LLAMA_TAG=<tag>` (default: latest).

## Security

- The **pairing token** is shown once in the cockpit UI; the backend only
  stores its SHA-256. It is sent as the `x-magnitude-token` header on each sync.
- `llama-server` binds `127.0.0.1:8791` only — never exposed.
- The shim binds `0.0.0.0:8790` so the cockpit container can reach it, but
  refuses every request without the per-serve **serve token** (generated fresh
  each time a model is started, sent as `x-api-key` or `Authorization: Bearer`).
- The agent opens no other port and makes outbound requests only (cockpit sync,
  GitHub releases, Hugging Face downloads).
