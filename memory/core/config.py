"""Environment configuration: ``CORTHEXIS_<NAME>``, then ``SOKKAN_<NAME>`` (compatibility)."""
from __future__ import annotations

import os

PREFIXES = ("CORTHEXIS_", "SOKKAN_")


def env(name: str, default: str | None = None) -> str | None:
    for prefix in PREFIXES:
        value = os.environ.get(prefix + name)
        if value not in (None, ""):
            return value
    return default


def env_int(name: str, default: int) -> int:
    raw = env(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_list(name: str, sep: str = ",") -> list[str]:
    raw = env(name) or ""
    return [x.strip() for x in raw.split(sep) if x.strip()]
