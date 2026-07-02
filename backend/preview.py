#!/usr/bin/env python3
"""preview.py — SOKKAN P3 : voir le résultat AVANT commit/push/deploy.

Deux sources :
- diff git d'un repo (changements non commités) — voir avant de commit ;
- screenshot d'une URL via chromium headless — voir une page rendue (robuste :
  contourne X-Frame-Options, et le backend gmk1 atteint les services locaux/Tailscale).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

# repos autorisés au diff (pas de chemin arbitraire) — env JSON {"nom": "/chemin"} ;
# défaut = le workspace courant
import json
REPOS = json.loads(os.environ.get("SOKKAN_REPOS", "{}")) or {"workspace": os.environ.get("SOKKAN_PROJECT_WD", os.getcwd())}
SHOT_DIR = Path(os.environ.get("SOKKAN_SHOT_DIR", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "preview")))
CHROMIUM = os.environ.get("SOKKAN_CHROMIUM", "chromium")
DIFF_MAX = 200_000  # octets


def _git(path: str, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", path, *args], capture_output=True, text=True, timeout=15
    )
    return r.stdout


def list_repos() -> list[dict]:
    out = []
    for name, path in REPOS.items():
        if not Path(path, ".git").is_dir():
            continue
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        modified = len([l for l in _git(path, "status", "--short").splitlines() if l.strip()])
        out.append({"name": name, "path": path, "branch": branch, "modified": modified})
    return out


def diff(repo: str) -> dict:
    path = REPOS.get(repo)
    if not path:
        raise ValueError(f"repo inconnu: {repo}")
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    status = _git(path, "status", "--short")
    d = _git(path, "diff", "HEAD")  # staged + unstaged vs dernier commit
    truncated = len(d) > DIFF_MAX
    return {"repo": repo, "path": path, "branch": branch,
            "status": status, "diff": d[:DIFF_MAX], "truncated": truncated}


def screenshot(url: str, width: int = 1440, height: int = 900) -> Path:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL http(s) requise")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{url}|{width}|{height}".encode()).hexdigest()[:16]
    out = SHOT_DIR / f"{key}.png"
    subprocess.run(
        [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         f"--screenshot={out}", f"--window-size={width},{height}", url],
        capture_output=True, timeout=60,
    )
    if not out.exists():
        raise RuntimeError("capture échouée")
    return out
