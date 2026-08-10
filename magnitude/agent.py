#!/usr/bin/env python3
"""agent.py — SOKKAN Magnitude : boucle de sync agent ⇄ cockpit.

Protocole (spec §2.4) : profil hardware au boot, puis POST
{cockpit}/api/magnitude/agent/sync toutes les 2 s avec le header
x-magnitude-token. L'agent ne reçoit JAMAIS de connexion entrante du cockpit —
tout est HTTP sortant (NAT/firewall-friendly). Le seul port ouvert est le shim
:8790 (auth serve_token) qui sert l'inférence au container.

Les commandes (bench/run/stop) arrivent dans la réponse du sync et s'exécutent
dans un worker thread pour que la boucle continue de remonter status/pct
pendant les opérations longues. L'agent ne crashe jamais : toute erreur
d'opération part en {"error": ...} + status phase=error.
"""
from __future__ import annotations

import json
import queue
import secrets
import signal
import threading
import time
import urllib.request

from . import __version__, catalog, engine, hw

SHIM_PORT = 8790
SYNC_INTERVAL = 2.0
SYNC_TIMEOUT = 5


def _log(msg: str) -> None:
    print(f"[magnitude] {msg}", flush=True)


class Agent:
    def __init__(self, cockpit: str, token: str):
        self.cockpit = cockpit.rstrip("/")
        self.token = token
        self._lock = threading.Lock()
        self._status = {"phase": "idle"}
        self._outbox = {}          # champs à joindre au prochain sync réussi
        self._cmds = queue.Queue()
        self._stopping = threading.Event()
        self._server = None        # Popen llama-server
        self._shim = None          # shim.Shim
        self._serving = None       # dict §2.2 serving (avec serve_token)
        self.profile = None

    # ------------------------------------------------------------- boucle main

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        _log(f"agent v{__version__} → {self.cockpit}")
        self.profile = hw.build_profile()
        _log(f"profile: class {self.profile['class']}, "
             f"gpu {(self.profile.get('gpu') or {}).get('name') or 'none'}")
        with self._lock:
            self._outbox["profile"] = self.profile
        worker = threading.Thread(target=self._work, daemon=True)
        worker.start()
        while not self._stopping.is_set():
            cmd = self._sync()
            if cmd:
                _log(f"command: {cmd.get('action')} {cmd.get('model') or ''}".strip())
                self._cmds.put(cmd)
            self._stopping.wait(SYNC_INTERVAL)
        self._shutdown()

    def _on_signal(self, _signum, _frame):
        self._stopping.set()

    def _shutdown(self) -> None:
        _log("shutting down…")
        try:
            self._teardown_serving()
        except Exception:
            pass
        self._sync()  # best-effort : remonter serving=null avant de partir
        _log("bye")

    # -------------------------------------------------------------------- sync

    def _sync(self):
        """Un POST sync. Erreurs réseau silencieuses (l'outbox est rejouée)."""
        with self._lock:
            body = {"status": dict(self._status)}
            pending, self._outbox = self._outbox, {}
            body.update(pending)
        try:
            req = urllib.request.Request(
                f"{self.cockpit}/api/magnitude/agent/sync",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "x-magnitude-token": self.token})
            with urllib.request.urlopen(req, timeout=SYNC_TIMEOUT) as r:
                resp = json.loads(r.read())
        except (OSError, ValueError):
            with self._lock:  # rejouer sans écraser plus frais
                for k, v in pending.items():
                    self._outbox.setdefault(k, v)
            return None
        return resp.get("command")

    def _set_status(self, phase, model=None, pct=None, detail=None):
        st = {"phase": phase}
        if model:
            st["model"] = model
        if pct is not None:
            st["pct"] = round(float(pct), 1)
        if detail:
            st["detail"] = detail
        with self._lock:
            self._status = st

    def _set_idle(self):
        """Retour au repos : phase serving si un modèle tourne, sinon idle."""
        if self._serving:
            self._set_status("serving", model=self._serving["model"])
        else:
            self._set_status("idle")

    # ------------------------------------------------------------------ worker

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                cmd = self._cmds.get(timeout=0.5)
            except queue.Empty:
                continue
            action, model = cmd.get("action"), cmd.get("model")
            try:
                if action == "bench":
                    self._do_bench(model)
                elif action == "run":
                    self._do_run(model)
                elif action == "stop":
                    self._do_stop()
                else:
                    raise ValueError(f"unknown action: {action}")
            except Exception as e:  # l'agent ne crashe jamais
                msg = (str(e) or e.__class__.__name__)[:300]
                _log(f"error: {msg}")
                self._set_status("error", model=model, detail=msg)
                with self._lock:
                    self._outbox["error"] = msg

    def _prepare(self, model_id: str):
        """llama.cpp + GGUF présents localement, avec progression remontée."""
        bindir = engine.ensure_llama(
            progress_cb=lambda pct: self._set_status(
                "downloading", model=model_id, pct=pct, detail="llama.cpp runtime"))
        entry = catalog.get(model_id)
        gguf = engine.ensure_gguf(
            entry, progress_cb=lambda pct: self._set_status(
                "downloading", model=model_id, pct=pct, detail=entry["label"]))
        return entry, bindir, gguf

    def _do_bench(self, model_id: str) -> None:
        entry, bindir, gguf = self._prepare(model_id)
        self._set_status("benching", model=model_id)
        _log(f"benching {entry['label']}…")
        res = engine.bench(bindir, gguf)
        _log(f"bench {model_id}: {res.get('gen_tok_s')} gen tok/s")
        with self._lock:
            self._outbox["bench_result"] = {"model": model_id, **res}
        self._set_idle()

    def _do_run(self, model_id: str) -> None:
        entry, bindir, gguf = self._prepare(model_id)
        self._teardown_serving()  # un seul modèle servi à la fois
        self._set_status("starting", model=model_id)
        gpu = ((self.profile or {}).get("gpu") or {}).get("backend") in (
            "cuda", "vulkan", "metal")
        self._server = engine.start_server(bindir, gguf, gpu=gpu)
        try:
            engine.wait_healthy(self._server)
        except Exception:
            engine.stop_server(self._server)
            self._server = None
            raise
        serve_token = secrets.token_urlsafe(18)
        from . import shim  # import tardif : uniquement nécessaire pour servir
        self._shim = shim.Shim(upstream=f"http://127.0.0.1:{engine.LLAMA_PORT}",
                               port=SHIM_PORT, token=serve_token,
                               model_id=model_id)
        self._shim.start()
        self._serving = {"model": model_id, "shim_port": SHIM_PORT,
                         "serve_token": serve_token, "since": round(time.time(), 1)}
        with self._lock:
            self._outbox["serving"] = dict(self._serving)
        self._set_status("serving", model=model_id)
        _log(f"{entry['label']} live — shim on :{SHIM_PORT}")

    def _do_stop(self) -> None:
        self._teardown_serving()
        self._set_status("idle")
        _log("stopped")

    def _teardown_serving(self) -> None:
        """Tue shim + llama-server et remonte serving=null au prochain sync."""
        if self._shim is not None:
            try:
                self._shim.stop()
            except Exception:
                pass
            self._shim = None
        engine.stop_server(self._server)
        self._server = None
        if self._serving is not None:
            self._serving = None
            with self._lock:
                self._outbox["serving"] = None
