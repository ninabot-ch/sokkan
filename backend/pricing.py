#!/usr/bin/env python3
"""pricing.py — SOKKAN 3.2.3: the Claude API price table, versioned, never guessed.

The table lives in `backend/model_prices.json` (version = the date of the public price
list it copies). An operator can add or override models with `SOKKAN_CLAUDE_PRICES`
(path to a JSON file of the same shape: {"models": {id: {input, output, cache_read}}}).

Resolution of a model id, and nothing more:
  exact id → id without a context suffix (`[1m]`) → id without a date suffix
  (`claude-haiku-4-5-20251001` → `claude-haiku-4-5`) → id without a provider prefix
  (`anthropic.` / `us.anthropic.`) → alias (`opus`, `sonnet`, `haiku`, `fable`).
An id that matches none of these is UNPRICED: its tokens are counted, its cost is not
invented (3.2.2 priced every unknown Claude model at the legacy Opus 15/75 tariff).

Cost of one API message (USD) =
    input_tokens × input
  + cache_creation (5-minute TTL) × input × 1.25
  + cache_creation (1-hour TTL)   × input × 2
  + cache_read_input_tokens × cache_read
  + output_tokens × output
all per million tokens. When the transcript does not split the cache write by TTL, the
5-minute rate applies (the API default).
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

TABLE_PATH = Path(__file__).resolve().parent / "model_prices.json"
_DATE_SUFFIX = re.compile(r"-\d{8}$")
_PROVIDER_PREFIX = re.compile(r"^(?:[a-z]{2}\.)?anthropic\.")


@lru_cache(maxsize=1)
def _load(override: str) -> dict:
    base = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    if override:
        try:
            extra = json.loads(Path(override).read_text(encoding="utf-8"))
            base = {**base, "models": {**base["models"], **(extra.get("models") or {})},
                    "aliases": {**base.get("aliases", {}), **(extra.get("aliases") or {})},
                    "override": override}
        except (OSError, ValueError):
            pass
    return base


def table() -> dict:
    return _load((os.environ.get("SOKKAN_CLAUDE_PRICES") or "").strip())


def version() -> dict:
    t = table()
    return {"version": t["version"], "source": t["source"], "currency": t["currency"],
            "unit": t["unit"], "cache_write_multiplier": t["cache_write_multiplier"],
            "override": t.get("override")}


def canonical(model: str) -> str | None:
    """The table key `model` is priced under, or None."""
    t = table()
    models, aliases = t["models"], t.get("aliases", {})
    m = (model or "").strip().lower()
    for cand in (m, m.split("[", 1)[0]):
        if cand in models:
            return cand
        nodate = _DATE_SUFFIX.sub("", cand)
        if nodate in models:
            return nodate
        bare = _PROVIDER_PREFIX.sub("", nodate)
        bare = re.sub(r"-v\d+(?::\d+)?$", "", bare)   # bedrock `…-v1:0`
        bare = _DATE_SUFFIX.sub("", bare)
        if bare in models:
            return bare
        if cand in aliases and aliases[cand] in models:
            return aliases[cand]
    return None


def price(model: str) -> dict | None:
    key = canonical(model)
    if key is None:
        return None
    p = table()["models"][key]
    return {"model": key, "input": float(p["input"]), "output": float(p["output"]),
            "cache_read": float(p["cache_read"])}


def cost(model: str, usage: dict) -> dict | None:
    """USD cost of one API message, split by token kind; None if the model is unpriced."""
    p = price(model)
    if p is None:
        return None
    mult = table()["cache_write_multiplier"]
    i = int(usage.get("input_tokens") or 0)
    o = int(usage.get("output_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    det = usage.get("cache_creation") or {}
    c1h = int(det.get("ephemeral_1h_input_tokens") or 0)
    c5m = int(det.get("ephemeral_5m_input_tokens") or 0)
    if c1h + c5m == 0:
        c5m = cw
    out = {
        "input": i * p["input"] / 1e6,
        "output": o * p["output"] / 1e6,
        "cache_read": cr * p["cache_read"] / 1e6,
        "cache_write": (c5m * p["input"] * mult["5m"] + c1h * p["input"] * mult["1h"]) / 1e6,
    }
    out["total"] = sum(out.values())
    return out
