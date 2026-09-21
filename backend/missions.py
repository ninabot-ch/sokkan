#!/usr/bin/env python3
"""missions.py — SOKKAN: the open-missions counter, fetched server-side.

The cockpit header can show how many SOKKAN Missions are currently open. That
counter used to be fetched by the browser, directly from app.sokkan.ch, which
handed each viewer's IP address to a third party just for displaying a number.

It is now fetched by this instance instead, at most once every six hours, and
served to the browser from `/api/missions/stats`. The outbound request carries
no identifier, no instance version and no referrer; a failure is silent and the
link simply does not appear.

Disable the feature entirely (link and fetch) with SOKKAN_FEATURE_MISSIONS_LINK=0.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request

URL = os.environ.get("SOKKAN_MISSIONS_STATS_URL",
                     "https://app.sokkan.ch/missions/stats.json")
ENABLED = os.environ.get("SOKKAN_FEATURE_MISSIONS_LINK", "1") != "0"
TTL_S = 6 * 3600

_lock = threading.Lock()
_cache: dict = {"open": None, "fetched_at": 0.0}


def _fetch() -> int | None:
    import json
    req = urllib.request.Request(URL, headers={"User-Agent": "sokkan"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read(64_000).decode("utf-8", "replace"))
    n = data.get("open")
    return n if isinstance(n, int) and n >= 0 else None


def stats() -> dict:
    """{"open": int|None}. Cached; never raises; None when unknown or disabled."""
    if not ENABLED:
        return {"open": None}
    now = time.time()
    with _lock:
        fresh = now - _cache["fetched_at"] < TTL_S
        if fresh:
            return {"open": _cache["open"]}
    try:
        n = _fetch()
    except Exception:  # noqa: BLE001 — offline / air-gapped: stay silent
        n = None
    with _lock:
        # keep a previously known value rather than blinking the link off
        if n is not None or _cache["open"] is None:
            _cache["open"] = n
        _cache["fetched_at"] = now
        return {"open": _cache["open"]}
