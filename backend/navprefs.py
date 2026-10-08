#!/usr/bin/env python3
"""navprefs.py — 3.2.2: the cockpit plane a person was on last (Control, Build, Operate,
Setup), kept per user so an instance admin lands there again on any browser. A UI hint, not
a right: what a plane shows is still decided by the features and the roles.
Store: `$SOKKAN_DATA_DIR/navprefs.json` ({email: {"last_plane": …, "at": ts}})."""
from __future__ import annotations

import json
import os
import threading
import time

PLANES = ("control", "build", "operate", "setup")
_lock = threading.Lock()


def store_path() -> str:
    return os.environ.get("SOKKAN_NAVPREFS_STORE") or os.path.join(
        os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
        "navprefs.json")


def _load() -> dict:
    try:
        with open(store_path()) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def get(email: str) -> dict:
    rec = _load().get((email or "").lower()) or {}
    plane = rec.get("last_plane")
    return {"last_plane": plane if plane in PLANES else None}


def set_last(email: str, plane: str) -> dict:
    if plane not in PLANES:
        raise ValueError(f"plane: one of {', '.join(PLANES)}")
    with _lock:
        d = _load()
        d[(email or "").lower()] = {"last_plane": plane, "at": time.time()}
        p = store_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, p)
    return {"last_plane": plane}
