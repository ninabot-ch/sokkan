#!/usr/bin/env python3
"""engine.py — SOKKAN Magnitude : llama.cpp prebuilt + GGUF + bench + serve.

Tout vit sous ~/.sokkan/magnitude/ :
  bin/<tag>/   binaires llama.cpp (release GitHub prebuilt, extraits)
  models/      GGUF téléchargés (<model_id>.gguf)
  run/         logs des process (llama-server.log)

- Prebuilt : release GitHub ggml-org/llama.cpp — tag `MAGNITUDE_LLAMA_TAG` ou
  `latest`. Asset par plateforme : macos-arm64 / ubuntu-vulkan-x64 (fallback
  ubuntu-x64) / win-vulkan-x64. Les binaires sont à la racine ou dans
  build/bin/ selon l'archive → recherche par walk.
- Bench : llama-bench -p 512 -n 128 -r 2 -o json, PowerSampler nvidia-smi si
  dispo (macOS/CPU : power et €/M à null). €/M tokens = électricité seule à
  0,30 €/kWh.
- Serve : llama-server 127.0.0.1:8791, -c 16384 --jinja (chat template natif,
  requis pour le tool calling), -ngl 999 si GPU. Health max 120 s (70B = long).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

LLAMA_PORT = 8791
KWH_PRICE_EUR = 0.30
GITHUB_REPO = "ggml-org/llama.cpp"
UA = "sokkan-magnitude/0.1"

WORKDIR = Path(os.environ.get("MAGNITUDE_HOME", "~/.sokkan/magnitude")).expanduser()
BIN_DIR = WORKDIR / "bin"
MODELS_DIR = WORKDIR / "models"
RUN_DIR = WORKDIR / "run"


# ---------------------------------------------------------------- helpers HTTP

def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _download(url: str, dest: Path, progress_cb=None) -> None:
    """Streaming urllib → .part puis rename atomique ; progress_cb(pct 0-100)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress_cb and total:
                progress_cb(min(100.0, round(done * 100.0 / total, 1)))
    os.replace(part, dest)


# ------------------------------------------------------------ llama.cpp prebuilt

def _asset_substrings() -> list:
    if sys.platform == "darwin":
        return ["bin-macos-arm64.tar.gz"]
    if sys.platform.startswith("win"):
        return ["bin-win-vulkan-x64.zip"]
    return ["bin-ubuntu-vulkan-x64.tar.gz", "bin-ubuntu-x64.tar.gz"]


def _pick_asset(assets: list):
    names = {a["name"]: a["browser_download_url"] for a in assets}
    for sub in _asset_substrings():
        for name, url in names.items():
            if sub in name:
                return name, url
    raise RuntimeError(f"no llama.cpp prebuilt asset for this platform "
                       f"(wanted one of {_asset_substrings()})")


def _find_bindir(root: Path):
    """Dossier contenant llama-server/llama-bench (racine ou build/bin/ selon l'archive)."""
    if not root.is_dir():
        return None
    for dirpath, _dirs, files in os.walk(root):
        if "llama-server" in files or "llama-server.exe" in files:
            return Path(dirpath)
    return None


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            try:
                tf.extractall(dest, filter="data")
            except TypeError:  # Python < 3.12
                tf.extractall(dest)
    # chmod +x sur tout (binaires + libs, inoffensif pour le reste)
    for dirpath, _dirs, files in os.walk(dest):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                os.chmod(p, os.stat(p).st_mode | 0o755)
            except OSError:
                pass


def ensure_llama(progress_cb=None) -> Path:
    """Retourne le dossier binaire llama.cpp, en le téléchargeant si besoin."""
    tag = os.environ.get("MAGNITUDE_LLAMA_TAG", "")
    if tag:
        bindir = _find_bindir(BIN_DIR / tag)
        if bindir:
            return bindir
        rel = _http_json(f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}")
    else:
        try:
            rel = _http_json(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
        except (OSError, ValueError):
            # offline : réutiliser une extraction existante si possible
            for d in sorted(BIN_DIR.glob("*"), reverse=True):
                bindir = _find_bindir(d)
                if bindir:
                    return bindir
            raise RuntimeError("cannot reach GitHub for llama.cpp and no local build found")
        tag = rel.get("tag_name", "latest")
        bindir = _find_bindir(BIN_DIR / tag)
        if bindir:
            return bindir
    name, url = _pick_asset(rel.get("assets", []))
    archive = BIN_DIR / name
    _download(url, archive, progress_cb)
    _extract(archive, BIN_DIR / tag)
    archive.unlink(missing_ok=True)
    bindir = _find_bindir(BIN_DIR / tag)
    if not bindir:
        raise RuntimeError(f"llama-server not found in extracted archive {name}")
    return bindir


def _bin(bindir: Path, name: str) -> str:
    exe = bindir / (name + ".exe")
    return str(exe if exe.exists() else bindir / name)


def _env(bindir: Path) -> dict:
    """Libs à côté des binaires → LD_LIBRARY_PATH/DYLD_LIBRARY_PATH sur le dossier."""
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(bindir)
    env["DYLD_LIBRARY_PATH"] = str(bindir)
    return env


# ---------------------------------------------------------------------- GGUF

def gguf_path(model_id: str) -> Path:
    return MODELS_DIR / f"{model_id}.gguf"


def ensure_gguf(entry: dict, progress_cb=None) -> Path:
    """Télécharge le GGUF du catalogue si absent. Vérif taille > 0.9 × weights_gb."""
    dest = gguf_path(entry["id"])
    min_bytes = entry["weights_gb"] * 0.9 * 1e9
    if dest.exists() and dest.stat().st_size > min_bytes:
        return dest
    _download(entry["url"], dest, progress_cb)
    if dest.stat().st_size <= min_bytes:
        size = dest.stat().st_size
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded GGUF for {entry['id']} looks truncated "
                           f"({size / 1e9:.2f} GB < 0.9 × {entry['weights_gb']} GB)")
    return dest


# ---------------------------------------------------------------------- bench

class PowerSampler(threading.Thread):
    """nvidia-smi en boucle 500 ms → samples (watts, util%). Puissance retenue =
    moyenne des samples où util GPU > 20 % (le bench, pas l'idle)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples = []
        self._stop = threading.Event()

    def run(self):
        try:
            proc = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=power.draw,utilization.gpu",
                 "--format=csv,noheader,nounits", "-lms", "500"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                try:
                    w, u = (float(x) for x in line.split(","))
                    self.samples.append((w, u))
                except ValueError:
                    pass
        finally:
            proc.kill()

    def stop(self):
        self._stop.set()

    def power_avg_w(self):
        active = [s for s in self.samples if s[1] > 20]
        pool = active or self.samples
        if not pool:
            return None
        return round(sum(s[0] for s in pool) / len(pool), 1)


def eur_per_mtok(tok_s, watts, kwh_price=KWH_PRICE_EUR):
    """€/M tokens générés — électricité seule."""
    if not tok_s or not watts:
        return None
    hours_per_mtok = 1e6 / tok_s / 3600
    return round((watts / 1000) * hours_per_mtok * kwh_price, 4)


def bench(bindir: Path, gguf: Path, timeout=900) -> dict:
    """llama-bench sur un GGUF → dict au format §2.2 bench (sans la clé model)."""
    sampler = PowerSampler() if shutil.which("nvidia-smi") else None
    if sampler:
        sampler.start()
    t0 = time.time()
    try:
        out = subprocess.run(
            [_bin(bindir, "llama-bench"), "-m", str(gguf),
             "-p", "512", "-n", "128", "-r", "2", "-o", "json"],
            capture_output=True, text=True, timeout=timeout, env=_env(bindir))
    finally:
        if sampler:
            sampler.stop()
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip()[-400:]
        raise RuntimeError(f"llama-bench failed (code {out.returncode}): {tail}")
    gen = prefill = None
    for row in json.loads(out.stdout):
        if row.get("n_prompt") and not row.get("n_gen"):
            prefill = round(row["avg_ts"], 1)
        elif row.get("n_gen") and not row.get("n_prompt"):
            gen = round(row["avg_ts"], 1)
    if gen is None or prefill is None:
        # jamais persister un bench partiel — le front affiche gen/prefill en dur
        raise RuntimeError("llama-bench output missing gen/prefill rows "
                           "(output format change?)")
    watts = sampler.power_avg_w() if sampler else None
    return {
        "gen_tok_s": gen,
        "prefill_tok_s": prefill,
        "power_avg_w": watts,
        "eur_per_mtok_gen": eur_per_mtok(gen, watts),
        "wall_s": round(time.time() - t0, 1),
        "at": round(time.time(), 1),
    }


# ---------------------------------------------------------------------- serve

def start_server(bindir: Path, gguf: Path, gpu=True) -> subprocess.Popen:
    """Lance llama-server (127.0.0.1:8791). --jinja = chat template natif,
    requis pour le tool calling. -ngl 999 = tout offloader si GPU."""
    # 16k par défaut ; une session Claude Code démarre à ~40k de prompt (mesuré
    # dogfood 2026-08-10) → MAGNITUDE_CTX=49152+. À ces tailles le KV f16 déborde
    # les petites VRAM : MAGNITUDE_KV=q8_0 le divise par 2 (impose flash attn).
    ctx = os.environ.get("MAGNITUDE_CTX", "16384")
    cmd = [_bin(bindir, "llama-server"), "-m", str(gguf),
           "--host", "127.0.0.1", "--port", str(LLAMA_PORT),
           "-c", ctx, "--jinja"]
    kv = os.environ.get("MAGNITUDE_KV", "")
    if kv:
        cmd += ["-ctk", kv, "-ctv", kv, "-fa", "on"]
    # échappatoire tuning (rope-scaling yarn, -np, etc.) sans multiplier les env vars
    extra = os.environ.get("MAGNITUDE_SERVER_ARGS", "")
    if extra:
        cmd += extra.split()
    if gpu:
        cmd += ["-ngl", "999"]
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log = open(RUN_DIR / "llama-server.log", "ab")
    try:
        return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                env=_env(bindir))
    finally:
        log.close()  # le fd est dupliqué par Popen


def wait_healthy(proc: subprocess.Popen, timeout=120) -> None:
    """Poll GET /health jusqu'à 200 (un 70B met du temps à charger)."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{LLAMA_PORT}/health"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited early (code {proc.returncode}) "
                               f"— see {RUN_DIR / 'llama-server.log'}")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError(f"llama-server not healthy after {timeout}s")


def stop_server(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
