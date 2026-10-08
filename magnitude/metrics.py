#!/usr/bin/env python3
"""metrics.py — SOKKAN Magnitude: live load of a node (agent ≥ 0.3), stdlib only.

`Sampler.sample()` returns one snapshot, sent by the agent with its sync every few
seconds and shown per card in the cockpit (plus `/metrics` for Prometheus):

    {"at": 1791460000.0,
     "cpu_pct": 12.5, "load1": 3.1, "ram_used_gb": 31.2, "ram_total_gb": 62.5,
     "gpus": [{"index": 0, "pci": "0000:19:00.0", "util_pct": 41.0,
               "vram_used_gb": 22.2, "vram_total_gb": 25.7,
               "temp_c": 52.0, "power_w": 61.3}, …]}

Every figure is optional (None when the platform does not expose it). Sources:

- NVIDIA: one `nvidia-smi --query-gpu=…` call.
- Intel (xe / i915 kernel driver), no extra tool needed:
  * VRAM used/total: debugfs `/sys/kernel/debug/dri/<pci>/tile0/vram_mm` (root),
    else the `drm-resident-vram0` of every DRM client in `/proc/*/fdinfo` (sum per card,
    only the processes we can read);
  * busy %: `tile0/gt0/gtidle/idle_residency_ms` (render/compute GT) between two samples;
  * temperature: hwmon `pkg` sensor; power: hwmon `energy1` counter (µJ) between two samples.
- AMD (amdgpu): `gpu_busy_percent`, `mem_info_vram_used/total`, hwmon temp1/power1.
- CPU/RAM: /proc/stat, /proc/loadavg, /proc/meminfo (Linux); macOS/Windows: None.

Card indexes follow PCI order, as in hw.py (devices[].index); `pci` lets the cockpit match.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time

GB = 1073741824.0   # GiB, as nvidia-smi / clinfo / hw.py report card memory


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _num(s) -> float | None:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def _r1(v):
    return None if v is None else round(v, 1)


# ------------------------------------------------------------------- CPU / RAM

def _cpu_times():
    s = _read("/proc/stat")
    if not s:
        return None
    f = [int(x) for x in s.splitlines()[0].split()[1:]]
    idle = f[3] + (f[4] if len(f) > 4 else 0)
    return sum(f), idle


def _meminfo():
    s = _read("/proc/meminfo")
    if not s:
        return None, None
    kv = {}
    for line in s.splitlines():
        k, _, v = line.partition(":")
        kv[k] = _num(v.split()[0]) if v.split() else None
    tot, avail = kv.get("MemTotal"), kv.get("MemAvailable")
    if not tot or avail is None:
        return None, None
    return (tot - avail) * 1024 / GB, tot * 1024 / GB


# ----------------------------------------------------------------------- GPUs

def _nvidia():
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,pci.bus_id,utilization.gpu,memory.used,"
             "memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    gpus = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 7:
            continue
        used, total = _num(f[3]), _num(f[4])
        gpus.append({"index": int(_num(f[0]) or 0), "pci": f[1].lower()[-12:],
                     "util_pct": _num(f[2]),
                     "vram_used_gb": _r1(used / 1024) if used is not None else None,
                     "vram_total_gb": _r1(total / 1024) if total is not None else None,
                     "temp_c": _num(f[5]), "power_w": _r1(_num(f[6]))})
    return gpus or None


def _drm_cards(driver_names):
    """[(pci, /sys/class/drm/cardN/device)] for the given kernel drivers, PCI order."""
    out = []
    for card in glob.glob("/sys/class/drm/card[0-9]*"):
        if not re.fullmatch(r"card\d+", os.path.basename(card)):
            continue
        dev = os.path.join(card, "device")
        drv = os.path.basename(os.path.realpath(os.path.join(dev, "driver")))
        if drv in driver_names:
            out.append((os.path.basename(os.path.realpath(dev)), dev))
    return sorted(set(out))


def _hwmon(dev):
    h = glob.glob(os.path.join(dev, "hwmon", "hwmon*"))
    return h[0] if h else None


def _fdinfo_vram():
    """{pci: bytes resident in VRAM} summed over the DRM clients we can read
    (each client counted once: one fd per drm-client-id)."""
    seen, out = set(), {}
    for fd in glob.glob("/proc/[0-9]*/fdinfo/*"):
        s = _read(fd)
        if not s or "drm-pdev" not in s:
            continue
        kv = dict(line.split(":", 1) for line in s.splitlines() if ":" in line)
        pdev, cid = kv.get("drm-pdev", "").strip(), kv.get("drm-client-id", "").strip()
        if (pdev, cid) in seen:
            continue
        seen.add((pdev, cid))
        v = kv.get("drm-resident-vram0") or kv.get("drm-memory-vram")
        if v:
            n, unit = (v.split() + ["KiB"])[:2]
            mult = {"KiB": 1024, "MiB": 1048576, "GiB": 1073741824}.get(unit, 1)
            out[pdev] = out.get(pdev, 0) + int(_num(n) or 0) * mult
    return out


def _xe_vram(pci):
    s = _read(f"/sys/kernel/debug/dri/{pci}/tile0/vram_mm")
    if not s:
        return None, None
    used = re.search(r"usage:\s*(\d+)", s)
    size = re.search(r"man size:\s*(\d+)", s)
    return (int(used.group(1)) if used else None), (int(size.group(1)) if size else None)


def _amd(dev):
    used = _num(_read(os.path.join(dev, "mem_info_vram_used")))
    total = _num(_read(os.path.join(dev, "mem_info_vram_total")))
    h = _hwmon(dev)
    temp = _num(_read(os.path.join(h, "temp1_input"))) if h else None
    power = _num(_read(os.path.join(h, "power1_average"))) if h else None
    return {"util_pct": _num(_read(os.path.join(dev, "gpu_busy_percent"))),
            "vram_used_gb": _r1(used / GB) if used is not None else None,
            "vram_total_gb": _r1(total / GB) if total is not None else None,
            "temp_c": _r1(temp / 1000) if temp is not None else None,
            "power_w": _r1(power / 1e6) if power is not None else None}


class Sampler:
    """Keeps the previous counters so busy % and watts are averages between samples."""

    def __init__(self, vram_totals: dict | None = None):
        self._cpu = _cpu_times()
        self._prev: dict = {}           # pci -> (t, idle_ms, energy_uj)
        self._last: dict = {}           # pci -> (util %, watts) of the last window
        self._vram_totals = vram_totals or {}   # pci -> GB from the hardware profile
        self._fdinfo_at, self._fdinfo = 0.0, {}
        # nvidia-smi can be installed without any NVIDIA card left (ROG1 after its 2080 Ti
        # went): it then fails in ~1.7 s on every call → asked once, then skipped
        self._nvidia_ok = bool(shutil.which("nvidia-smi"))

    def _intel(self, now):
        cards = _drm_cards(("xe", "i915"))
        need_fdinfo = False
        gpus = []
        # integrated / older i915 GPUs (no tile0) are not compute cards
        cards = [(pci, dev) for pci, dev in cards
                 if os.path.basename(os.path.realpath(os.path.join(dev, "driver"))) == "xe"
                 or os.path.exists(os.path.join(dev, "tile0"))]
        for i, (pci, dev) in enumerate(cards):
            used, size = _xe_vram(pci)
            if used is None:
                need_fdinfo = True
            h = _hwmon(dev)
            temp = None
            if h:
                for lab in glob.glob(os.path.join(h, "temp*_label")):
                    if (_read(lab) or "").strip() == "pkg":
                        temp = _num(_read(lab.replace("_label", "_input")))
                        break
            energy = _num(_read(os.path.join(h, "energy1_input"))) if h else None
            idle = _num(_read(os.path.join(dev, "tile0", "gt0", "gtidle", "idle_residency_ms")))
            # idle_residency_ms moves in coarse steps: averages over ≥ 2.5 s windows,
            # the last value is repeated in between
            prev = self._prev.get(pci)
            util, power = self._last.get(pci, (None, None))
            if prev is None or now - prev[0] >= 2.5:
                if prev:
                    dt = now - prev[0]
                    if idle is not None and prev[1] is not None:
                        util = max(0.0, min(100.0, 100.0 * (1 - (idle - prev[1]) / (dt * 1000))))
                    if energy is not None and prev[2] is not None and energy >= prev[2]:
                        power = (energy - prev[2]) / 1e6 / dt
                self._prev[pci] = (now, idle, energy)
                self._last[pci] = (util, power)
            total = size / GB if size else self._vram_totals.get(pci)
            gpus.append({"index": i, "pci": pci, "util_pct": _r1(util),
                         "vram_used_gb": _r1(used / GB) if used is not None else None,
                         "vram_total_gb": _r1(total),
                         "temp_c": _r1(temp / 1000) if temp is not None else None,
                         "power_w": _r1(power)})
        if need_fdinfo and gpus:
            if now - self._fdinfo_at > 10:      # /proc walk: at most every 10 s
                self._fdinfo, self._fdinfo_at = _fdinfo_vram(), now
            for g in gpus:
                if g["vram_used_gb"] is None and g["pci"] in self._fdinfo:
                    g["vram_used_gb"] = _r1(self._fdinfo[g["pci"]] / GB)
        return gpus or None

    def _amdgpu(self):
        gpus = []
        for i, (pci, dev) in enumerate(_drm_cards(("amdgpu",))):
            gpus.append({"index": i, "pci": pci, **_amd(dev)})
        return gpus or None

    def sample(self) -> dict:
        now = time.time()
        cpu_pct = None
        cur = _cpu_times()
        if cur and self._cpu and cur[0] > self._cpu[0]:
            cpu_pct = 100.0 * (1 - (cur[1] - self._cpu[1]) / (cur[0] - self._cpu[0]))
        self._cpu = cur
        load = _read("/proc/loadavg")
        used, total = _meminfo()
        gpus = None
        if self._nvidia_ok:
            gpus = _nvidia()
            self._nvidia_ok = bool(gpus)
        gpus = gpus or self._intel(now) or self._amdgpu() or []
        return {"at": round(now, 1), "cpu_pct": _r1(cpu_pct),
                "load1": _num(load.split()[0]) if load else None,
                "ram_used_gb": _r1(used), "ram_total_gb": _r1(total),
                "gpus": gpus}


if __name__ == "__main__":
    import json
    s = Sampler()
    s.sample()
    time.sleep(1)
    print(json.dumps(s.sample(), indent=2))
