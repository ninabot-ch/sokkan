#!/usr/bin/env python3
"""previewenv.py — SOKKAN P3 : environnements de preview du WIP (non commité).

Lance un dev-server depuis un working tree (ex. `next dev` de ninjob/frontend) sur
un port dédié de gmk1 → on prévisualise le code MODIFIÉ avant commit/push/deploy
(le backend gmk1 atteint le dev-server en loopback ; on le screenshot via /shot).

Config éditable dans preview-envs.json (seedée). `{port}` est substitué dans cmd.
Démarrage non bloquant (Popen + le front poll le statut) ; arrêt via fuser -k.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

CFG = Path(os.environ.get("SOKKAN_PREVIEW_ENVS", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "preview-envs.json")))
LOG_DIR = Path(os.environ.get("SOKKAN_SHOT_DIR", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "preview")))

_SEED = {
    "ninjob-frontend": {
        "cwd": "/root/ninjob-work/frontend",
        "cmd": "npm run dev -- -p {port}",
        "port": 4311,
        "label": "ninjob — frontend (next dev, WIP)",
    },
}


def _load() -> dict:
    if not CFG.exists():
        CFG.parent.mkdir(parents=True, exist_ok=True)
        CFG.write_text(json.dumps(_SEED, indent=2), encoding="utf-8")
        return dict(_SEED)
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_SEED)


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def list_envs() -> list[dict]:
    out = []
    for name, e in _load().items():
        port = int(e["port"])
        out.append({
            "name": name, "label": e.get("label", name), "cwd": e["cwd"],
            "port": port, "url": f"http://localhost:{port}",
            # URL interactive publique (cloudflared + CF Access ; cf. <env>-preview.ninabot.ch)
            "preview_url": e.get("host", f"https://{name}-preview.ninabot.ch"),
            "running": _listening(port),
            "cwd_exists": Path(e["cwd"]).is_dir(),
        })
    return out


def start(name: str) -> dict:
    envs = _load()
    if name not in envs:
        raise ValueError(f"env inconnu: {name}")
    e = envs[name]
    port = int(e["port"])
    if _listening(port):
        return {"running": True, "url": f"http://localhost:{port}"}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / f"{name}.dev.log", "ab")  # noqa: SIM115
    env = {**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PORT": str(port)}
    subprocess.Popen(  # noqa: S602 — cmd vient d'une config locale de confiance
        e["cmd"].replace("{port}", str(port)),
        shell=True, cwd=e["cwd"], stdout=log, stderr=log,
        start_new_session=True, env=env,
    )
    return {"running": False, "starting": True, "url": f"http://localhost:{port}"}


def stop(name: str) -> dict:
    envs = _load()
    if name not in envs:
        raise ValueError(f"env inconnu: {name}")
    port = int(envs[name]["port"])
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=10)
    return {"running": False}


# ---------- trigger par session ----------
# L'aperçu a du sens quand une SESSION le déclenche (elle sait ce qu'elle vient
# de modifier et où le voir) — un start manuel pointe sur rien. Une session
# pousse son WIP via l'outil MCP open_preview → on démarre l'env + on retient
# qui/quoi/quand pour que l'onglet Preview s'ouvre directement au bon endroit.

TRIGGER = Path(os.environ.get("SOKKAN_PREVIEW_TRIGGER", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "preview-trigger.json")))


def trigger(env: str, path: str = "/", session_id: str = "", tag: str = "",
            window: str = "", user: str = "") -> dict:
    st = start(env)  # ValueError si env inconnu (propagée)
    data = {
        "env": env, "path": path if path.startswith("/") else f"/{path}",
        "session_id": session_id, "tag": tag, "window": window, "user": user,
        "ts": time.time(),
    }
    TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    TRIGGER.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {**data, **st}


def latest_trigger() -> dict | None:
    if not TRIGGER.exists():
        return None
    try:
        data = json.loads(TRIGGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    envs = _load()
    e = envs.get(data.get("env", ""))
    if e:
        data["running"] = _listening(int(e["port"]))
        data["url"] = f"http://localhost:{e['port']}"
        data["preview_url"] = e.get("host", f"https://{data['env']}-preview.ninabot.ch")
    return data
