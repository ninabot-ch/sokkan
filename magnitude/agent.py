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

from . import __version__, catalog, discover, engine, hw, metrics

SHIM_PORT = 8790
SYNC_INTERVAL = 2.0
SYNC_TIMEOUT = 5
DISCOVER_INTERVAL = 30.0   # engines already running on the node (discover.py)
METRICS_INTERVAL = 3.0     # 0.3: live load per card + CPU/RAM (metrics.py)
# UA explicite : sans lui, un cockpit derrière Cloudflare (sokkan.ch, SOKKAN
# Cloud) répond 403 au défaut « Python-urllib » et le sync échoue en silence.
UA = "sokkan-magnitude/0.1"


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
        self._engines = None       # last engines list found (discover.scan)

    # ------------------------------------------------------------- boucle main

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        _log(f"agent v{__version__} → {self.cockpit}")
        self.profile = hw.build_profile()
        self.profile["agent_version"] = __version__   # 0.2: engines running + attach; 0.3: metrics
        if self.profile.get("gpu"):
            # 0.3: the cards Magnitude's own Run / Benchmark may use (MAGNITUDE_GPU_DEVICES)
            self.profile["gpu"]["run_devices"] = engine.run_devices(
                [d.get("index") for d in self.profile["gpu"].get("devices") or []]
                or list(range(int(self.profile["gpu"].get("count") or 1))))
        # 0.3: how a Run executes here (docker SYCL image / prebuilt Vulkan-Metal-CPU)
        self.profile["runtime"] = engine.runtime_for(self.profile)
        try:  # profil mémoire recommandé (CortHeXis), remonté avec le profil hardware
            from . import memprofile
            mem = memprofile.detect()
            self.profile["memory"] = {k: mem[k] for k in
                                      ("recommended", "reason", "warnings", "accel", "hardware")}
        except Exception as e:  # noqa: BLE001 — jamais bloquant pour l'agent LLM
            _log(f"memory profile unavailable: {e}")
        _log(f"profile: class {self.profile['class']}, "
             f"gpu {(self.profile.get('gpu') or {}).get('name') or 'none'}")
        with self._lock:
            self._outbox["profile"] = self.profile
        worker = threading.Thread(target=self._work, daemon=True)
        worker.start()
        threading.Thread(target=self._discover_loop, daemon=True).start()
        threading.Thread(target=self._metrics_loop, daemon=True).start()
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
                         "User-Agent": UA,
                         "x-magnitude-token": self.token})
            with urllib.request.urlopen(req, timeout=SYNC_TIMEOUT) as r:
                resp = json.loads(r.read())
        except (OSError, ValueError):
            with self._lock:  # rejouer sans écraser plus frais
                for k, v in pending.items():
                    if k != "metrics":   # a stale load sample is worth nothing
                        self._outbox.setdefault(k, v)
            return None
        # 0.3: a cockpit that lost (or never stored) what we sent asks for it again —
        # e.g. a cockpit upgraded after the agent, which had ignored the engines list
        resend = resp.get("resend") or []
        if resend:
            with self._lock:
                if "profile" in resend and self.profile:
                    self._outbox.setdefault("profile", self.profile)
                if "engines" in resend and self._engines is not None:
                    self._outbox.setdefault("engines", self._engines)
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
                if action == "attach":
                    self._do_attach(model, cmd.get("port"))
                elif action == "bench":
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

    def _prepare(self, model_id: str, for_run: bool = False):
        """llama.cpp + GGUF présents localement, avec progression remontée."""
        bindir = None
        if not (for_run and engine.runtime_for(self.profile or {}) == "docker"):
            bindir = engine.ensure_llama(
                progress_cb=lambda pct: self._set_status(
                    "downloading", model=model_id, pct=pct, detail="llama.cpp runtime"))
        entry = catalog.get(model_id)
        gguf = engine.ensure_gguf(
            entry, progress_cb=lambda pct: self._set_status(
                "downloading", model=model_id, pct=pct, detail=entry["label"]))
        return entry, bindir, gguf

    def _gpu_run(self) -> bool:
        """Our llama.cpp may offload to the GPU: a usable backend AND at least one card
        allowed by MAGNITUDE_GPU_DEVICES (none = CPU only — the cards serve others)."""
        g = (self.profile or {}).get("gpu") or {}
        usable = g.get("backend") in ("cuda", "vulkan", "metal") or g.get("offload") == "vulkan"
        return usable and g.get("run_devices") != []

    def _do_bench(self, model_id: str) -> None:
        entry, bindir, gguf = self._prepare(model_id)
        self._set_status("benching", model=model_id)
        _log(f"benching {entry['label']}…")
        gpu = self._gpu_run()
        res = engine.bench(bindir, gguf, gpu=gpu)
        devs = ((self.profile or {}).get("gpu") or {}).get("run_devices")
        res["on"] = ("gpu " + ",".join(f"#{d}" for d in devs)) if gpu and devs else ("gpu" if gpu else "cpu")
        _log(f"bench {model_id}: {res.get('gen_tok_s')} gen tok/s")
        with self._lock:
            self._outbox["bench_result"] = {"model": model_id, **res}
        self._set_idle()

    def _do_run(self, model_id: str) -> None:
        entry, bindir, gguf = self._prepare(model_id, for_run=True)
        self._teardown_serving()  # un seul modèle servi à la fois
        self._set_status("starting", model=model_id)
        g = (self.profile or {}).get("gpu") or {}
        runtime = engine.runtime_for(self.profile or {})
        if runtime == "docker":
            cards = g.get("run_devices")
            if cards is None:
                cards = [d.get("index") for d in g.get("devices") or []]
            self._set_status("starting", model=model_id,
                             detail=f"docker {g.get('vendor')} image on card(s) {','.join(map(str, cards))}")
            self._server = engine.start_docker_server(gguf, cards, g.get("vendor") or "intel")
        else:
            self._server = engine.start_server(bindir, gguf, gpu=self._gpu_run())
        try:
            engine.wait_healthy(self._server, timeout=300 if getattr(self._server, "container", None) else 120)
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

    # ------------------------------------------------------- engines running

    def _discover_loop(self) -> None:
        """Every DISCOVER_INTERVAL: the engines already served on this node, sent with the
        next sync EVERY time (0.3 — 0.2 sent it only on change, so a cockpit upgraded
        afterwards never got it and showed no engine)."""
        while not self._stopping.is_set():
            skip = {engine.LLAMA_PORT}
            if self._shim is not None:
                skip.add(SHIM_PORT)
            try:
                found = discover.scan(skip=skip)
            except Exception as e:  # noqa: BLE001 — discovery never stops the agent
                _log(f"discovery failed: {e}")
                found = None
            if found is not None:
                if found != self._engines:
                    _log("engines running: " + (", ".join(
                        f"{e['model']}@{e['port']}" for e in found) or "none"))
                self._engines = found
                with self._lock:
                    self._outbox["engines"] = found
            self._stopping.wait(DISCOVER_INTERVAL)

    def _metrics_loop(self) -> None:
        """Every METRICS_INTERVAL: load per card, CPU and RAM — joined to the next sync."""
        totals = {d.get("pci"): d.get("vram_gb")
                  for d in ((self.profile or {}).get("gpu") or {}).get("devices") or []
                  if d.get("pci")}
        sampler = metrics.Sampler(totals)
        while not self._stopping.is_set():
            try:
                snap = sampler.sample()
            except Exception as e:  # noqa: BLE001 — metrics never stop the agent
                _log(f"metrics failed: {e}")
                snap = None
            if snap:
                with self._lock:
                    self._outbox["metrics"] = snap
            self._stopping.wait(METRICS_INTERVAL)

    def _do_attach(self, model_id: str, port) -> None:
        """Put the shim in front of an engine that is ALREADY running on this node (no
        download, no llama-server of ours): SOKKAN sessions then use it after Connect."""
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ValueError("attach needs the engine port") from None
        eng = next((e for e in discover.probe(port) if e["model"] == model_id), None)
        if eng is None:
            raise RuntimeError(f"no engine serves {model_id!r} on port {port} any more")
        self._teardown_serving()
        self._set_status("starting", model=model_id, detail=f"{eng['engine']} on :{port}")
        serve_token = secrets.token_urlsafe(18)
        from . import shim
        self._shim = shim.Shim(upstream=eng["base_url"], port=SHIM_PORT, token=serve_token,
                               model_id=model_id)
        self._shim.start()
        self._serving = {"model": model_id, "shim_port": SHIM_PORT,
                         "serve_token": serve_token, "since": round(time.time(), 1),
                         "engine": eng["engine"], "port": port, "external": True}
        with self._lock:
            self._outbox["serving"] = dict(self._serving)
        self._set_status("serving", model=model_id)
        _log(f"{model_id} ({eng['engine']} :{port}) attached — shim on :{SHIM_PORT}")

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
