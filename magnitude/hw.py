#!/usr/bin/env python3
"""hw.py — SOKKAN Magnitude : profil hardware de la machine hôte.

Détection GPU dans l'ordre : nvidia-smi → Apple Silicon (darwin/arm64, sysctl)
→ Intel discrete (Level Zero : xpu-smi / sycl-ls / clinfo / sysfs) → vulkaninfo → lspci.
Plusieurs cartes = plusieurs `devices` sur UN node (décision 2026-09-09 : 1 machine =
1 node) ; `vram_total_gb` = somme des cartes, `vram_per_card_gb` = la plus grande,
classe calculée sur le total (`class`) et sur une carte (`class_per_card`). Sans GPU exploitable : classe CPU si ram_gb ≥ 8 (VRAM
utilisable = 50 % RAM), sinon unsupported. Apple Silicon : VRAM utilisable =
75 % de la RAM unifiée, backend metal.

Schéma produit : sokkan-magnitude/profile/v1 (spec §2.5).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import socket
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
    rows = [[x.strip() for x in ln.split(",")] for ln in out.splitlines()]
    rows = [f for f in rows if len(f) >= 5 and _float(f[1]) is not None]
    if not rows:   # "NVIDIA-SMI has failed…" (driver gone) lands on stdout
        return None
    devs = [{"index": i, "name": f[0],
             "vram_gb": round(_float(f[1]) / 1024, 1),
             "vram_free_gb": round(_float(f[2]) / 1024, 1) if _float(f[2]) is not None else None}
            for i, f in enumerate(rows)]
    f = rows[0]
    total = round(sum(d["vram_gb"] for d in devs), 1)
    free = [d["vram_free_gb"] for d in devs if d["vram_free_gb"] is not None]
    names = sorted({d["name"] for d in devs})
    return {
        "vendor": "nvidia", "backend": "cuda",
        "name": (f"{len(devs)}× {names[0]}" if len(names) == 1 else " + ".join(names))
                if len(devs) > 1 else f[0],
        "count": len(devs), "devices": devs,
        "vram_total_gb": total,
        "vram_per_card_gb": max(d["vram_gb"] for d in devs),
        "vram_free_gb": round(sum(free), 1) if free else None,
        "driver": f[3], "power_limit_w": _float(f[4]),
    }


# Intel discrete GPU PCI device ids (Arc A-series 0x56xx, Arc B-series / Pro B60 0xe2xx,
# Data Center GPU Max 0x0bdx) — same list as memory/core/profiles.py
_INTEL_DGPU_PREFIXES = ("0x56", "0xe2", "0x0bd")
_INTEL_IGPU = re.compile(r"\b(UHD|Iris|HD Graphics)\b", re.I)


def _intel_xpu_smi():
    """`xpu-smi discovery -j` → [{index, name, vram_gb, pci}] (Intel XPU Manager)."""
    if not shutil.which("xpu-smi"):
        return []
    try:
        import json
        d = json.loads(_run(["xpu-smi", "discovery", "-j"]) or "{}")
    except ValueError:
        return []
    out = []
    for i, dev in enumerate(d.get("device_list") or []):
        name = dev.get("device_name") or "Intel GPU"
        mem = None
        did = dev.get("device_id", i)
        try:  # per-device detail carries the memory size
            det = json.loads(_run(["xpu-smi", "discovery", "-d", str(did), "-j"]) or "{}")
            mem = _float(det.get("memory_physical_size_byte") or det.get("max_mem_alloc_size_byte"))
        except ValueError:
            pass
        if mem is None:
            mem = _float(dev.get("memory_physical_size_byte"))
        out.append({"index": int(did) if str(did).isdigit() else i, "name": name,
                    "vram_gb": round(mem / 1024 ** 3, 1) if mem else None,
                    "pci": dev.get("pci_bdf_address") or None})
    return out


def _intel_clinfo():
    """`clinfo` (OpenCL) → Intel devices with their global memory."""
    if not shutil.which("clinfo"):
        return []
    out, name, pci, drv = [], None, None, None
    for line in _run(["clinfo"]).splitlines():
        s = line.strip()
        if s.startswith("Device Name"):
            name, pci, drv = s.split("Device Name", 1)[1].strip(), None, None
        elif s.startswith("Driver Version") and name:
            drv = s.split("Driver Version", 1)[1].strip() or None
        elif s.startswith("Device PCI bus info") and name:
            m = re.search(r"([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])", s, re.I)
            pci = m.group(1) if m else None
        elif s.startswith("Global memory size") and name:
            m = re.match(r"Global memory size\s+(\d+)", s)
            if m and "intel" in name.lower():
                out.append({"index": len(out), "name": name,
                            "vram_gb": round(int(m.group(1)) / 1024 ** 3, 1), "pci": pci,
                            "driver": drv})
            name = None
    return out


def _intel_sycl_ls():
    """`sycl-ls` → Level Zero GPU names (no memory figure)."""
    if not shutil.which("sycl-ls"):
        return []
    out = []
    for line in _run(["sycl-ls"]).splitlines():
        m = re.match(r"\s*\[(?:level_zero|ext_oneapi_level_zero):gpu\]\s*(?:\[[^\]]*\]\s*)?(.+)", line)
        if m:
            name = m.group(1).split(",")[-1].strip()
            name = re.sub(r"\s+\d+\.\d+\.\d+\s*\[.*$", "", name).strip()
            out.append({"index": len(out), "name": name, "vram_gb": None, "pci": None})
    return out


def _intel_sysfs(root="/sys/bus/pci/devices"):
    out = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
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
            out.append({"index": len(out), "name": f"Intel discrete GPU {vals['device']}",
                        "vram_gb": None, "pci": e})
    return out


def _level_zero_present():
    if shutil.which("sycl-ls") or shutil.which("xpu-smi"):
        return True
    for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib", "/usr/local/lib"):
        try:
            if any(f.startswith("libze_loader.so") for f in os.listdir(d)):
                return True
        except OSError:
            continue
    return False


def _vulkan_icd(vendor):
    """A Vulkan loader + an ICD of this vendor: Magnitude's llama.cpp build (Vulkan)
    can offload to these cards."""
    has_loader = False
    for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib", "/usr/local/lib"):
        try:
            if any(f.startswith("libvulkan.so") for f in os.listdir(d)):
                has_loader = True
                break
        except OSError:
            continue
    if not has_loader:
        return False
    for d in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d"):
        try:
            if any(f.startswith(vendor) for f in os.listdir(d)):
                return True
        except OSError:
            continue
    return False


def _detect_intel():
    """Intel discrete GPUs (Arc / Arc Pro / Data Center Max) — every card a device of
    this node. Sources, first that lists cards wins for names; memory filled from clinfo
    when the first source has none. Integrated GPUs (UHD / Iris) are ignored."""
    devs = []
    for source in (_intel_xpu_smi, _intel_clinfo, _intel_sycl_ls, _intel_sysfs):
        try:
            devs = [d for d in source() if not _INTEL_IGPU.search(d["name"] or "")]
        except Exception:  # noqa: BLE001 — a broken tool = try the next one
            devs = []
        if devs:
            if any(d["vram_gb"] is None for d in devs) and source is not _intel_clinfo:
                mem = [d["vram_gb"] for d in _intel_clinfo() if not _INTEL_IGPU.search(d["name"])]
                if len(mem) == len(devs):
                    for d, v in zip(devs, mem):
                        d["vram_gb"] = d["vram_gb"] or v
            break
    if not devs:
        return None
    vram = [d["vram_gb"] for d in devs if d["vram_gb"]]
    names = sorted({d["name"] for d in devs})
    name = names[0] if len(names) == 1 else " + ".join(names)
    return {
        "vendor": "intel",
        "name": f"{len(devs)}× {name}" if len(devs) > 1 else name,
        "backend": "level_zero" if _level_zero_present() else "opencl",
        "count": len(devs),
        "devices": devs,
        "vram_total_gb": round(sum(vram), 1) if vram else None,
        "vram_per_card_gb": max(vram) if vram else None,
        "vram_free_gb": None,
        "driver": next((d.get("driver") for d in devs if d.get("driver")), None),
        "power_limit_w": None,
        # Magnitude's own engine is the Vulkan build of llama.cpp: it can use these cards
        # only through a Vulkan driver; the engines already running (SYCL, vLLM-XPU) are
        # discovered apart (magnitude/discover.py)
        "offload": "vulkan" if _vulkan_icd("intel") else None,
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


def _detect_host_windows():
    cpu = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
    ram_gb = None
    try:  # GlobalMemoryStatusEx — stdlib ctypes, pas de wmic (déprécié)
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            ram_gb = round(st.ullTotalPhys / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001 — profil dégradé plutôt que crash
        pass
    return cpu, os.cpu_count() or 0, ram_gb


def _machine_class(gpu, ram_gb, per_card=False) -> str:
    vram = (gpu or {}).get("vram_per_card_gb" if per_card else "vram_total_gb") \
        or (gpu or {}).get("vram_total_gb")
    if vram:
        for floor, cls in _CLASS_THRESHOLDS:
            if vram >= floor:
                return cls
    # Pas de VRAM exploitable → CPU (50 % RAM utilisable) si assez de mémoire.
    if (ram_gb or 0) >= 8:
        return "CPU"
    return "unsupported"


def detect_gpu(os_name=None, arch=None, ram_gb=None):
    """Dict gpu (schéma §2.5) ou None. Ordre : nvidia → apple → intel → vulkan → lspci."""
    gpu = _detect_nvidia()
    if gpu:
        return gpu
    if (os_name or _os_name()) == "darwin" and (arch or _arch()) == "arm64":
        return _detect_apple(ram_gb)
    if (os_name or _os_name()) == "linux":
        gpu = _detect_intel()
        if gpu:
            return gpu
    return _detect_vulkan() or _detect_lspci()


def build_profile() -> dict:
    os_name, arch = _os_name(), _arch()
    if os_name == "darwin":
        cpu, cores, ram_gb = _detect_host_darwin()
    elif os_name == "windows":
        cpu, cores, ram_gb = _detect_host_windows()
    else:
        cpu, cores, ram_gb = _detect_host_linux()
    gpu = detect_gpu(os_name, arch, ram_gb)
    try:
        hostname = socket.gethostname().split(".")[0]
    except OSError:
        hostname = ""
    return {
        "schema": "sokkan-magnitude/profile/v1",
        "os": os_name, "arch": arch,
        "hostname": hostname,  # nomme le node dans le registry du cockpit
        "gpu": gpu,
        "cpu": cpu, "cores": cores, "ram_gb": ram_gb,
        "class": _machine_class(gpu, ram_gb),
        "class_per_card": _machine_class(gpu, ram_gb, per_card=True),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(build_profile(), indent=2))
