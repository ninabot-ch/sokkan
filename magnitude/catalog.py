#!/usr/bin/env python3
"""catalog.py — SOKKAN Magnitude : catalogue des modèles servables + calcul du fit.

IDs/poids/URLs = spec §2.6, dupliqués à l'identique côté backend
(backend/magnitude.py) — toute modification doit être répercutée des deux côtés.
Quantisation Q4_K_M partout (MXFP4 pour gpt-oss-20b). URLs vérifiées HTTP 302
le 2026-08-10.
"""
from __future__ import annotations

# Marge KV-cache/compute ajoutée aux poids pour le check de fit.
CTX_MARGIN_GB = 1.2

_HF = "https://huggingface.co"

CATALOG = [
    {"id": "qwen3-4b", "label": "Qwen3 4B", "params": "4B", "moe": False,
     "weights_gb": 2.5, "note": "light & quick",
     "url": f"{_HF}/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf"},
    {"id": "qwen3-8b", "label": "Qwen3 8B", "params": "8B", "moe": False,
     "weights_gb": 5.0, "note": "balanced daily driver",
     "url": f"{_HF}/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf"},
    {"id": "llama-3.1-8b", "label": "Llama 3.1 8B", "params": "8B", "moe": False,
     "weights_gb": 4.9, "note": "classic all-rounder",
     "url": f"{_HF}/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"},
    {"id": "qwen3-14b", "label": "Qwen3 14B", "params": "14B", "moe": False,
     "weights_gb": 9.0, "note": "strong reasoning",
     "url": f"{_HF}/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf"},
    {"id": "phi-4-14b", "label": "Phi-4 14B", "params": "14B", "moe": False,
     "weights_gb": 9.1, "note": "compact quality",
     "url": f"{_HF}/bartowski/phi-4-GGUF/resolve/main/phi-4-Q4_K_M.gguf"},
    {"id": "gpt-oss-20b", "label": "GPT-OSS 20B", "params": "20B", "moe": True,
     "weights_gb": 12.2, "note": "OpenAI open-weight, fast MoE",
     "url": f"{_HF}/ggml-org/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf"},
    {"id": "qwen3-30b-a3b", "label": "Qwen3 30B-A3B", "params": "30B", "moe": True,
     "weights_gb": 18.6, "note": "MoE — big brain, quick tokens",
     "url": f"{_HF}/Qwen/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf"},
    {"id": "qwen3-coder-30b", "label": "Qwen3 Coder 30B-A3B", "params": "30B", "moe": True,
     "weights_gb": 18.6, "note": "the local coding reference",
     "url": f"{_HF}/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"},
    {"id": "qwen3-32b", "label": "Qwen3 32B", "params": "32B", "moe": False,
     "weights_gb": 19.8, "note": "dense heavyweight",
     "url": f"{_HF}/Qwen/Qwen3-32B-GGUF/resolve/main/Qwen3-32B-Q4_K_M.gguf"},
    {"id": "llama-3.3-70b", "label": "Llama 3.3 70B", "params": "70B", "moe": False,
     "weights_gb": 42.5, "note": "near-frontier open weights",
     "url": f"{_HF}/unsloth/Llama-3.3-70B-Instruct-GGUF/resolve/main/Llama-3.3-70B-Instruct-Q4_K_M.gguf"},
]


def get(model_id: str) -> dict:
    for entry in CATALOG:
        if entry["id"] == model_id:
            return entry
    raise ValueError(f"unknown model: {model_id}")


def usable_gb(profile) -> float:
    """VRAM utilisable (Go) : vram_total_gb du GPU (Apple = 75 % RAM, déjà
    intégré au profil par hw.py), sinon 50 % de la RAM en classe CPU.
    0.0 = machine non servable — même sémantique que backend/magnitude.py."""
    gpu = (profile or {}).get("gpu") or {}
    if gpu.get("vram_total_gb"):
        return float(gpu["vram_total_gb"])
    if (profile or {}).get("class") == "CPU":
        ram = (profile or {}).get("ram_gb") or 0.0
        return round(ram * 0.5, 1)
    return 0.0


def fit(weights_gb: float, usable: float) -> str:
    """comfortable si poids+marge ≤ 85 % de l'utilisable ; tight si ≤ utilisable ; sinon no."""
    need = weights_gb + CTX_MARGIN_GB
    if usable and need <= usable * 0.85:
        return "comfortable"
    if usable and need <= usable:
        return "tight"
    return "no"


def annotate(profile) -> list:
    """Catalogue enrichi de fits/fit vs le profil (fit 'unknown' sans profil)."""
    out = []
    usable = usable_gb(profile) if profile else None
    for entry in CATALOG:
        e = dict(entry)
        if profile is None:
            e["fit"], e["fits"] = "unknown", False
        else:
            e["fit"] = fit(entry["weights_gb"], usable)
            e["fits"] = e["fit"] != "no"
        out.append(e)
    return out
