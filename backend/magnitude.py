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
              "status", "bench", "serving", "pending")


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


def _usable_gb(profile: dict) -> float | None:
    """Mémoire utilisable (Go) pour les poids : VRAM GPU telle que remontée
    (hw.py donne déjà 75 % de la RAM unifiée sur Apple Silicon), sinon 50 % de
    la RAM en classe CPU. None = machine non servable (class unsupported)."""
    gpu = profile.get("gpu") or {}
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


def catalog_view(profile: dict | None) -> list[dict]:
    """Catalogue pour l'UI, annoté du fit vs profil (sans les URLs de poids)."""
    usable = _usable_gb(profile) if profile else None
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
def set_pending(node_id: str, action: str, model: str = "") -> bool:
    """Pose la commande à livrer au prochain sync du node (une seule en vol)."""
    with _LOCK:
        st = load()
        node = st["nodes"].get(node_id)
        if node is None:
            return False
        node["pending"] = {"action": action, "model": model}
        save(st)
    return True


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
        node["last_seen"] = time.time()
        cmd = node.pop("pending", None)
        save(st)
    return cmd or None


# --- vue UI ------------------------------------------------------------------
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
        nodes.append({
            "id": nid,
            "name": node_name(nid, node),
            "online": online(node),
            "last_seen": node.get("last_seen"),
            "shim_url": shim_url_of(node),
            "profile": node.get("profile"),
            "status": node.get("status") or {"phase": "idle"},
            "bench": node.get("bench") or {},
            "serving": ({"model": serving.get("model"),
                         "shim_url": shim_url_of(node),
                         "since": serving.get("since")} if serving else None),
            "connected": nid == conn,
            "catalog": catalog_view(node.get("profile")),
        })
    return {"paired": bool(nodes), "shim_default": SHIM_URL, "nodes": nodes}
