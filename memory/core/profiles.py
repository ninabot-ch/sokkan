#!/usr/bin/env python3
"""profiles.py — memory profiles (leger | standard | gpu): table, costs, recommendation.

  python3 profiles.py                  # JSON (schema corthexis/memory-profile/v1)
  python3 profiles.py --env [PREFIX]   # PREFIX_MEMORY_PROFILE=… lines (default CORTHEXIS)

Pure standard library and self-contained (no import from its package): the
same file ships with the SOKKAN Magnitude host agent as `magnitude/memprofile.py`
(`python3 -m magnitude --memory-profile`). Keep the two copies identical —
a test checks it.

Profiles (figures from the memory bench of 03.10.2026: 300 questions, 2 498
chunks, i9-9980XE shared with a production load, Arc Pro B60 for the GPU pass):

  leger     EmbeddingGemma-300m Q8 on CPU, no reranker                4 cores, 4 GB
  standard  EmbeddingGemma-300m Q8 on CPU, reranker in background only  8 cores, 16 GB
  gpu       EmbeddingGemma-300m on GPU + Qwen3-Reranker-0.6B, top 10    GPU ≥ 4 GB
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys

SCHEMA = "corthexis/memory-profile/v1"

LEGER_MIN_RAM_GB = 4      # below: still leger, flagged
STANDARD_MIN_CORES = 8
STANDARD_MIN_RAM_GB = 15  # a "16 GB" machine reports ~15.5
GPU_MIN_VRAM_GB = 4       # embedding +0.5 GB, reranker +3.0 GB measured on a B60

# Costs per profile. ram_gb / vram_gb = model servers only (the application and
# its database come on top). reindex_250k_h = initial import of 250 000 chunks.
# rerank_ms_top10 on CPU (standard) is the bench figure for bge-reranker-v2-m3.
PROFILES: dict[str, dict] = {
    "leger": {
        "label": "Léger", "embed": "embeddinggemma-300m-q8", "embed_device": "cpu",
        "reranker": None, "rerank_policy": "off",
        "min": {"cores": 4, "ram_gb": LEGER_MIN_RAM_GB},
        "costs": {"ram_gb": 1.2, "vram_gb": 0, "download_mb": 334,
                  "query_ms_p50": 53, "rerank_ms_top10": None,
                  "index_chunks_per_s": 1.8, "reindex_250k_h": 39,
                  "mrr": 0.82, "mrr_fallback": 0.77},
    },
    "standard": {
        "label": "Standard", "embed": "embeddinggemma-300m-q8", "embed_device": "cpu",
        "reranker": "bge-reranker-v2-m3-q8", "rerank_policy": "async",
        "min": {"cores": STANDARD_MIN_CORES, "ram_gb": STANDARD_MIN_RAM_GB},
        "costs": {"ram_gb": 3.5, "vram_gb": 0, "download_mb": 970,
                  "query_ms_p50": 35, "rerank_ms_top10": 4500,
                  "index_chunks_per_s": 3.0, "reindex_250k_h": 23,
                  "mrr": 0.82, "mrr_reranked": 0.84, "mrr_fallback": 0.77},
    },
    "gpu": {
        "label": "GPU", "embed": "embeddinggemma-300m-q8", "embed_device": "gpu",
        "reranker": "qwen3-reranker-0.6b-q8", "rerank_policy": "interactive",
        "min": {"vram_gb": GPU_MIN_VRAM_GB},
        "costs": {"ram_gb": 1.2, "vram_gb": 3.5, "download_mb": 973,
                  "query_ms_p50": 13, "rerank_ms_top10": 1300,
                  "index_chunks_per_s": 49.9, "reindex_250k_h": 1.4,
                  "mrr": 0.88, "mrr_fallback": 0.77},
    },
}
ORDER = ("leger", "standard", "gpu")

# llama.cpp server image tag per accelerator (docker/embed/compose.<accel>.yml)
IMAGES = {"cpu": "server", "cuda": "server-cuda", "sycl": "server-intel",
          "vulkan": "server-vulkan", "metal": None}
# Intel discrete GPUs (Arc A/B, Arc Pro, Data Center) by PCI device id family:
# lspci often prints only "Intel Corporation Device e211" with an older pci.ids.
_INTEL_DGPU_PREFIXES = ("0x56", "0xe2", "0x0bd")


# --------------------------------------------------------------------------- recommendation
def _gpu_ok(acc: dict | None) -> bool:
    if not acc:
        return False
    if acc.get("vram_gb") is None:  # Intel dGPU seen by PCI id only: every Arc has ≥ 4 GB
        return acc.get("kind") == "sycl"
    return acc["vram_gb"] >= GPU_MIN_VRAM_GB


def recommend(cores: int, ram_gb: float | None, acc: dict | None = None) -> dict:
    """Recommended profile for a machine.

    acc = {"kind": cuda|sycl|metal|vulkan, "name", "vram_gb"} or None (CPU only)."""
    cores, ram = int(cores or 0), float(ram_gb or 0)
    if acc:
        acc = {"kind": acc.get("kind"), "name": acc.get("name"), "vram_gb": acc.get("vram_gb"),
               "image": IMAGES.get(acc.get("kind") or ""),
               "serve": "native" if acc.get("kind") == "metal" else "docker"}
    warnings = []
    fits = {"leger": True,
            "standard": cores >= STANDARD_MIN_CORES and ram >= STANDARD_MIN_RAM_GB,
            "gpu": _gpu_ok(acc)}
    if fits["gpu"]:
        rec = "gpu"
        reason = f"GPU {acc['name'] or acc['kind']} ({acc['kind']})"
        if acc["vram_gb"]:
            reason += f", {acc['vram_gb']:g} GB"
        if acc["serve"] == "native":
            warnings.append("Apple GPU: run the memory servers natively (llama-server with "
                            "Metal); Docker on macOS has no GPU access")
    elif fits["standard"]:
        rec, reason = "standard", f"{cores} cores, {ram:g} GB RAM, no usable GPU"
    else:
        rec, reason = "leger", f"{cores} cores, {ram:g} GB RAM"
        if acc:
            reason += f" (GPU {acc['name'] or acc['kind']}: under {GPU_MIN_VRAM_GB} GB)"
    if ram and ram < LEGER_MIN_RAM_GB:
        warnings.append(f"{ram:g} GB RAM: below the {LEGER_MIN_RAM_GB} GB minimum — "
                        "expect swapping during indexing")
    if cores and cores < 4:
        warnings.append(f"{cores} cores: indexing will be slow (initial import measured "
                        "on 4 cores: ~39 h for 250 000 chunks)")
    return {
        "schema": SCHEMA, "recommended": rec, "reason": reason, "warnings": warnings,
        "hardware": {"cores": cores, "ram_gb": ram or None, "accelerator": acc},
        "accel": acc["kind"] if rec == "gpu" else "cpu",
        "profiles": [{"id": p, **PROFILES[p], "fits": fits[p]} for p in ORDER],
    }


def env_lines(rec: dict, prefix: str = "CORTHEXIS") -> str:
    return (f"{prefix}_MEMORY_PROFILE={rec['recommended']}\n"
            f"{prefix}_EMBED_ACCEL={rec['accel']}\n")


# --------------------------------------------------------------------------- detection
def _run(cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _ram_gb() -> float | None:
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        return round(int(out) / 1024 ** 3, 1) if out.isdigit() else None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return None


def _nvidia() -> dict | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if not out:
        return None
    best = None
    for line in out.splitlines():
        f = [x.strip() for x in line.split(",")]
        try:
            vram = round(float(f[1]) / 1024, 1)
        except (IndexError, ValueError):
            continue
        if not best or vram > best["vram_gb"]:
            best = {"kind": "cuda", "name": f[0], "vram_gb": vram}
    return best


def _intel_clinfo() -> list[dict]:
    if not shutil.which("clinfo"):
        return []
    devs, name = [], None
    for line in _run(["clinfo"]).splitlines():
        s = line.strip()
        if s.startswith("Device Name"):
            name = s.split("Device Name", 1)[1].strip()
        elif s.startswith("Global memory size") and name:
            m = re.match(r"Global memory size\s+(\d+)", s)
            if m and "intel" in name.lower() and re.search(r"arc|data center|max", name, re.I):
                devs.append({"name": name, "vram_gb": round(int(m.group(1)) / 1024 ** 3, 1)})
            name = None
    return devs


def _intel_sysfs(root: str = "/sys/bus/pci/devices") -> list[dict]:
    devs = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return devs
    for e in entries:
        vals = {}
        for k in ("vendor", "device", "class"):
            try:
                with open(os.path.join(root, e, k)) as fh:
                    vals[k] = fh.read().strip().lower()
            except OSError:
                break
        if (vals.get("vendor") == "0x8086" and vals.get("class", "").startswith("0x03")
                and vals.get("device", "").startswith(_INTEL_DGPU_PREFIXES)):
            devs.append({"name": f"Intel discrete GPU {vals['device']}", "vram_gb": None})
    return devs


def _intel() -> dict | None:
    devs = _intel_clinfo() or _intel_sysfs()
    if not devs:
        return None
    vram = [d["vram_gb"] for d in devs if d["vram_gb"]]
    return {"kind": "sycl", "name": devs[0]["name"], "vram_gb": max(vram) if vram else None,
            "count": len(devs)}


def _apple(ram_gb: float | None) -> dict | None:
    if sys.platform != "darwin" or platform.machine().lower() not in ("arm64", "aarch64"):
        return None
    chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
    return {"kind": "metal", "name": chip,
            "vram_gb": round(ram_gb * 0.75, 1) if ram_gb else None}  # unified memory


def detect_accelerator(ram_gb: float | None = None) -> dict | None:
    """Order: NVIDIA (nvidia-smi) → Apple Silicon → Intel discrete (clinfo, PCI ids)."""
    return _nvidia() or _apple(ram_gb) or _intel()


def detect() -> dict:
    """Detect this machine and recommend a profile (stdlib only)."""
    ram = _ram_gb()
    return recommend(os.cpu_count() or 0, ram, detect_accelerator(ram))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rec = detect()
    if argv and argv[0] == "--env":
        print(env_lines(rec, argv[1] if len(argv) > 1 else "CORTHEXIS"), end="")
    else:
        print(json.dumps(rec, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
