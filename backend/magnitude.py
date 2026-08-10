#!/usr/bin/env python3
"""magnitude.py — SOKKAN : état cockpit de la feature Magnitude (LLM local).

Magnitude profile le hardware du client, benche les modèles locaux servables
et en fait tourner un (llama.cpp) branché au router LLM en un clic. L'agent
host (`python3 -m magnitude` — package séparé, JAMAIS importé ici : le
container n'y a pas accès) poll le backend en HTTP sortant ; ce module tient
l'état partagé dans `$SOKKAN_DATA_DIR/magnitude.json` (chmod 600 — il contient
le serve_token) :

  {"token_sha256": "…",             # sha256 du pairing token (jamais le clair)
   "profile": {…},                  # profil hardware remonté par l'agent
   "last_seen": 1754800000.0,       # dernier sync agent (online = < 15 s)
   "status": {"phase": "…", …},     # idle|benching|downloading|starting|serving|error
   "bench": {"<model>": {…}},       # résultats de bench par modèle
   "serving": {"model", "shim_port", "serve_token", "since"},
   "pending": {"action", "model"}}  # commande UI, livrée au prochain sync

Le pairing token est généré ici (secrets.token_urlsafe) et montré UNE fois à
l'UI ; l'agent le renvoie en header `x-magnitude-token`, vérifié en
constant-time sur son sha256. Le serve_token (généré par l'agent) ne sort
JAMAIS vers l'UI — il ne part que dans llm.json au « Connect ».
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
SHIM_URL = "http://host.docker.internal:8790"  # shim Anthropic-compatible sur le host
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


def load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, ValueError):
        return {}


def save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        STATE.chmod(0o600)  # contient le serve_token
    except OSError:
        pass


# --- pairing -----------------------------------------------------------------
def pair() -> str:
    """Regénère le pairing token et REPART DE ZÉRO (pairing = nouveau départ :
    profil/bench/serving appartiennent à la machine précédente). Retourne le
    token en clair — montré une seule fois, seul son sha256 est persisté."""
    token = secrets.token_urlsafe(24)
    with _LOCK:
        save({"token_sha256": hashlib.sha256(token.encode()).hexdigest()})
    return token


def unpair() -> None:
    """Efface tout l'état (token, profil, bench, serving, pending)."""
    with _LOCK:
        STATE.unlink(missing_ok=True)


def verify_token(token: str) -> bool:
    """Compare le token agent (header x-magnitude-token) au sha256 stocké."""
    stored = load().get("token_sha256") or ""
    if not stored or not token:
        return False
    return secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), stored)


def online(state: dict | None = None) -> bool:
    st = load() if state is None else state
    last = st.get("last_seen") or 0
    return bool(last) and (time.time() - last) < ONLINE_WINDOW_S


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
def set_pending(action: str, model: str = "") -> None:
    """Pose la commande à livrer au prochain sync agent (une seule en vol)."""
    with _LOCK:
        st = load()
        st["pending"] = {"action": action, "model": model}
        save(st)


# --- sync agent --------------------------------------------------------------
def sync(payload: dict, serving_set: bool) -> dict | None:
    """Applique un sync agent (profil/status/bench/serving/erreur) + last_seen,
    et livre la commande pending s'il y en a une (effacée à la livraison).
    `serving_set` distingue `serving: null` (stop effectif) du champ absent."""
    with _LOCK:
        st = load()
        if payload.get("profile") is not None:
            st["profile"] = payload["profile"]
        if payload.get("status") is not None:
            st["status"] = payload["status"]
        elif payload.get("error"):
            st["status"] = {"phase": "error", "detail": str(payload["error"])[:300]}
        br = payload.get("bench_result")
        if br and br.get("model"):
            entry = {k: v for k, v in br.items() if k != "model"}
            entry.setdefault("at", time.time())
            st.setdefault("bench", {})[br["model"]] = entry
        if serving_set:
            st["serving"] = payload.get("serving")
        st["last_seen"] = time.time()
        cmd = st.pop("pending", None)
        save(st)
    return cmd or None


# --- vue UI ------------------------------------------------------------------
def connected() -> bool:
    """Le router LLM de l'instance pointe-t-il sur le shim Magnitude ?"""
    c = llm.load()
    return c.get("mode") == "custom" and "host.docker.internal:8790" in (c.get("base_url") or "")


def view() -> dict:
    """État complet pour GET /api/magnitude — le serve_token n'en sort jamais."""
    st = load()
    serving = st.get("serving") or None
    return {
        "paired": bool(st.get("token_sha256")),
        "online": online(st),
        "last_seen": st.get("last_seen"),
        "profile": st.get("profile"),
        "status": st.get("status") or {"phase": "idle"},
        "bench": st.get("bench") or {},
        "serving": ({"model": serving.get("model"), "shim_url": SHIM_URL,
                     "since": serving.get("since")} if serving else None),
        "connected": connected(),
        "catalog": catalog_view(st.get("profile")),
    }
