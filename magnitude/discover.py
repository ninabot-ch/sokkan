#!/usr/bin/env python3
"""discover.py — SOKKAN Magnitude: the inference engines ALREADY running on this node.

A GPU machine often serves models that Magnitude did not start (a vLLM container, a
llama.cpp server, ollama). The agent scans local ports for OpenAI-compatible endpoints
and reports them in its sync as `engines`; the cockpit shows them under the node
("engines running") and can put the Magnitude shim in front of one (action `attach`) so
SOKKAN sessions use it, exactly like a model Magnitude launched itself.

Scan: `MAGNITUDE_DISCOVER_PORTS` (default "8000-8010,11434,8790", ranges and lists),
on 127.0.0.1, GET /v1/models (1.5 s timeout). The ports of Magnitude's own llama-server
and shim are skipped. For each model listed:

  engine   vllm (owned_by "vllm") | llama.cpp (/props answers) | ollama (/api/version)
           | openai-compatible (anything else)
  ctx      max_model_len (vLLM), n_ctx (llama.cpp /props), else None
  healthy  GET /health → 200 (ollama: GET / → 200)
  cards    the GPU indices of the container publishing the port (docker inspect:
           ZE_AFFINITY_MASK, ONEAPI_DEVICE_SELECTOR, CUDA_VISIBLE_DEVICES,
           GGML_VK_VISIBLE_DEVICES), or `MAGNITUDE_ENGINE_CARDS` = {"8003": "1"}.
  container the container name when docker knows the port

Stdlib only, never raises: a port that does not answer is simply not an engine.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

DEFAULT_PORTS = "8000-8010,11434,8790"
TIMEOUT_S = 1.5
UA = "sokkan-magnitude/0.1"
_CARD_VARS = ("ZE_AFFINITY_MASK", "ONEAPI_DEVICE_SELECTOR", "CUDA_VISIBLE_DEVICES",
              "GGML_VK_VISIBLE_DEVICES")


def parse_ports(spec: str | None) -> list[int]:
    out: list[int] = []
    for part in (spec if spec is not None else DEFAULT_PORTS).split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            lo, hi = int(a), int(b or a)
        except ValueError:
            continue
        if 0 < lo <= hi < 65536 and hi - lo <= 200:
            out.extend(range(lo, hi + 1))
    return sorted(set(out))


def _get(url: str, timeout: float = TIMEOUT_S) -> tuple[int, object]:
    """(status, parsed JSON or text); (0, None) when nothing answers."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(1_000_000)
            status = r.status
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return 0, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode("utf-8", "replace")


def _model_ids(doc: object) -> list[dict]:
    """Models of a /v1/models answer (OpenAI `data`, or llama.cpp's `models`)."""
    if not isinstance(doc, dict):
        return []
    rows = doc.get("data")
    if isinstance(rows, list) and rows:
        return [{"id": str(m.get("id")), "owned_by": m.get("owned_by"),
                 "max_model_len": m.get("max_model_len")}
                for m in rows if isinstance(m, dict) and m.get("id")]
    rows = doc.get("models")
    if isinstance(rows, list):
        return [{"id": str(m.get("model") or m.get("name")), "owned_by": None, "max_model_len": None}
                for m in rows if isinstance(m, dict) and (m.get("model") or m.get("name"))]
    return []


def _cards_from_env(env: dict) -> list[int] | None:
    for var in _CARD_VARS:
        v = (env.get(var) or "").strip()
        if not v:
            continue
        if var == "ONEAPI_DEVICE_SELECTOR":   # level_zero:0,1  |  level_zero:*
            v = v.split(":", 1)[-1]
        idx = [int(x) for x in re.findall(r"\d+", v.split(";")[0])]
        if idx:
            return sorted(set(idx))
    return None


def docker_ports() -> dict[int, dict]:
    """host port → {container, cards} from `docker ps` + `docker inspect` (best effort)."""
    exe = shutil.which("docker")
    if not exe:
        return {}
    try:
        ps = subprocess.run([exe, "ps", "--format", "{{.Names}}\t{{.Ports}}"],
                            capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[int, dict] = {}
    for line in ps.splitlines():
        name, _, ports = line.partition("\t")
        hp = {int(p) for p in re.findall(r":(\d+)->", ports)}
        if not hp:
            continue
        cards = None
        try:
            env_lines = subprocess.run(
                [exe, "inspect", name, "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
                capture_output=True, text=True, timeout=8).stdout
            env = dict(ln.split("=", 1) for ln in env_lines.splitlines() if "=" in ln)
            cards = _cards_from_env(env)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        for p in hp:
            out[p] = {"container": name, "cards": cards}
    return out


def _cards_override() -> dict[int, list[int]]:
    raw = (os.environ.get("MAGNITUDE_ENGINE_CARDS") or "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except ValueError:
        return {}
    out = {}
    for k, v in (d.items() if isinstance(d, dict) else []):
        idx = [int(x) for x in re.findall(r"\d+", str(v))]
        if str(k).isdigit() and idx:
            out[int(k)] = sorted(set(idx))
    return out


def probe(port: int, host: str = "127.0.0.1") -> list[dict]:
    """Engines served on one port (one entry per model), [] if none."""
    base = f"http://{host}:{port}"
    status, doc = _get(f"{base}/v1/models")
    if status != 200:
        return []
    models = _model_ids(doc)
    if not models:
        return []
    engine, ctx_default = "openai-compatible", None
    if any((m.get("owned_by") or "").lower() == "vllm" for m in models):
        engine = "vllm"
    else:
        st, props = _get(f"{base}/props")
        if st == 200 and isinstance(props, dict) and (
                "default_generation_settings" in props or "n_ctx" in props or "build_info" in props):
            engine = "llama.cpp"
            dgs = props.get("default_generation_settings") or {}
            ctx_default = dgs.get("n_ctx") or props.get("n_ctx")
        else:
            st, ver = _get(f"{base}/api/version")
            if st == 200 and isinstance(ver, dict) and "version" in ver:
                engine = "ollama"
    hs, _ = _get(f"{base}/health") if engine != "ollama" else _get(f"{base}/")
    return [{"port": port, "model": m["id"], "engine": engine,
             "ctx": m.get("max_model_len") or ctx_default,
             "healthy": hs == 200, "base_url": base} for m in models]


def scan(skip: set[int] | None = None, ports: list[int] | None = None) -> list[dict]:
    """Every engine found on the configured ports, cards/container filled when known."""
    ports = parse_ports(os.environ.get("MAGNITUDE_DISCOVER_PORTS")) if ports is None else ports
    skip = skip or set()
    found: list[dict] = []
    for p in ports:
        if p in skip:
            continue
        found.extend(probe(p))
    if not found:
        return []
    dock = docker_ports()
    over = _cards_override()
    for e in found:
        d = dock.get(e["port"]) or {}
        e["container"] = d.get("container")
        e["cards"] = over.get(e["port"]) or d.get("cards")
    found.sort(key=lambda e: (e["port"], e["model"]))
    return found


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2))
