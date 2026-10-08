#!/usr/bin/env python3
"""magnitude.py — SOKKAN : état cockpit de la feature Magnitude (LLM local).

Magnitude profile le hardware des machines du client, benche les modèles locaux
servables et en fait tourner un (llama.cpp) branché au router LLM en un clic.
Chaque machine fait tourner l'agent host (`python3 -m magnitude` — package
séparé, JAMAIS importé ici : le container n'y a pas accès) qui poll le backend
en HTTP sortant. Ce module tient le REGISTRY multi-nodes dans
`$SOKKAN_DATA_DIR/magnitude.json` (chmod 600 — il contient les serve_tokens) :

  {"schema": 2,
   "nodes": {"<id>": {                # id = sha256(token)[:12]
      "token_sha256": "…",            # sha256 du pairing token (jamais le clair)
      "name": "",                     # nom explicite (sinon dérivé du profil)
      "shim_url": null,               # URL du shim vue des sessions (sinon défaut)
      "profile": {…},                 # profil hardware remonté par l'agent
      "last_seen": 1754800000.0,      # dernier sync (online = < 15 s)
      "status": {"phase": "…", …},    # idle|benching|downloading|starting|serving|error
      "bench": {"<model>": {…}},      # résultats de bench par modèle
      "serving": {"model", "shim_port", "serve_token", "since"},
      "pending": {"action", "model"}}}}  # commande UI, livrée au prochain sync

Un pairing token par node, généré ici (secrets.token_urlsafe) et montré UNE
fois à l'UI ; l'agent le renvoie en header `x-magnitude-token`, résolu vers son
node en comparant les sha256 en constant-time. Les serve_tokens (générés par
les agents) ne sortent JAMAIS vers l'UI — ils ne partent que dans llm.json au
« Connect ». L'ancien schéma mono-agent (pré-multi) est migré à la lecture.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

import llm

STATE = Path(os.path.join(
    os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
    "magnitude.json"))

ONLINE_WINDOW_S = 15.0  # agent « online » = dernier sync il y a moins de 15 s
# URL PAR DÉFAUT du shim telle que VUE PAR LES SESSIONS (node sans `shim_url`
# explicite) : cockpit docker + agent sur le même host. Backend natif ou node
# distant → SOKKAN_MAGNITUDE_SHIM_URL global, ou `shim_url` par node via l'UI.
SHIM_URL = os.environ.get("SOKKAN_MAGNITUDE_SHIM_URL",
                          "http://host.docker.internal:8790")
FIT_MARGIN_GB = 1.2  # marge KV cache / compute buffers au-dessus des poids

_LOCK = threading.Lock()  # sérialise les read-modify-write de l'état

# 3.2.3 — live load of each node (agent ≥ 0.3), kept in memory only: a sample every few
# seconds has no business in magnitude.json. {node_id: {"last": snap, "hist": [...]}}
_METRICS: dict[str, dict] = {}
METRICS_FRESH_S = 20.0      # older than that = not shown (agent stopped sampling)
METRICS_HISTORY = 40        # ~2 min of samples at 3 s, for the sparklines
STALE_AFTER_S = 600.0       # a node silent for 10 min is « offline since … », with Unpair

# Catalogue des modèles servables — DUPLIQUÉ de magnitude/catalog.py (le
# container ne voit pas le package host) : ids et poids strictement identiques.
CATALOG: list[dict] = [
    {"id": "qwen3-4b", "label": "Qwen3 4B", "params": "4B", "moe": False,
     "weights_gb": 2.5, "note": "light & quick",
     "url": "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf"},
    {"id": "qwen3-8b", "label": "Qwen3 8B", "params": "8B", "moe": False,
     "weights_gb": 5.0, "note": "balanced daily driver",
     "url": "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf"},
    {"id": "llama-3.1-8b", "label": "Llama 3.1 8B", "params": "8B", "moe": False,
     "weights_gb": 4.9, "note": "classic all-rounder",
     "url": "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"},
    {"id": "qwen3-14b", "label": "Qwen3 14B", "params": "14B", "moe": False,
     "weights_gb": 9.0, "note": "strong reasoning",
     "url": "https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf"},
    {"id": "phi-4-14b", "label": "Phi-4 14B", "params": "14B", "moe": False,
     "weights_gb": 9.1, "note": "compact quality",
     "url": "https://huggingface.co/bartowski/phi-4-GGUF/resolve/main/phi-4-Q4_K_M.gguf"},
    {"id": "gpt-oss-20b", "label": "GPT-OSS 20B", "params": "20B", "moe": True,
     "weights_gb": 12.2, "note": "OpenAI open-weight, fast MoE",
     "url": "https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf"},
    {"id": "qwen3-30b-a3b", "label": "Qwen3 30B-A3B", "params": "30B", "moe": True,
     "weights_gb": 18.6, "note": "MoE — big brain, quick tokens",
     "url": "https://huggingface.co/Qwen/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf"},
    {"id": "qwen3-coder-30b", "label": "Qwen3 Coder 30B-A3B", "params": "30B", "moe": True,
     "weights_gb": 18.6, "note": "the local coding reference",
     "url": "https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"},
    {"id": "qwen3-32b", "label": "Qwen3 32B", "params": "32B", "moe": False,
     "weights_gb": 19.8, "note": "dense heavyweight",
     "url": "https://huggingface.co/Qwen/Qwen3-32B-GGUF/resolve/main/Qwen3-32B-Q4_K_M.gguf"},
    {"id": "llama-3.3-70b", "label": "Llama 3.3 70B", "params": "70B", "moe": False,
     "weights_gb": 42.5, "note": "near-frontier open weights",
     "url": "https://huggingface.co/unsloth/Llama-3.3-70B-Instruct-GGUF/resolve/main/Llama-3.3-70B-Instruct-Q4_K_M.gguf"},
]

_NODE_KEYS = ("token_sha256", "name", "shim_url", "profile", "last_seen",
              "status", "bench", "serving", "pending", "engines")
# a Claude Code session opens at ~40 500 tokens of prompt (dogfood 2026-08-10): an
# engine whose context is below this cannot carry one
SESSION_MIN_CTX = 40_960


def _migrate(raw: dict) -> dict:
    """Schéma v1 mono-agent → v2 registry (idempotent, en mémoire — persisté à
    la prochaine mutation)."""
    if "nodes" in raw:
        return raw
    nodes = {}
    if raw.get("token_sha256"):
        nid = raw["token_sha256"][:12]
        nodes[nid] = {k: raw.get(k) for k in _NODE_KEYS if raw.get(k) is not None}
    return {"schema": 2, "nodes": nodes}


def load() -> dict:
    try:
        return _migrate(json.loads(STATE.read_text(encoding="utf-8")) or {})
    except (FileNotFoundError, ValueError):
        return {"schema": 2, "nodes": {}}


def save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        STATE.chmod(0o600)  # contient les serve_tokens
    except OSError:
        pass


# --- pairing -----------------------------------------------------------------
def pair() -> tuple[str, str]:
    """Crée un NOUVEAU node (les autres ne bougent pas) et retourne
    (node_id, token). Le token en clair est montré une seule fois — seul son
    sha256 est persisté."""
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode()).hexdigest()
    nid = digest[:12]
    with _LOCK:
        st = load()
        st["nodes"][nid] = {"token_sha256": digest, "paired_at": time.time()}
        save(st)
    return nid, token


def unpair(node_id: str) -> bool:
    """Retire UN node du registry (token révoqué, bench/serving oubliés)."""
    with _LOCK:
        st = load()
        if node_id not in st["nodes"]:
            return False
        del st["nodes"][node_id]
        save(st)
    return True


def resolve_token(token: str) -> str | None:
    """Header agent `x-magnitude-token` → node_id (constant-time sur les
    sha256 ; on parcourt tous les nodes sans early-exit sur mismatch)."""
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    found = None
    for nid, node in load()["nodes"].items():
        if secrets.compare_digest(digest, node.get("token_sha256") or ""):
            found = nid
    return found


def get_node(node_id: str) -> dict | None:
    return load()["nodes"].get(node_id)


def node_config(node_id: str, name: str | None = None,
                shim_url: str | None = None) -> bool:
    """Config explicite d'un node ('' = revenir au défaut/auto)."""
    with _LOCK:
        st = load()
        node = st["nodes"].get(node_id)
        if node is None:
            return False
        if name is not None:
            node["name"] = name.strip()
        if shim_url is not None:
            node["shim_url"] = shim_url.strip().rstrip("/") or None
        save(st)
    return True


def online(node: dict) -> bool:
    last = node.get("last_seen") or 0
    return bool(last) and (time.time() - last) < ONLINE_WINDOW_S


def shim_url_of(node: dict) -> str:
    """URL du shim de CE node telle que vue par les sessions."""
    return (node.get("shim_url") or SHIM_URL).rstrip("/")


def node_name(node_id: str, node: dict) -> str:
    """Nom affiché : explicite > hostname du profil > GPU > CPU > id."""
    if node.get("name"):
        return node["name"]
    p = node.get("profile") or {}
    return (p.get("hostname") or (p.get("gpu") or {}).get("name")
            or p.get("cpu") or node_id)


# --- catalogue & fit ---------------------------------------------------------
def catalog_get(model_id: str) -> dict | None:
    return next((m for m in CATALOG if m["id"] == model_id), None)


def _run_target(profile: dict, metrics: dict | None = None) -> dict:
    """Where Magnitude's own Run/Benchmark would execute, and how much memory it has.

    {"where": "gpu"|"cpu", "cards": [indexes] | None, "usable_gb": float | None,
     "basis": "free"|"total"|"ram"}. With live metrics (agent ≥ 0.3) the fit uses the
    FREE memory of the allowed cards — a card serving another engine is not « 24 GB »."""
    gpu = profile.get("gpu") or {}
    allowed = gpu.get("run_devices")
    devices = gpu.get("devices") or []
    gpu_usable = gpu.get("backend") in ("cuda", "vulkan", "metal") or gpu.get("offload") == "vulkan"
    if gpu.get("vendor") not in (None, "", "none") and gpu_usable and allowed != []:
        cards = allowed if allowed is not None else [d.get("index") for d in devices] or None
        live = {g.get("index"): g for g in (metrics or {}).get("gpus") or []}
        if cards and live and all(c in live and live[c].get("vram_used_gb") is not None
                                  and live[c].get("vram_total_gb") for c in cards):
            free = sum(max(0.0, live[c]["vram_total_gb"] - live[c]["vram_used_gb"]) for c in cards)
            return {"where": "gpu", "cards": cards, "usable_gb": round(free, 1), "basis": "free"}
        if cards and devices and allowed is not None:
            per = {d.get("index"): d.get("vram_gb") for d in devices}
            tot = sum(per.get(c) or 0 for c in cards)
            return {"where": "gpu", "cards": cards, "usable_gb": round(tot, 1) or None,
                    "basis": "total"}
        u = _usable_gb(profile)
        if u is not None and (gpu.get("vendor") != "intel" or gpu_usable):
            return {"where": "gpu", "cards": cards, "usable_gb": u, "basis": "total"}
    # CPU: half of the RAM — or of the RAM still available when we can see it
    ram = profile.get("ram_gb")
    m = metrics or {}
    if m.get("ram_total_gb") and m.get("ram_used_gb") is not None:
        avail = m["ram_total_gb"] - m["ram_used_gb"]
        return {"where": "cpu", "cards": [] if allowed == [] else None,
                "usable_gb": round(min(avail * 0.8, (ram or avail) * 0.5), 1), "basis": "ram"}
    return {"where": "cpu", "cards": [] if allowed == [] else None,
            "usable_gb": float(ram) * 0.5 if ram else None, "basis": "ram"}


def run_target_of(node_id: str) -> dict | None:
    node = load()["nodes"].get(node_id) or {}
    return _run_target(node["profile"], metrics_of(node_id)) if node.get("profile") else None


def _usable_gb(profile: dict) -> float | None:
    """Mémoire utilisable (Go) pour les poids : VRAM GPU telle que remontée
    (hw.py donne déjà 75 % de la RAM unifiée sur Apple Silicon), sinon 50 % de
    la RAM en classe CPU. None = machine non servable (class unsupported)."""
    gpu = profile.get("gpu") or {}
    # 3.2.3: Intel cards seen through Level Zero / OpenCL are usable by Magnitude's own
    # engine (llama.cpp Vulkan build) only when the node has a Vulkan driver for them
    gpu_usable = gpu.get("backend") in ("cuda", "vulkan", "metal") or gpu.get("offload") == "vulkan"
    if gpu.get("vendor") == "intel" and not gpu_usable:
        return float(profile["ram_gb"]) * 0.5 if profile.get("ram_gb") else None
    if gpu.get("vendor") not in (None, "", "none") and gpu.get("vram_total_gb"):
        return float(gpu["vram_total_gb"])
    if profile.get("class") == "CPU" and profile.get("ram_gb"):
        return float(profile["ram_gb"]) * 0.5
    return None


def _fit(weights_gb: float, usable: float | None) -> str:
    if usable is None:
        return "no"
    need = weights_gb + FIT_MARGIN_GB
    if need <= usable * 0.85:
        return "comfortable"
    if need <= usable:
        return "tight"
    return "no"


def catalog_view(profile: dict | None, metrics: dict | None = None) -> list[dict]:
    """Catalogue pour l'UI, annoté du fit vs profil (sans les URLs de poids) : sur les
    cartes autorisées pour Run (mémoire libre si l'agent la remonte), sinon le CPU."""
    usable = _run_target(profile, metrics)["usable_gb"] if profile else None
    out = []
    for m in CATALOG:
        e = {k: m[k] for k in ("id", "label", "params", "moe", "weights_gb", "note")}
        if profile is None:
            e["fits"], e["fit"] = False, "unknown"
        else:
            f = _fit(m["weights_gb"], usable)
            e["fits"], e["fit"] = f != "no", f
        out.append(e)
    return out


# --- commandes UI → agent ----------------------------------------------------
def set_pending(node_id: str, action: str, model: str = "", port: int | None = None) -> bool:
    """Pose la commande à livrer au prochain sync du node (une seule en vol)."""
    with _LOCK:
        st = load()
        node = st["nodes"].get(node_id)
        if node is None:
            return False
        node["pending"] = {"action": action, "model": model,
                           **({"port": int(port)} if port is not None else {})}
        save(st)
    return True


# --- engines already running on a node (agent ≥ 0.2, magnitude/discover.py) --
def _clean_engine(e: dict) -> dict:
    """Keep the known fields of an engine reported by an agent (untrusted input)."""
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    cards = e.get("cards")
    return {"port": _int(e.get("port")), "model": str(e.get("model") or "")[:200],
            "engine": str(e.get("engine") or "openai-compatible")[:40],
            "ctx": _int(e.get("ctx")), "healthy": bool(e.get("healthy")),
            "container": (str(e["container"])[:120] if e.get("container") else None),
            "cards": ([c for c in (_int(x) for x in cards) if c is not None]
                      if isinstance(cards, list) else None)}


def find_engine(node: dict, model: str, port) -> dict | None:
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return next((e for e in node.get("engines") or []
                 if e.get("port") == port and e.get("model") == model), None)


def engines_view(node: dict) -> list[dict]:
    """Engines of a node for the UI: card names, serving marker, context check."""
    serving = node.get("serving") or {}
    devices = {d.get("index"): d for d in ((node.get("profile") or {}).get("gpu") or {}).get("devices") or []}
    out = []
    for e in node.get("engines") or []:
        cards = e.get("cards")
        out.append({**e,
                    "card_names": [f"#{c} {devices[c]['name']}" if c in devices else f"#{c}"
                                   for c in cards] if cards else None,
                    "serving": bool(serving.get("external")) and serving.get("port") == e.get("port")
                    and serving.get("model") == e.get("model"),
                    "ctx_ok": None if not e.get("ctx") else e["ctx"] >= SESSION_MIN_CTX})
    return out


# --- sync agent --------------------------------------------------------------
def sync(node_id: str, payload: dict, serving_set: bool) -> dict | None:
    """Applique un sync agent (profil/status/bench/serving/erreur) + last_seen,
    et livre la commande pending du node s'il y en a une (effacée à la
    livraison). `serving_set` distingue `serving: null` (stop effectif) du
    champ absent."""
    with _LOCK:
        st = load()
        node = st["nodes"].get(node_id)
        if node is None:
            return None
        if payload.get("profile") is not None:
            node["profile"] = payload["profile"]
        if payload.get("status") is not None:
            node["status"] = payload["status"]
        elif payload.get("error"):
            node["status"] = {"phase": "error", "detail": str(payload["error"])[:300]}
        br = payload.get("bench_result")
        if br and br.get("model"):
            entry = {k: v for k, v in br.items() if k != "model"}
            entry.setdefault("at", time.time())
            node.setdefault("bench", {})[br["model"]] = entry
        if serving_set:
            node["serving"] = payload.get("serving")
        if isinstance(payload.get("engines"), list):   # agent ≥ 0.2: engines running
            node["engines"] = [_clean_engine(e) for e in payload["engines"] if isinstance(e, dict)][:50]
            node["engines_at"] = time.time()
        if isinstance(payload.get("metrics"), dict):   # agent ≥ 0.3: live load
            record_metrics(node_id, payload["metrics"])
        node["last_seen"] = time.time()
        cmd = node.pop("pending", None)
        save(st)
    return cmd or None


def resend_needed(node_id: str) -> list[str]:
    """What the cockpit does not hold for this node and asks its agent again (a cockpit
    upgraded after its agent, a registry restored from a backup) — agents ≥ 0.3 honour it."""
    node = load()["nodes"].get(node_id) or {}
    return [k for k, missing in (("profile", not node.get("profile")),
                                 ("engines", node.get("engines_at") is None)) if missing]


def _clean_metrics(m: dict) -> dict:
    """Known numeric fields of a load sample (untrusted input)."""
    def f(v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return x if x == x and abs(x) < 1e7 else None   # NaN / absurd → None
    gpus = []
    for g in (m.get("gpus") or [])[:16]:
        if isinstance(g, dict):
            idx = f(g.get("index"))
            gpus.append({"index": int(idx) if idx is not None else len(gpus),
                         "pci": str(g.get("pci") or "")[:16] or None,
                         **{k: f(g.get(k)) for k in ("util_pct", "vram_used_gb", "vram_total_gb",
                                                     "temp_c", "power_w")}})
    return {"at": time.time(), **{k: f(m.get(k)) for k in ("cpu_pct", "load1", "ram_used_gb",
                                                           "ram_total_gb")}, "gpus": gpus}


def record_metrics(node_id: str, m: dict) -> None:
    snap = _clean_metrics(m)
    slot = _METRICS.setdefault(node_id, {"last": None, "hist": []})
    slot["last"] = snap
    slot["hist"] = (slot["hist"] + [{"at": snap["at"], "cpu": snap["cpu_pct"],
                                     "gpu": [g["util_pct"] for g in snap["gpus"]]}])[-METRICS_HISTORY:]


def metrics_of(node_id: str) -> dict | None:
    slot = _METRICS.get(node_id)
    if not slot or not slot["last"] or time.time() - slot["last"]["at"] > METRICS_FRESH_S:
        return None
    return {**slot["last"], "history": slot["hist"]}


def prometheus() -> str:
    """`sokkan_magnitude_*` gauges of every node, Prometheus text format 0.0.4."""
    st = load()
    lines = []

    def g(name, help_, rows):
        lines.append(f"# HELP sokkan_magnitude_{name} {help_}")
        lines.append(f"# TYPE sokkan_magnitude_{name} gauge")
        for labels, v in rows:
            if v is None:
                continue
            lab = ",".join(f'{k}="{str(val).replace(chr(92), "").replace(chr(34), "")}"'
                           for k, val in labels.items())
            lines.append(f"sokkan_magnitude_{name}{{{lab}}} {v}")

    nodes, fresh = [], {}
    for nid, node in st["nodes"].items():
        name = node_name(nid, node)
        nodes.append((nid, name, node))
        fresh[nid] = metrics_of(nid)
    g("node_up", "1 when the node's agent synced in the last 15 s",
      [({"node": n}, 1 if online(node) else 0) for _, n, node in nodes])
    g("node_last_seen_seconds", "Unix time of the node's last sync",
      [({"node": n}, round(node.get("last_seen") or 0, 1)) for _, n, node in nodes])
    for key, name, help_ in (("cpu_pct", "cpu_utilization_percent", "CPU busy %, whole machine"),
                             ("load1", "load1", "1-minute load average"),
                             ("ram_used_gb", "ram_used_gib", "RAM in use (GiB)"),
                             ("ram_total_gb", "ram_total_gib", "RAM installed (GiB)")):
        g(name, help_, [({"node": n}, (fresh[i] or {}).get(key)) for i, n, _ in nodes])
    for key, name, help_ in (("util_pct", "gpu_utilization_percent", "GPU busy % (compute engine)"),
                             ("vram_used_gb", "gpu_memory_used_gib", "GPU memory in use (GiB)"),
                             ("vram_total_gb", "gpu_memory_total_gib", "GPU memory size (GiB)"),
                             ("temp_c", "gpu_temperature_celsius", "GPU package temperature"),
                             ("power_w", "gpu_power_watts", "GPU board power (W)")):
        g(name, help_, [({"node": n, "gpu": gg["index"], "pci": gg.get("pci") or ""}, gg.get(key))
                        for i, n, _ in nodes for gg in (fresh[i] or {}).get("gpus") or []])
    g("engine_up", "1 per engine found running on the node (healthy) / 0 (not answering)",
      [({"node": n, "model": e.get("model"), "engine": e.get("engine"), "port": e.get("port")},
        1 if e.get("healthy") else 0) for _, n, node in nodes for e in node.get("engines") or []])
    g("serving", "1 when Magnitude serves (or bridges) a model on the node",
      [({"node": n, "model": (node.get("serving") or {}).get("model") or ""},
        1 if node.get("serving") else 0) for _, n, node in nodes])
    return "\n".join(lines) + "\n"


# --- vue UI ------------------------------------------------------------------
def _stale(node: dict) -> bool:
    """Never synced since pairing 10+ min ago, or silent for 10+ min: the UI says
    « offline since … » / « never connected » with Unpair, never an endless spinner."""
    ref = node.get("last_seen") or node.get("paired_at") or 0
    return bool(ref) and time.time() - ref > STALE_AFTER_S
def connected_node(state: dict | None = None) -> str | None:
    """node_id dont le shim est branché au router LLM de l'instance, sinon None."""
    c = llm.load()
    if c.get("mode") != "custom":
        return None
    base = (c.get("base_url") or "").rstrip("/")
    st = load() if state is None else state
    for nid, node in st["nodes"].items():
        if base and base == shim_url_of(node):
            return nid
    return None


def view() -> dict:
    """État complet pour GET /api/magnitude — les serve_tokens n'en sortent
    jamais. Nodes triés par date de pairing."""
    st = load()
    conn = connected_node(st)
    nodes = []
    for nid, node in sorted(st["nodes"].items(),
                            key=lambda kv: kv[1].get("paired_at") or 0):
        serving = node.get("serving") or None
        shim = shim_url_of(node)
        try:
            import hostnames
            host = urllib.parse.urlsplit(shim).hostname or ""
            shim_host = hostnames.label(host) if host else ""
        except Exception:  # noqa: BLE001 — a label is a nicety
            shim_host = ""
        nodes.append({
            "id": nid,
            "name": node_name(nid, node),
            "online": online(node),
            "last_seen": node.get("last_seen"),
            "shim_url": shim_url_of(node),
            "profile": node.get("profile"),
            "status": node.get("status") or {"phase": "idle"},
            "bench": node.get("bench") or {},
            "shim_host": shim_host,
            "serving": ({"model": serving.get("model"),
                         "shim_url": shim,
                         "since": serving.get("since"),
                         "engine": serving.get("engine"),
                         "port": serving.get("port"),
                         "external": bool(serving.get("external"))} if serving else None),
            "connected": nid == conn,
            "catalog": catalog_view(node.get("profile"), metrics_of(nid)),
            "run_target": _run_target(node["profile"], metrics_of(nid)) if node.get("profile") else None,
            "metrics": metrics_of(nid),
            "paired_at": node.get("paired_at"),
            "stale": _stale(node),
            "engines": engines_view(node),
            "engines_at": node.get("engines_at"),
            "agent_version": (node.get("profile") or {}).get("agent_version"),
        })
    return {"paired": bool(nodes), "shim_default": SHIM_URL, "nodes": nodes}


# --- mémoire (CortHeXis) : profil courant / recommandé et coûts -------------
def _node_memory(profile: dict) -> dict | None:
    """Recommandation mémoire d'un node : celle que l'agent calcule lui-même
    (agents récents), sinon déduite du profil hardware remonté."""
    from core import profiles

    if profile.get("memory"):
        return profile["memory"]
    gpu = profile.get("gpu") or {}
    acc = None
    if gpu.get("vendor") == "intel" and gpu.get("backend") in ("level_zero", "opencl"):
        acc = {"kind": "sycl", "name": gpu.get("name"), "vram_gb": gpu.get("vram_per_card_gb")
               or gpu.get("vram_total_gb")}
    elif gpu.get("vendor") == "nvidia" and gpu.get("backend") == "cuda":
        acc = {"kind": "cuda", "name": gpu.get("name"), "vram_gb": gpu.get("vram_total_gb")}
    elif gpu.get("vendor") == "apple":
        acc = {"kind": "metal", "name": gpu.get("name"), "vram_gb": gpu.get("vram_total_gb")}
    elif gpu.get("backend") == "vulkan" and gpu.get("vram_total_gb"):
        acc = {"kind": "vulkan", "name": gpu.get("name"), "vram_gb": gpu.get("vram_total_gb")}
    if not profile.get("cores"):
        return None
    rec = profiles.recommend(profile.get("cores"), profile.get("ram_gb"), acc)
    return {k: rec[k] for k in ("recommended", "reason", "warnings", "accel", "hardware")}


def _profiles_with_nodes(plist: list[dict], nodes: list[dict], order) -> list[dict]:
    """A profile the cockpit's own machine cannot hold may still be served by an online
    node (GPU profile on a Magnitude node): `fits` is true then, `fits_on` names it."""
    out = []
    for p in plist:
        q = dict(p)
        q["fits_on"] = "this server" if p.get("fits") else None
        if not p.get("fits"):
            for n in nodes:
                if n.get("online") and n.get("recommended") in order and p["id"] in order \
                        and order.index(n["recommended"]) >= order.index(p["id"]):
                    q["fits"], q["fits_on"] = True, n["name"]
                    break
        out.append(q)
    return out


def memory_view() -> dict:
    """État pour GET /api/magnitude/memory — l'UI (vague 2) n'a qu'à l'afficher.

    current     profil configuré (CORTHEXIS_MEMORY_PROFILE / SOKKAN_MEMORY_PROFILE)
    recommended le meilleur profil servable : cockpit, ou un node Magnitude en
                ligne qui peut porter le profil GPU
    local       recommandation pour la machine du cockpit (vue du conteneur : RAM et
                cœurs de l'hôte, GPU repéré par ses identifiants PCI)
    nodes       recommandation par node Magnitude
    profiles    tableau des profils et de leurs coûts (Go, ms/requête, réindexation)
    engine      moteur actif : identité, modèle, licence, serveurs (sans réseau)
    models      décision sur les Gemma Terms, modèles installés"""
    from core import embed, models as mmodels, profiles

    local = profiles.detect()
    nodes = []
    for nid, node in sorted(load()["nodes"].items(),
                            key=lambda kv: kv[1].get("paired_at") or 0):
        mem = _node_memory(node.get("profile") or {})
        if mem:
            nodes.append({"id": nid, "name": node_name(nid, node), "online": online(node),
                          **mem})
    candidates = [("this server", local["recommended"], local["reason"])] + [
        (n["name"], n["recommended"], n["reason"]) for n in nodes if n["online"]]
    best_on, best, best_reason = max(candidates, key=lambda c: profiles.ORDER.index(c[1]))
    try:
        current = embed.current_profile()
        engine = embed.describe()
    except Exception as e:  # noqa: BLE001 — config invalide : on l'affiche
        current, engine = None, {"error": str(e)}
    st = mmodels.status()
    return {
        "current": current,
        "recommended": best,
        # 3.2.3: the reason shown is the one of the machine that can serve it (was the
        # cockpit's own « 8 cores, 12.6 GB RAM » next to a GPU recommendation)
        "recommended_on": best_on,
        "recommended_reason": best_reason,
        "local": {k: local[k] for k in ("recommended", "reason", "warnings", "accel",
                                        "hardware")},
        "nodes": nodes,
        "profiles": _profiles_with_nodes(local["profiles"], nodes, profiles.ORDER),
        "engine": engine,
        "models": {"active": st["active"], "installed": st["installed"],
                   "licence": {k: v for k, v in st["licence"].items() if k != "history"},
                   "terms_version": st["terms_version"],
                   "terms_url": mmodels.GEMMA_TERMS_URL,
                   "policy_url": mmodels.GEMMA_POLICY_URL},
    }
