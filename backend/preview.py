#!/usr/bin/env python3
"""preview.py — SOKKAN P3 : voir le résultat AVANT commit/push/deploy.

Deux sources :
- diff git d'un repo (changements non commités) — voir avant de commit ;
- screenshot d'une URL via chromium headless — voir une page rendue (robuste :
  contourne X-Frame-Options, et le backend gmk1 atteint les services locaux/Tailscale).
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

# repos autorisés au diff (pas de chemin arbitraire) — env JSON {"nom": "/chemin"} ;
# défaut = le workspace courant
import json
_raw_repos = json.loads(os.environ.get("SOKKAN_REPOS", "{}")) or {
    "workspace": os.environ.get("SOKKAN_PROJECT_WD", os.getcwd())}
# valeur = chemin (str) OU {"path": ..., "test_cmd": ...} — test_cmd optionnel,
# lancé UNIQUEMENT sur clic humain (POST /api/preview/test/{repo}), jamais auto
REPOS = {k: (v["path"] if isinstance(v, dict) else v) for k, v in _raw_repos.items()}
TEST_CMDS = {k: v.get("test_cmd", "") for k, v in _raw_repos.items() if isinstance(v, dict)}
SHOT_DIR = Path(os.environ.get("SOKKAN_SHOT_DIR", os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "preview")))
CHROMIUM = os.environ.get("SOKKAN_CHROMIUM", "chromium")
DIFF_MAX = 200_000  # octets
# SSRF policy: by default the screenshot target must resolve to a public address.
# Set SOKKAN_PREVIEW_ALLOW_PRIVATE=1 to allow private/loopback targets (legitimate
# when previewing a local dev-server) — see .env.example.
ALLOW_PRIVATE = os.environ.get("SOKKAN_PREVIEW_ALLOW_PRIVATE", "0") == "1"


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
        raise ValueError(f"unknown repo: {repo}")
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    status = _git(path, "status", "--short")
    d = _git(path, "diff", "HEAD")  # staged + unstaged vs dernier commit
    truncated = len(d) > DIFF_MAX
    # résumé structuré par fichier (numstat) — la vue Preview s'en sert pour la
    # colonne fichiers + compteurs +/- ; les binaires remontent added/deleted null
    files = []
    for line in _git(path, "diff", "--numstat", "HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, r, f = parts
        files.append({"path": f,
                      "added": int(a) if a.isdigit() else None,
                      "deleted": int(r) if r.isdigit() else None})
    return {"repo": repo, "path": path, "branch": branch,
            "status": status, "diff": d[:DIFF_MAX], "truncated": truncated,
            "files": files, "has_tests": bool(TEST_CMDS.get(repo))}


def run_tests(repo: str) -> dict:
    """Lance la commande de tests déclarée pour ce repo (SOKKAN_REPOS →
    {"path":…, "test_cmd":…}). Déclenchée par un CLIC humain uniquement."""
    path = REPOS.get(repo)
    cmd = TEST_CMDS.get(repo, "")
    if not path or not cmd:
        raise ValueError(f"no test_cmd configured for repo: {repo}")
    r = subprocess.run(cmd, shell=True, cwd=path, capture_output=True,
                       text=True, timeout=600)
    tail = (r.stdout + r.stderr)[-4000:]
    return {"repo": repo, "cmd": cmd, "code": r.returncode,
            "passed": r.returncode == 0, "output": tail}


def _assert_url_allowed(url: str) -> None:
    """Reject screenshot targets that resolve to private/loopback/link-local
    addresses (SSRF guard — includes cloud metadata 169.254.169.254), unless
    SOKKAN_PREVIEW_ALLOW_PRIVATE=1. Resolution happens BEFORE Chromium runs."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("http(s) URL with a hostname required")
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve host {u.hostname!r}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
            raise ValueError(
                f"{u.hostname} resolves to a non-public address ({ip}) — "
                "set SOKKAN_PREVIEW_ALLOW_PRIVATE=1 to allow private targets"
            )


def screenshot(url: str, width: int = 1440, height: int = 900) -> Path:
    import shutil
    if not shutil.which(CHROMIUM):
        raise RuntimeError(
            f"{CHROMIUM} not found — screenshots need a Chromium binary "
            "(install it, or set SOKKAN_CHROMIUM). The diff and env views work without it.")
    _assert_url_allowed(url)
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
        raise RuntimeError("screenshot failed")
    return out
