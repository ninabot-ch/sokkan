#!/usr/bin/env python3
"""heartbeat.py — SOKKAN appliance: outbound supervision heartbeat (opt-in, no client content).

Off unless ``SOKKAN_HEARTBEAT_URL`` is set (and ``SOKKAN_HEARTBEAT`` is not ``0``). One small
JSON POST every ``SOKKAN_HEARTBEAT_INTERVAL_S`` seconds (default 900) so the operator knows the
box is alive and healthy. The payload is exactly the fields of ``FIELDS`` — nothing about the
client's data: no host name, no IP, no e-mail, no project / session / note names or counts.
``GET /api/appliance/heartbeat`` (admin) shows the exact payload. Documented in
docs/appliance/HEARTBEAT.md (a test keeps the doc and ``FIELDS`` in sync).

Config is read at call time (tests and operators can change the environment).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Depends

import features

SCHEMA = 1
FIELDS = (
    "schema", "instance", "ts", "version", "commit", "edition", "uptime_s", "health",
    "backup_age_s", "backup_ok", "disk_free_pct", "disk_free_gb", "memory_review_score",
    "support_tunnel_open",
)

# Set by app.py when an inference backend can be probed: () -> True / False / None (unknown).
inference_probe: Callable[[], bool | None] | None = None

_BOOT = time.time()
_state: dict = {"last_sent_ts": None, "last_ok": None, "last_status": None, "last_error": ""}
_volatile_id = uuid.uuid4().hex[:16]


def _data_dir() -> str:
    return os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))


def url() -> str:
    return (os.environ.get("SOKKAN_HEARTBEAT_URL") or "").strip()


def enabled() -> bool:
    if (os.environ.get("SOKKAN_HEARTBEAT") or "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(url())


def interval_s() -> int:
    try:
        v = int(features.env_num("SOKKAN_HEARTBEAT_INTERVAL_S", 900, int))
    except (TypeError, ValueError):
        v = 900
    return max(60, v)


def instance_id() -> str:
    env = (os.environ.get("SOKKAN_INSTANCE_ID") or "").strip()
    if env:
        return env
    p = Path(_data_dir()) / "heartbeat-id"
    try:
        v = p.read_text().strip()
        if v:
            return v
    except OSError:
        pass
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_volatile_id + "\n")
    except OSError:
        pass
    return _volatile_id


def _version() -> str:
    env = (os.environ.get("SOKKAN_VERSION") or "").strip()
    if env and env != "dev":
        return env
    root = Path(__file__).resolve().parent.parent
    for cand in (root / "VERSION", Path("/app/VERSION")):
        try:
            v = cand.read_text().strip()
        except OSError:
            continue
        if v:
            return v
    return env or "dev"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a probe never breaks the payload
        return default


def _db_up() -> bool | None:
    dsn = (os.environ.get("CORTHEXIS_DATABASE_URL") or "").strip()
    host, port = "db", 5432
    if dsn:
        u = urlparse(dsn)
        host, port = u.hostname or host, u.port or port
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _inference_up() -> bool | None:
    probe = inference_probe
    if probe is None:
        return None
    r = probe()
    return None if r is None else bool(r)


def _disk() -> tuple[float | None, float | None]:
    d = Path(_data_dir())
    while not d.exists() and d != d.parent:
        d = d.parent
    u = shutil.disk_usage(d)
    return round(100.0 * u.free / u.total, 1), round(u.free / 1e9, 1)


def _review_score():
    """Latest CortHeXis review score: the in-memory report of the running review loop
    (corthexis._state), else the last one recorded in its history. Never imports corthexis."""
    cx = sys.modules.get("corthexis")
    if cx is None:
        return None
    rep = cx._state.get("report") or cx.history().last_report()
    score = (rep or {}).get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return score


def payload() -> dict:
    import appliance
    bk = _safe(appliance.backup_status, {}) or {}
    ok = None
    if bk.get("last_attempt_ts") is not None:
        ls, la = bk.get("last_success_ts"), bk["last_attempt_ts"]
        ok = bool(isinstance(ls, (int, float)) and ls >= la and not bk.get("last_error"))
    pct, gb = _safe(_disk, (None, None))
    return {
        "schema": SCHEMA,
        "instance": _safe(instance_id, ""),
        "ts": int(time.time()),
        "version": _safe(_version, "dev"),
        "commit": (os.environ.get("SOKKAN_COMMIT") or "").strip(),
        "edition": _safe(features.edition, ""),
        "uptime_s": int(time.time() - _BOOT),
        "health": {"api": True, "db": _safe(_db_up), "inference": _safe(_inference_up)},
        "backup_age_s": bk.get("age_s"),
        "backup_ok": ok,
        "disk_free_pct": pct,
        "disk_free_gb": gb,
        "memory_review_score": _safe(_review_score),
        "support_tunnel_open": bool(_safe(lambda: appliance.support_state()["open"], False)),
    }


def send_once() -> dict:
    """POST the payload once. Never raises; the outcome is kept in state()."""
    if not enabled():
        return {"ok": False, "status": None, "error": "disabled"}
    res: dict = {"ok": False, "status": None, "error": ""}
    try:
        data = json.dumps(payload()).encode()
        headers = {"Content-Type": "application/json", "User-Agent": f"sokkan-heartbeat/{_version()}"}
        tok = (os.environ.get("SOKKAN_HEARTBEAT_TOKEN") or "").strip()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(url(), data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            res["status"] = r.status
            res["ok"] = 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        res["status"], res["error"] = e.code, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        res["error"] = type(e).__name__
    _state.update(last_sent_ts=int(time.time()), last_ok=res["ok"], last_status=res["status"],
                  last_error=res["error"])
    return res


def state() -> dict:
    host = ""
    try:
        host = urlparse(url()).hostname or ""
    except ValueError:
        pass
    return {"enabled": enabled(), "url_host": host, "interval_s": interval_s(),
            "last_sent_ts": _state["last_sent_ts"], "last_ok": _state["last_ok"],
            "last_status": _state["last_status"], "fields": list(FIELDS)}


def router(require) -> APIRouter:
    r = APIRouter()

    @r.get("/api/appliance/heartbeat")
    def heartbeat_preview(_u: dict = Depends(require("admin"))) -> dict:
        return {"state": state(), "payload": payload()}

    return r


def _loop() -> None:
    time.sleep(60)
    while True:
        if enabled():
            send_once()  # silent on failure: the outcome is in state()
        time.sleep(interval_s())


def start() -> None:
    if not enabled():
        return
    threading.Thread(target=_loop, daemon=True, name="sokkan-heartbeat").start()
