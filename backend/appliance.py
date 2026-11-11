#!/usr/bin/env python3
"""appliance.py — SOKKAN on-prem appliance: the api side of the host agent.

The host (root, systemd) and the api container share ONE directory, the only channel
between them: ``$SOKKAN_APPLIANCE_DIR`` on the host, ``$SOKKAN_DATA_DIR/appliance`` in
the container.

  support-request.json   api  -> host   "open the support tunnel for N hours" / "close"
  support-state.json     host -> api    current state of the tunnel
  support-events.jsonl   host -> api    opened / closed / expired / ... journaled here in audit
  backup-status.json     host -> api    last backup result

The support tunnel is OFF by default, only an admin turns it on, for a bounded time; every
opening and closing ends up in the audit journal (actions ``support.tunnel.*``).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import audit
import features
import heartbeat

DATA_DIR = os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))
DIR = Path(os.environ.get("SOKKAN_APPLIANCE_DIR") or os.path.join(DATA_DIR, "appliance"))

_ingest_lock = threading.Lock()
_seen_ids: deque = deque(maxlen=500)  # last event ids journaled, also kept next to the offset


def max_hours() -> float:
    try:
        v = float(features.env_num("SOKKAN_SUPPORT_MAX_HOURS", 8.0, float))
    except (TypeError, ValueError):
        v = 8.0
    return v if v > 0 else 8.0


def _read_json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    try:
        tmp.write_text(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- support tunnel
def support_state() -> dict:
    """State written by the host agent. Missing / corrupt file = closed, no agent."""
    raw = _read_json(DIR / "support-state.json")
    st = {"open": False, "opened_at": None, "deadline": None, "by": "", "reason": "",
          "updated_at": None, "remaining_s": 0, "host_agent": raw is not None}
    if raw:
        for k in ("open", "opened_at", "deadline", "by", "reason", "updated_at"):
            if k in raw:
                st[k] = raw[k]
        st["open"] = st["open"] is True
    if st["open"] and isinstance(st["deadline"], (int, float)):
        st["remaining_s"] = max(0, int(st["deadline"] - time.time()))
    else:
        st["remaining_s"] = 0
    if st["open"] and st["remaining_s"] == 0 and isinstance(st["deadline"], (int, float)):
        st["open"] = False  # past the deadline: the host timer closes it within a minute
    return st


def request_support(action: str, hours: float, by: str) -> dict:
    """Write the request the host agent will apply. Raises ValueError on invalid input."""
    if action not in ("open", "close"):
        raise ValueError("action must be 'open' or 'close'")
    try:
        h = float(hours)
    except (TypeError, ValueError):
        raise ValueError("hours must be a number") from None
    if h != h:  # NaN
        raise ValueError("hours must be a number")
    mx = max_hours()
    if action == "open" and not (0 < h <= mx):
        raise ValueError(f"hours must be greater than 0 and at most {mx:g}")
    req = {"id": uuid.uuid4().hex, "action": action, "hours": h if action == "open" else 0,
           "by": by or "", "ts": time.time()}
    _atomic_write(DIR / "support-request.json", json.dumps(req))
    audit.log(by, "support.tunnel.request", action, f"hours={req['hours']:g}")
    return req


def _offset_path() -> Path:
    p = DIR / "support-events.offset"
    try:
        if os.access(DIR, os.W_OK):
            return p
    except OSError:
        pass
    return Path(DATA_DIR) / "appliance-events.offset"


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _detail(ev: dict) -> str:
    parts = []
    if ev.get("hours") is not None:
        parts.append(f"hours={ev['hours']:g}" if isinstance(ev["hours"], (int, float)) else f"hours={ev['hours']}")
    if ev.get("deadline") is not None:
        parts.append(f"deadline={_iso(ev['deadline'])}")
    if ev.get("reason"):
        parts.append(f"reason={ev['reason']}")
    return " ".join(parts)


def ingest_events() -> int:
    """Journal the new host events in audit. Idempotent, never raises. Returns how many."""
    n = 0
    try:
        with _ingest_lock:
            src = DIR / "support-events.jsonl"
            if not src.exists():
                return 0
            offp = _offset_path()
            off = 0
            try:
                saved = json.loads(offp.read_text())
                off = int(saved["offset"])
                _seen_ids.extend(i for i in saved.get("ids", []) if i not in _seen_ids)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            size = src.stat().st_size
            if off > size:  # file rotated / truncated
                off = 0
            with open(src, "rb") as f:
                f.seek(off)
                data = f.read()
            pos = 0
            while True:
                nl = data.find(b"\n", pos)
                if nl < 0:
                    break  # incomplete last line: do not advance past it
                line = data[pos:nl]
                pos = nl + 1
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(ev, dict) or not ev.get("event"):
                    continue
                eid = str(ev.get("id") or "")
                if eid and eid in _seen_ids:
                    continue
                audit.log(str(ev.get("by") or "") or "system", f"support.tunnel.{ev['event']}",
                          "tailscale", _detail(ev))
                if eid:
                    _seen_ids.append(eid)
                n += 1
            try:
                offp.parent.mkdir(parents=True, exist_ok=True)
                offp.write_text(json.dumps({"offset": off + pos, "ids": list(_seen_ids)}))
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001 — a journaling hiccup must never break the caller
        print(f"[appliance] ingest failed: {e}", file=sys.stderr)
    return n


# ----------------------------------------------------------------------------- backup
def backup_status() -> dict:
    out = {"last_success_ts": None, "last_attempt_ts": None, "last_error": "", "snapshot": "",
           "size_bytes": None, "age_s": None}
    try:
        raw = _read_json(DIR / "backup-status.json") or {}
        for k in ("last_success_ts", "last_attempt_ts", "last_error", "snapshot", "size_bytes"):
            if k in raw:
                out[k] = raw[k]
        ts = out["last_success_ts"]
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            out["age_s"] = max(0, int(time.time() - ts))
    except Exception:  # noqa: BLE001
        pass
    return out


# ----------------------------------------------------------------------------- routes
class SupportBody(BaseModel):
    action: str
    hours: float = 1


def router(require) -> APIRouter:
    r = APIRouter()

    @r.get("/api/appliance/status")
    def appliance_status(_u: dict = Depends(require("viewer"))) -> dict:
        ingest_events()
        return {"support": support_state(), "backup": backup_status(), "heartbeat": heartbeat.state()}

    @r.post("/api/appliance/support")
    def appliance_support(body: SupportBody, u: dict = Depends(require("admin"))) -> dict:
        if not (DIR / "support-state.json").exists():
            raise HTTPException(409, "support tunnel agent not installed on this host")
        try:
            req = request_support(body.action, body.hours, u.get("email", ""))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except OSError as e:
            raise HTTPException(500, f"cannot write the request: {e.strerror or e}") from None
        return {"ok": True, "request": req, "note": "applied by the host agent within a few seconds"}

    return r


def _loop() -> None:
    while True:
        ingest_events()
        time.sleep(30)


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="sokkan-appliance").start()
