"""3.2.3 — Magnitude on Intel GPUs and on engines it did not start.

hw.py: Intel discrete cards (xpu-smi / clinfo / sycl-ls, simulated outputs) come before
vulkan, several cards = several devices on ONE node, class on the total and per card.
discover.py: OpenAI-compatible endpoints already running (fake vLLM / llama.cpp servers),
engine kind, context, cards from the container's env. Agent: « attach » puts the shim in
front of such an engine and a session request goes through it. Backend: engines stored
from the sync, shown with card names, attach validated against what the node reported."""
import importlib.util
import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# the agent package is loaded under another name: `magnitude` is backend/magnitude.py here
_PKG = Path(__file__).resolve().parent.parent / "magnitude"
_spec = importlib.util.spec_from_file_location("magnitude_agent", _PKG / "__init__.py",
                                               submodule_search_locations=[str(_PKG)])
MA = importlib.util.module_from_spec(_spec)
sys.modules["magnitude_agent"] = MA
_spec.loader.exec_module(MA)
import magnitude_agent.agent as agent_mod  # noqa: E402
import magnitude_agent.discover as discover  # noqa: E402
import magnitude_agent.hw as hw  # noqa: E402

B60 = "Intel(R) Arc(TM) Pro B60 Graphics"


def _clinfo(n=4, igpu=True):
    out = ["Number of platforms                               1",
           "  Platform Name                                   Intel(R) OpenCL Graphics"]
    devs = []
    if igpu:
        devs.append(("Intel(R) UHD Graphics 770", "0000:00:02.0", 26_000_000_000))
    devs += [(B60, f"0000:{0x19 + i * 0x20:02x}:00.0", 24385683456) for i in range(n)]
    for name, pci, mem in devs:
        out += [f"  Device Name                                     {name}",
                "  Driver Version                                  26.27.39122.14",
                f"  Device PCI bus info (KHR)                       PCI-E, {pci}",
                "  Max compute units                               160",
                f"  Global memory size                              {mem} ({mem / 2**30:.2f}GiB)"]
    return "\n".join(out)


NVIDIA_FAIL = ("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. "
               "Make sure that the latest NVIDIA driver is installed and running.")
SYCL_LS = "\n".join(
    [f"[level_zero:gpu][level_zero:{i}] Intel(R) oneAPI Unified Runtime over Level-Zero, {B60} 20.1.0 [1.6.33578+11]"
     for i in range(4)] + ["[opencl:cpu][opencl:0] Intel(R) OpenCL, Intel(R) Core(TM) i9 OpenCL 3.0"])


def _tools(monkeypatch, outputs: dict[str, str], vulkan_icd=True):
    """Simulated host: `outputs` = command name → stdout (absent = not installed)."""
    monkeypatch.setattr(hw.shutil, "which", lambda exe: f"/usr/bin/{exe}" if exe in outputs else None)

    def run(cmd):
        key = cmd[0] if cmd[0] != "xpu-smi" else " ".join(cmd)
        return outputs.get(key, outputs.get(cmd[0], ""))
    monkeypatch.setattr(hw, "_run", run)
    monkeypatch.setattr(hw, "_vulkan_icd", lambda vendor: vulkan_icd)
    monkeypatch.setattr(hw, "_intel_sysfs", lambda root="": [])


def test_intel_cards_from_clinfo_before_vulkan(monkeypatch):
    _tools(monkeypatch, {"nvidia-smi": NVIDIA_FAIL, "clinfo": _clinfo(),
                         "vulkaninfo": "deviceName = Intel(R) Graphics (BMG G21)"})
    g = hw.detect_gpu("linux", "x86_64", 62.5)
    assert g["vendor"] == "intel" and g["count"] == 4             # the UHD iGPU is not a card
    assert g["name"] == f"4× {B60}"
    assert [d["vram_gb"] for d in g["devices"]] == [22.7] * 4
    assert g["devices"][0]["pci"] == "0000:19:00.0"
    assert g["vram_total_gb"] == 90.8 and g["vram_per_card_gb"] == 22.7
    assert g["driver"] == "26.27.39122.14" and g["offload"] == "vulkan"
    assert hw._machine_class(g, 62.5) == "XL"
    assert hw._machine_class(g, 62.5, per_card=True) == "L"


def test_intel_from_xpu_smi_and_sycl_ls(monkeypatch):
    disc = {"device_list": [{"device_id": i, "device_name": B60, "pci_bdf_address": f"0000:{i}:00.0"}
                            for i in range(2)]}
    detail = {"memory_physical_size_byte": str(24 * 2**30)}
    _tools(monkeypatch, {"xpu-smi discovery -j": json.dumps(disc),
                         "xpu-smi discovery -d 0 -j": json.dumps(detail),
                         "xpu-smi discovery -d 1 -j": json.dumps(detail),
                         "xpu-smi": ""})
    g = hw._detect_intel()
    assert g["count"] == 2 and g["vram_total_gb"] == 48.0 and g["backend"] == "level_zero"
    # sycl-ls lists the cards without memory: clinfo fills it
    _tools(monkeypatch, {"sycl-ls": SYCL_LS, "clinfo": _clinfo(igpu=False)}, vulkan_icd=False)
    monkeypatch.setattr(hw, "_intel_xpu_smi", lambda: [])
    monkeypatch.setattr(hw, "_intel_clinfo", lambda: [])          # first pass: sycl-ls wins
    g = hw._detect_intel()
    assert g["count"] == 4 and g["devices"][0]["name"] == B60 and g["vram_total_gb"] is None
    assert g["offload"] is None


def test_no_intel_card_falls_through(monkeypatch):
    _tools(monkeypatch, {"clinfo": _clinfo(n=0, igpu=True),
                         "vulkaninfo": "deviceName = Intel(R) UHD Graphics 770"})
    g = hw.detect_gpu("linux", "x86_64", 32)
    assert g["backend"] == "vulkan"                               # iGPU only → old path


def test_nvidia_several_cards(monkeypatch):
    _tools(monkeypatch, {"nvidia-smi": "NVIDIA RTX 3090, 24576, 20000, 570.1, 350\n"
                                       "NVIDIA RTX 3090, 24576, 24000, 570.1, 350"})
    g = hw.detect_gpu("linux", "x86_64", 64)
    assert g["count"] == 2 and g["vram_total_gb"] == 48.0 and g["vram_free_gb"] == 42.9
    assert g["name"] == "2× NVIDIA RTX 3090"


# ---- engines already running ----------------------------------------------------------

class _Engine(BaseHTTPRequestHandler):
    kind = "vllm"
    model = "gpt-oss-20b"
    calls: list = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            if self.kind == "vllm":
                return self._json(200, {"object": "list", "data": [
                    {"id": self.model, "object": "model", "owned_by": "vllm", "max_model_len": 65536}]})
            if self.kind == "llama.cpp":
                return self._json(200, {"models": [{"name": self.model, "model": self.model}],
                                        "object": "list", "data": [{"id": self.model, "owned_by": "llamacpp"}]})
            return self._json(404, {"detail": "Not Found"})
        if self.path == "/props" and self.kind == "llama.cpp":
            return self._json(200, {"default_generation_settings": {"n_ctx": 32768}})
        if self.path == "/health":
            return self._json(200, {"status": "ok"})
        return self._json(404, {"detail": "Not Found"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        type(self).calls.append(req)
        self._json(200, {"id": "c1", "object": "chat.completion", "model": req.get("model"),
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": "Paris"}}],
                         "usage": {"prompt_tokens": 12, "completion_tokens": 1}})


def _serve(kind, model):
    h = type(f"H_{kind}", (_Engine,), {"kind": kind, "model": model, "calls": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, h


@pytest.fixture()
def engines():
    servers = [_serve("vllm", "gpt-oss-20b"), _serve("llama.cpp", "qwen3-coder-30b"), _serve("none", "-")]
    yield servers
    for s, _ in servers:
        s.shutdown()


def test_parse_ports_and_cards_from_env():
    assert discover.parse_ports("8000-8002, 11434,x,9-1") == [8000, 8001, 8002, 11434]
    assert discover.parse_ports(None)[:3] == [8000, 8001, 8002] and 8790 in discover.parse_ports(None)
    assert discover._cards_from_env({"ZE_AFFINITY_MASK": "2,3"}) == [2, 3]
    assert discover._cards_from_env({"ONEAPI_DEVICE_SELECTOR": "level_zero:1"}) == [1]
    assert discover._cards_from_env({"CUDA_VISIBLE_DEVICES": "0"}) == [0]
    assert discover._cards_from_env({"PATH": "/bin"}) is None


def test_scan_finds_vllm_and_llamacpp(engines, monkeypatch):
    (v, _), (lc, _), (none, _) = engines
    pv, pl, pn = v.server_address[1], lc.server_address[1], none.server_address[1]
    monkeypatch.setattr(discover, "docker_ports", lambda: {pv: {"container": "vllm-xpu", "cards": [1]},
                                                          pl: {"container": "llamacpp-coder", "cards": None}})
    monkeypatch.setenv("MAGNITUDE_ENGINE_CARDS", json.dumps({str(pl): "0"}))
    found = discover.scan(ports=[pv, pl, pn, 1])
    assert [(e["model"], e["engine"]) for e in found] == sorted(
        [("gpt-oss-20b", "vllm"), ("qwen3-coder-30b", "llama.cpp")], key=lambda x: {"gpt-oss-20b": pv, "qwen3-coder-30b": pl}[x[0]])
    by = {e["model"]: e for e in found}
    assert by["gpt-oss-20b"]["ctx"] == 65536 and by["gpt-oss-20b"]["cards"] == [1]
    assert by["gpt-oss-20b"]["container"] == "vllm-xpu" and by["gpt-oss-20b"]["healthy"] is True
    assert by["qwen3-coder-30b"]["ctx"] == 32768 and by["qwen3-coder-30b"]["cards"] == [0]   # override
    assert discover.scan(skip={pv, pl}, ports=[pv, pl]) == []


def test_attach_bridges_a_running_engine(engines, monkeypatch):
    import socket

    (v, handler), _, _ = engines
    port = v.server_address[1]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        shim_port = s.getsockname()[1]
    monkeypatch.setattr(agent_mod, "SHIM_PORT", shim_port)
    a = agent_mod.Agent("http://127.0.0.1:1", "tok")
    try:
        a._do_attach("gpt-oss-20b", port)
        assert a._serving["external"] is True and a._serving["engine"] == "vllm"
        assert a._serving["port"] == port and a._outbox["serving"]["model"] == "gpt-oss-20b"
        assert a._status == {"phase": "serving", "model": "gpt-oss-20b"}
        req = urllib.request.Request(
            f"http://127.0.0.1:{shim_port}/v1/messages",
            data=json.dumps({"model": "claude-x", "max_tokens": 20,
                             "messages": [{"role": "user", "content": "capital of France?"}]}).encode(),
            headers={"content-type": "application/json",
                     "x-api-key": a._serving["serve_token"]})
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        assert resp["content"][-1]["text"] == "Paris"
        assert handler.calls[-1]["model"] == "gpt-oss-20b"         # the engine's own model id
        with pytest.raises(RuntimeError):
            a._do_attach("not-served", port)
    finally:
        a._teardown_serving()
    assert a._serving is None and a._outbox["serving"] is None


# ---- cockpit side ------------------------------------------------------------------------

@pytest.fixture()
def reg(tmp_path, monkeypatch):
    import magnitude

    monkeypatch.setattr(magnitude, "STATE", tmp_path / "magnitude.json")
    nid, token = magnitude.pair()
    profile = {"schema": "sokkan-magnitude/profile/v1", "hostname": "rog1", "class": "XL",
               "class_per_card": "L", "ram_gb": 62.5, "cores": 36, "cpu": "i9",
               "gpu": {"vendor": "intel", "name": f"4× {B60}", "backend": "level_zero",
                       "vram_total_gb": 90.8, "vram_per_card_gb": 22.7, "offload": None,
                       "devices": [{"index": i, "name": B60, "vram_gb": 22.7} for i in range(4)]}}
    eng = [{"port": 8003, "model": "gpt-oss-20b", "engine": "vllm", "ctx": 65536, "healthy": True,
            "container": "vllm-xpu", "cards": [1]},
           {"port": 8007, "model": "qwen3-next-80b", "engine": "llama.cpp", "ctx": 32768,
            "healthy": True, "container": "llamacpp-next80b", "cards": [2, 3], "evil": "x" * 9}]
    magnitude.sync(nid, {"profile": profile, "status": {"phase": "idle"}, "engines": eng}, False)
    return magnitude, nid


def test_engines_stored_and_viewed(reg):
    magnitude, nid = reg
    (node,) = magnitude.view()["nodes"]
    assert node["name"] == "rog1" and node["profile"]["gpu"]["devices"][3]["vram_gb"] == 22.7
    e = {x["model"]: x for x in node["engines"]}
    assert set(e) == {"gpt-oss-20b", "qwen3-next-80b"} and "evil" not in e["qwen3-next-80b"]
    assert e["qwen3-next-80b"]["card_names"] == [f"#2 {B60}", f"#3 {B60}"]
    assert e["gpt-oss-20b"]["ctx_ok"] is True and e["qwen3-next-80b"]["ctx_ok"] is False
    # Intel cards without a Vulkan driver: Magnitude's own engine runs on CPU → fit by RAM
    fits = {m["id"]: m["fit"] for m in node["catalog"]}
    assert fits["llama-3.3-70b"] == "no" and fits["qwen3-8b"] != "no"
    # an attached engine is reported as serving and marked in the list
    magnitude.sync(nid, {"serving": {"model": "gpt-oss-20b", "shim_port": 8790, "serve_token": "s",
                                     "since": 1.0, "engine": "vllm", "port": 8003, "external": True}}, True)
    (node,) = magnitude.view()["nodes"]
    assert node["serving"]["external"] is True and node["serving"]["port"] == 8003
    assert "serve_token" not in json.dumps(node)
    assert {x["model"]: x["serving"] for x in node["engines"]} == {"gpt-oss-20b": True, "qwen3-next-80b": False}


def test_attach_command_is_validated(reg, monkeypatch):
    import app
    from fastapi import HTTPException

    magnitude, nid = reg
    monkeypatch.setattr(app.audit, "log", lambda *a, **k: None)
    admin = {"email": "root@x", "role": "admin"}
    assert magnitude.find_engine(magnitude.get_node(nid), "gpt-oss-20b", "8003")["cards"] == [1]
    for body in ({"model": "gpt-oss-20b", "port": 9999}, {"model": "qwen3-coder-30b", "port": 8003},
                 {"model": "gpt-oss-20b"}):
        with pytest.raises(HTTPException) as e:
            app.magnitude_cmd(app.MagnitudeCmdBody(node=nid, action="attach", **body), u=admin, _f=None)
        assert e.value.status_code == 400
    app.magnitude_cmd(app.MagnitudeCmdBody(node=nid, action="attach", model="gpt-oss-20b", port=8003),
                      u=admin, _f=None)
    assert magnitude.sync(nid, {}, False) == {"action": "attach", "model": "gpt-oss-20b", "port": 8003}
    # a catalogue command carries no port
    app.magnitude_cmd(app.MagnitudeCmdBody(node=nid, action="run", model="qwen3-8b", port=8003),
                      u=admin, _f=None)
    assert magnitude.sync(nid, {}, False) == {"action": "run", "model": "qwen3-8b"}
