"""3.2.3 — Magnitude live load, cards allowed for Run, stale nodes, Prometheus.

Agent (0.3): metrics.py reads xe sysfs/debugfs, hwmon and /proc from a fake tree;
engine.run_devices / _device_args honour MAGNITUDE_GPU_DEVICES (none = CPU only, no
backend device at all). Backend: samples kept in memory (never in magnitude.json),
fit computed on the FREE memory of the allowed cards (or the CPU), a Run that does not
fit refused, resend asked when the cockpit lacks the engines, stale nodes flagged,
/metrics in Prometheus text — loopback only without a token."""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "magnitude"
if "magnitude_agent" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("magnitude_agent", _PKG / "__init__.py",
                                                   submodule_search_locations=[str(_PKG)])
    _m = importlib.util.module_from_spec(_spec)
    sys.modules["magnitude_agent"] = _m
    _spec.loader.exec_module(_m)
import magnitude_agent.engine as engine  # noqa: E402
import magnitude_agent.metrics as metrics  # noqa: E402

B60 = "Intel(R) Arc(TM) Pro B60 Graphics"
GiB = 1073741824


# ------------------------------------------------------------------- agent side

def test_run_devices_and_device_args(monkeypatch):
    monkeypatch.delenv("MAGNITUDE_GPU_DEVICES", raising=False)
    assert engine.run_devices([0, 1, 2, 3]) == [0, 1, 2, 3]
    assert engine._device_args(True) == (["-ngl", "999"], {})
    for v in ("none", "cpu", "NONE", "-1"):
        monkeypatch.setenv("MAGNITUDE_GPU_DEVICES", v)
        assert engine.run_devices([0, 1, 2, 3]) == []
        args, env = engine._device_args(True)
        assert args[:4] == ["--device", "none", "-ngl", "0"] and env == {}
    monkeypatch.setenv("MAGNITUDE_GPU_DEVICES", "2, 9,2")
    assert engine.run_devices([0, 1, 2, 3]) == [2]        # unknown index dropped, no duplicate
    monkeypatch.setenv("MAGNITUDE_GPU_DEVICES", "2")
    assert engine._device_args(True) == (["-ngl", "999"], {"GGML_VK_VISIBLE_DEVICES": "2",
                                                           "CUDA_VISIBLE_DEVICES": "2"})
    # no usable GPU backend: CPU whatever the variable says
    assert engine._device_args(False)[0][:2] == ["--device", "none"]
    monkeypatch.setenv("MAGNITUDE_CPU_THREADS", "6")
    assert engine.cpu_threads() == 6


def _fake_xe(root: Path, pci: str, card: int, used: int, idle_ms: int, energy_uj: int):
    dev = root / "devices" / pci
    (dev / "tile0" / "gt0" / "gtidle").mkdir(parents=True)
    (dev / "tile0" / "gt0" / "gtidle" / "idle_residency_ms").write_text(str(idle_ms))
    hw = dev / "hwmon" / "hwmon9"
    hw.mkdir(parents=True)
    (hw / "temp2_label").write_text("pkg\n")
    (hw / "temp2_input").write_text("52000\n")
    (hw / "temp3_label").write_text("vram\n")
    (hw / "temp3_input").write_text("99000\n")
    (hw / "energy1_input").write_text(str(energy_uj))
    drv = root / "drivers" / "xe"
    drv.mkdir(parents=True, exist_ok=True)
    os.symlink(drv, dev / "driver")
    c = root / "class" / f"card{card}"
    c.mkdir(parents=True)
    os.symlink(dev, c / "device")
    dbg = root / "debug" / pci / "tile0"
    dbg.mkdir(parents=True)
    dbg.joinpath("vram_mm").write_text(f"  usage: {used}\ndefault_page_size: 4KiB\nman size:{24 * GiB}\n")
    return dev


def test_metrics_intel_xe_from_sysfs(tmp_path, monkeypatch):
    devs = [_fake_xe(tmp_path, "0000:19:00.0", 0, 20 * GiB, 1000, 10_000_000),
            _fake_xe(tmp_path, "0000:67:00.0", 1, 4 * GiB, 1000, 10_000_000)]
    real_glob, real_read = metrics.glob.glob, metrics._read

    def fake_glob(pat):
        if pat.startswith("/sys/class/drm/"):
            return real_glob(pat.replace("/sys/class/drm", str(tmp_path / "class")))
        return real_glob(pat)

    def fake_read(path):
        if path.startswith("/sys/kernel/debug/dri/"):
            return real_read(path.replace("/sys/kernel/debug/dri", str(tmp_path / "debug")))
        return real_read(path)

    monkeypatch.setattr(metrics.glob, "glob", fake_glob)
    monkeypatch.setattr(metrics, "_read", fake_read)
    monkeypatch.setattr(metrics.shutil, "which", lambda _x: None)   # no nvidia-smi
    s = metrics.Sampler()
    t0 = time.time()
    first = s._intel(t0)
    assert [g["pci"] for g in first] == ["0000:19:00.0", "0000:67:00.0"]
    assert first[0]["vram_used_gb"] == 20.0 and first[0]["vram_total_gb"] == 24.0
    assert first[0]["temp_c"] == 52.0          # the pkg sensor, not the hottest one
    assert first[0]["util_pct"] is None        # needs two samples
    # 3 s later: card 0 idle 1.5 s of 3 (50 % busy), 60 J spent (20 W); card 1 fully idle
    (devs[0] / "tile0/gt0/gtidle/idle_residency_ms").write_text("2500")
    (devs[0] / "hwmon/hwmon9/energy1_input").write_text(str(10_000_000 + 60_000_000))
    (devs[1] / "tile0/gt0/gtidle/idle_residency_ms").write_text("4000")
    second = s._intel(t0 + 3)
    assert second[0]["util_pct"] == 50.0 and second[0]["power_w"] == 20.0
    assert second[1]["util_pct"] == 0.0
    # within the 2.5 s window: the last values are repeated, not recomputed on noise
    assert s._intel(t0 + 4)[0]["util_pct"] == 50.0


def test_metrics_snapshot_shape():
    snap = metrics.Sampler().sample()
    assert set(snap) == {"at", "cpu_pct", "load1", "ram_used_gb", "ram_total_gb", "gpus"}
    if sys.platform.startswith("linux"):
        assert snap["ram_total_gb"] and snap["ram_used_gb"] is not None
    json.dumps(snap)


# ------------------------------------------------------------------ cockpit side

def _profile(run_devices=None):
    gpu = {"vendor": "intel", "name": f"4× {B60}", "backend": "level_zero",
           "vram_total_gb": 90.8, "vram_per_card_gb": 22.7, "offload": "vulkan",
           "devices": [{"index": i, "name": B60, "vram_gb": 22.7, "pci": f"0000:{i}0:00.0"}
                       for i in range(4)]}
    if run_devices is not None:
        gpu["run_devices"] = run_devices
    return {"schema": "sokkan-magnitude/profile/v1", "hostname": "rog1", "class": "XL",
            "class_per_card": "L", "ram_gb": 62.5, "cores": 36, "cpu": "i9", "gpu": gpu,
            "agent_version": "0.3.0"}


def _sample(used=(20.7, 20.9, 18.7, 18.8), ram_used=40.0):
    return {"cpu_pct": 23.0, "load1": 7.1, "ram_used_gb": ram_used, "ram_total_gb": 62.5,
            "gpus": [{"index": i, "pci": f"0000:{i}0:00.0", "util_pct": 10.0 * i,
                      "vram_used_gb": u, "vram_total_gb": 23.9, "temp_c": 50.0, "power_w": 33.0}
                     for i, u in enumerate(used)]}


@pytest.fixture
def reg(tmp_path, monkeypatch):
    import magnitude

    monkeypatch.setattr(magnitude, "STATE", tmp_path / "magnitude.json")
    monkeypatch.setattr(magnitude, "_METRICS", {})
    nid, _tok = magnitude.pair()
    return magnitude, nid


def test_metrics_kept_in_memory_and_viewed(reg):
    magnitude, nid = reg
    magnitude.sync(nid, {"profile": _profile(), "metrics": {**_sample(), "evil": "x"}}, False)
    raw = (magnitude.STATE).read_text()
    assert "cpu_pct" not in raw and "vram_used_gb" not in raw      # never on disk
    (node,) = magnitude.view()["nodes"]
    m = node["metrics"]
    assert m["gpus"][2]["vram_used_gb"] == 18.7 and "evil" not in m and len(m["history"]) == 1
    # garbage from an agent is dropped, not trusted
    magnitude.sync(nid, {"metrics": {"cpu_pct": "nan", "gpus": [{"index": "x", "util_pct": 1e99}]}}, False)
    m = magnitude.view()["nodes"][0]["metrics"]
    assert m["cpu_pct"] is None and m["gpus"][0]["util_pct"] is None
    # a stale sample is not shown
    magnitude._METRICS[nid]["last"]["at"] -= 3600
    assert magnitude.view()["nodes"][0]["metrics"] is None


def test_fit_on_allowed_cards_free_memory(reg):
    magnitude, nid = reg
    # unrestricted + live free memory of the 4 cards: 3.2+3.0+5.2+5.1 = 16.5 GB, not 90.8
    magnitude.sync(nid, {"profile": _profile(), "metrics": _sample()}, False)
    (node,) = magnitude.view()["nodes"]
    assert node["run_target"]["where"] == "gpu" and node["run_target"]["basis"] == "free"
    assert node["run_target"]["usable_gb"] == pytest.approx(16.5, abs=0.2)
    fit = {m["id"]: m["fit"] for m in node["catalog"]}
    assert fit["llama-3.3-70b"] == "no" and fit["qwen3-4b"] == "comfortable"
    # one card allowed, with room
    magnitude.sync(nid, {"profile": _profile([2]), "metrics": _sample(used=(20, 20, 4.0, 20))}, False)
    t = magnitude.view()["nodes"][0]["run_target"]
    assert t == {"where": "gpu", "cards": [2], "usable_gb": 19.9, "basis": "free"}
    # none allowed: the CPU, on the RAM still available
    magnitude.sync(nid, {"profile": _profile([]), "metrics": _sample(ram_used=40.0)}, False)
    t = magnitude.view()["nodes"][0]["run_target"]
    assert t["where"] == "cpu" and t["cards"] == [] and t["usable_gb"] == 18.0   # 22.5 × 0.8


def test_run_that_does_not_fit_is_refused(reg, monkeypatch):
    import app
    from fastapi import HTTPException

    magnitude, nid = reg
    monkeypatch.setattr(app.audit, "log", lambda *a, **k: None)
    magnitude.sync(nid, {"profile": _profile([]), "metrics": _sample(ram_used=58.0)}, False)
    admin = {"email": "root@x", "role": "admin"}
    with pytest.raises(HTTPException) as e:
        app.magnitude_cmd(app.MagnitudeCmdBody(node=nid, action="run", model="qwen3-8b"), u=admin, _f=None)
    assert e.value.status_code == 409 and "the CPU" in e.value.detail
    magnitude.sync(nid, {"metrics": _sample(ram_used=30.0)}, False)
    app.magnitude_cmd(app.MagnitudeCmdBody(node=nid, action="run", model="qwen3-4b"), u=admin, _f=None)
    assert magnitude.sync(nid, {}, False) == {"action": "run", "model": "qwen3-4b"}


def test_resend_and_stale(reg):
    magnitude, nid = reg
    assert magnitude.resend_needed(nid) == ["profile", "engines"]
    magnitude.sync(nid, {"profile": _profile(), "engines": []}, False)
    assert magnitude.resend_needed(nid) == []
    (node,) = magnitude.view()["nodes"]
    assert node["stale"] is False
    st = magnitude.load()
    st["nodes"][nid]["last_seen"] = time.time() - 3600
    magnitude.save(st)
    assert magnitude.view()["nodes"][0]["stale"] is True
    # never connected, paired 20 min ago: stale too (no endless « waiting for the agent »)
    nid2, _ = magnitude.pair()
    st = magnitude.load()
    st["nodes"][nid2]["paired_at"] = time.time() - 1200
    magnitude.save(st)
    assert {n["id"]: n["stale"] for n in magnitude.view()["nodes"]}[nid2] is True


def test_prometheus_text(reg):
    magnitude, nid = reg
    magnitude.sync(nid, {"profile": _profile(), "metrics": _sample(),
                         "engines": [{"port": 8003, "model": 'gpt"oss', "engine": "vllm",
                                      "healthy": True, "cards": [1]}]}, False)
    txt = magnitude.prometheus()
    assert 'sokkan_magnitude_node_up{node="rog1"} 1' in txt
    assert 'sokkan_magnitude_gpu_memory_used_gib{node="rog1",gpu="2",pci="0000:20:00.0"} 18.7' in txt
    assert "# TYPE sokkan_magnitude_gpu_utilization_percent gauge" in txt
    assert 'model="gptoss"' in txt                 # quotes cannot break the exposition format
    assert "serve_token" not in txt


def test_metrics_endpoint_access(monkeypatch):
    import app
    from starlette.requests import Request

    def req(host, headers=()):
        return Request({"type": "http", "method": "GET", "path": "/metrics", "client": (host, 1),
                        "headers": [(k.encode(), v.encode()) for k, v in headers]})

    monkeypatch.delenv("SOKKAN_METRICS_TOKEN", raising=False)
    assert app._metrics_allowed(req("127.0.0.1")) is True
    assert app._metrics_allowed(req("127.0.0.1", [("x-forwarded-for", "1.2.3.4")])) is False
    assert app._metrics_allowed(req("10.0.0.5")) is False
    monkeypatch.setenv("SOKKAN_METRICS_TOKEN", "s3cret")
    assert app._metrics_allowed(req("127.0.0.1")) is False
    assert app._metrics_allowed(req("10.0.0.5", [("authorization", "Bearer s3cret")])) is True


def test_llama_release_resolution(tmp_path, monkeypatch):
    """Upstream `releases/latest` = a source-only release since Oct. 2026: the newest
    pre-release with our asset is used, and a build already on disk wins (no re-download)."""
    monkeypatch.delenv("MAGNITUDE_LLAMA_TAG", raising=False)
    monkeypatch.setattr(engine, "BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(engine.sys, "platform", "linux")
    asked = []

    def fake_json(url):
        asked.append(url)
        assert "releases/latest" not in url
        return [{"tag_name": "v0.6.0", "assets": [{"name": "source.tar.gz", "browser_download_url": "u0"}]},
                {"tag_name": "b11498", "prerelease": True, "assets": [
                    {"name": "llama-b11498-bin-ubuntu-cuda-12.8-x64.tar.gz", "browser_download_url": "u1"},
                    {"name": "llama-b11498-bin-ubuntu-vulkan-x64.tar.gz", "browser_download_url": "u2"}]}]

    monkeypatch.setattr(engine, "_http_json", fake_json)
    assert engine._resolve_release()["tag_name"] == "b11498"
    got = {}

    def fake_download(url, dest, cb=None):
        got["url"] = url

    def fake_extract(archive, dest):
        (dest / "build" / "bin").mkdir(parents=True)
        (dest / "build" / "bin" / "llama-server").write_text("")

    monkeypatch.setattr(engine, "_download", fake_download)
    monkeypatch.setattr(engine, "_extract", fake_extract)
    bindir = engine.ensure_llama()
    assert got["url"] == "u2" and bindir == tmp_path / "bin" / "b11498" / "build" / "bin"
    # second call: the local build, no network
    asked.clear()
    (tmp_path / "bin" / "b10344").mkdir()
    (tmp_path / "bin" / "b10344" / "llama-server").write_text("")
    assert engine.ensure_llama() == bindir and asked == []     # b11498 > b10344, numerically


def test_runtime_choice_and_docker_command(monkeypatch, tmp_path):
    """Intel + a card allowed + Docker → the SYCL image on that card only; no card
    allowed, or no Docker → the prebuilt (CPU / Vulkan)."""
    for v in ("MAGNITUDE_RUNTIME", "MAGNITUDE_DOCKER_IMAGE", "MAGNITUDE_CTX", "MAGNITUDE_KV",
              "MAGNITUDE_SERVER_ARGS"):
        monkeypatch.delenv(v, raising=False)
    intel = {"gpu": {"vendor": "intel", "run_devices": [2]}}
    monkeypatch.setattr(engine, "docker_ok", lambda: True)
    assert engine.runtime_for(intel) == "docker"
    assert engine.runtime_for({"gpu": {"vendor": "intel", "run_devices": []}}) == "prebuilt"
    assert engine.runtime_for({"gpu": {"vendor": "nvidia", "run_devices": [0]}}) == "prebuilt"
    monkeypatch.setenv("MAGNITUDE_RUNTIME", "prebuilt")
    assert engine.runtime_for(intel) == "prebuilt"
    monkeypatch.delenv("MAGNITUDE_RUNTIME")
    monkeypatch.setattr(engine, "docker_ok", lambda: False)
    assert engine.runtime_for(intel) == "prebuilt"
    cmd = engine.docker_command(tmp_path / "qwen3-4b.gguf", [2])
    j = " ".join(cmd)
    assert "ZE_AFFINITY_MASK=2" in j and "ghcr.io/ggml-org/llama.cpp:server-intel" in j
    assert f"127.0.0.1:{engine.LLAMA_PORT}:8080" in j and f"{tmp_path}:/models:ro" in j
    assert "/models/qwen3-4b.gguf" in j and "--cache-ram" in cmd
    with pytest.raises(RuntimeError):
        engine.docker_command(tmp_path / "x.gguf", [])
