#!/usr/bin/env python3
"""hw.py — SOKKAN Magnitude : profil hardware de la machine hôte.

Détection GPU dans l'ordre : nvidia-smi → Apple Silicon (darwin/arm64, sysctl)
→ vulkaninfo → lspci. Sans GPU exploitable : classe CPU si ram_gb ≥ 8 (VRAM
utilisable = 50 % RAM), sinon unsupported. Apple Silicon : VRAM utilisable =
75 % de la RAM unifiée, backend metal.

Schéma produit : sokkan-magnitude/profile/v1 (spec §2.5).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys

# Classes par VRAM utilisable (Go) : XL ≥ 30, L ≥ 22, M ≥ 10, S ≥ 6.
_CLASS_THRESHOLDS = [(30, "XL"), (22, "L"), (10, "M"), (6, "S")]


def _run(cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _os_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return m or "unknown"


def _float(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _guess_vendor(name: str) -> str:
    n = name.lower()
    if "nvidia" in n or "geforce" in n or "quadro" in n or "tesla" in n:
        return "nvidia"
    if "amd" in n or "radeon" in n:
        return "amd"
    if "intel" in n:
        return "intel"
    if "apple" in n:
        return "apple"
    return "none"


def _detect_nvidia():
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version,power.limit",
                "--format=csv,noheader,nounits"])
    if not out:
        return None
    f = [x.strip() for x in out.splitlines()[0].split(",")]
    if len(f) < 5:
        return None
    mem_total, mem_free = _float(f[1]), _float(f[2])
    return {
        "vendor": "nvidia", "name": f[0], "backend": "cuda",
        "vram_total_gb": round(mem_total / 1024, 1) if mem_total else None,
        "vram_free_gb": round(mem_free / 1024, 1) if mem_free else None,
        "driver": f[3], "power_limit_w": _float(f[4]),
    }


def _detect_apple(ram_gb):
    """Apple Silicon : mémoire unifiée, 75 % utilisable comme VRAM, backend metal."""
    chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
    usable = round(ram_gb * 0.75, 1) if ram_gb else None
    return {
        "vendor": "apple", "name": chip, "backend": "metal",
        "vram_total_gb": usable, "vram_free_gb": usable,
        "driver": None, "power_limit_w": None,
    }


def _detect_vulkan():
    if not shutil.which("vulkaninfo"):
        return None
    out = _run(["vulkaninfo", "--summary"])
    m = re.search(r"deviceName\s*=\s*(.+)", out)
    if not m:
        return None
    name = m.group(1).strip()
    return {"vendor": _guess_vendor(name), "name": name, "backend": "vulkan",
            "vram_total_gb": None, "vram_free_gb": None,
            "driver": None, "power_limit_w": None}


def _detect_lspci():
    out = _run(["sh", "-c", "lspci 2>/dev/null | grep -Ei 'vga|3d controller'"])
    if not out:
        return None
    name = out.splitlines()[0].split(": ", 1)[-1].strip()
    # GPU visible mais pas de loader Vulkan ni CUDA → inference CPU seulement.
    return {"vendor": _guess_vendor(name), "name": name, "backend": "cpu",
            "vram_total_gb": None, "vram_free_gb": None,
            "driver": None, "power_limit_w": None}


def _detect_host_darwin():
    cpu = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    mem = _float(_run(["sysctl", "-n", "hw.memsize"]))
    ram_gb = round(mem / (1024 ** 3), 1) if mem else None
    return cpu, os.cpu_count() or 0, ram_gb


def _detect_host_linux():
    cpu = ""
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    ram_gb = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    ram_gb = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except OSError:
        pass
    return cpu, os.cpu_count() or 0, ram_gb


def _machine_class(gpu, ram_gb) -> str:
    vram = (gpu or {}).get("vram_total_gb")
    if vram:
        for floor, cls in _CLASS_THRESHOLDS:
            if vram >= floor:
                return cls
    # Pas de VRAM exploitable → CPU (50 % RAM utilisable) si assez de mémoire.
    if (ram_gb or 0) >= 8:
        return "CPU"
    return "unsupported"


def detect_gpu(os_name=None, arch=None, ram_gb=None):
    """Dict gpu (schéma §2.5) ou None. Ordre : nvidia → apple → vulkan → lspci."""
    gpu = _detect_nvidia()
    if gpu:
        return gpu
    if (os_name or _os_name()) == "darwin" and (arch or _arch()) == "arm64":
        return _detect_apple(ram_gb)
    return _detect_vulkan() or _detect_lspci()


def build_profile() -> dict:
    os_name, arch = _os_name(), _arch()
    if os_name == "darwin":
        cpu, cores, ram_gb = _detect_host_darwin()
    else:
        cpu, cores, ram_gb = _detect_host_linux()
    gpu = detect_gpu(os_name, arch, ram_gb)
    return {
        "schema": "sokkan-magnitude/profile/v1",
        "os": os_name, "arch": arch,
        "gpu": gpu,
        "cpu": cpu, "cores": cores, "ram_gb": ram_gb,
        "class": _machine_class(gpu, ram_gb),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(build_profile(), indent=2))
