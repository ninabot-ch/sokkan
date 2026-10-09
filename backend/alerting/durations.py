"""Durations as people write them: "30s", "5m", "1h", "1d", "7d", "1w" (also plain seconds)."""
from __future__ import annotations

import re

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_RX = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.I)


class BadDuration(ValueError):
    pass


def seconds(v, default: float | None = None) -> float:
    """"5m" → 300.0 ; 90 → 90.0 ; "" / None → default (BadDuration when no default)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        if default is None:
            raise BadDuration("a duration is required (e.g. 5m)")
        return float(default)
    if isinstance(v, (int, float)):
        if v < 0:
            raise BadDuration("a duration cannot be negative")
        return float(v)
    m = _RX.match(str(v))
    if not m:
        raise BadDuration(f"not a duration: {v!r} (use 30s, 5m, 1h, 1d)")
    return float(m.group(1)) * _UNITS[(m.group(2) or "s").lower()]


def text(s: float) -> str:
    """300 → "5m" (largest exact unit), for sentences and PromQL ranges."""
    s = int(round(s))
    if s <= 0:
        return "0s"
    for u, n in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if s % n == 0:
            return f"{s // n}{u}"
    return f"{s}s"


def human(s: float) -> str:
    """300 → "5 minutes" — for the rule's sentence."""
    s = int(round(s))
    for u, n in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= n and s % n == 0:
            k = s // n
            return f"{k} {u}{'s' if k > 1 else ''}"
    return f"{s} second{'s' if s != 1 else ''}"
