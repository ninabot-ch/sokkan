#!/usr/bin/env python3
"""agentcost.py — SOKKAN 3.1.2: what a run costs when the model is not Claude.

The Claude Code CLI prices a turn with its own table of Claude models. For any
other model id (SOKKAN Inference tiers such as `sokkan-ship`, a custom
Anthropic-compatible endpoint: Kimi, GLM, Mistral behind LiteLLM…) it falls back
to the price of its default Claude model — so `total_cost_usd`, and the SDK's
`max_budget_usd`, are computed at a Claude tariff that has nothing to do with
what the endpoint bills (checked on CLI 2.1.218: unknown model → the default
model's price, plus a `tengu_unknown_model_cost` event).

So, for a run that does not go to Anthropic with a Claude model, SOKKAN meters it
itself: tokens (from each assistant message's `usage`) × a price per model.

* Price table (per million tokens), first match wins:
  1. `$SOKKAN_MODEL_PRICES` (path to a JSON file), default
     `$SOKKAN_DATA_DIR/model-prices.json`:
     {"models": {"kimi-k2*": {"currency": "USD", "input": 0.6, "output": 2.5,
                               "cache_read": 0.15}}}
     Keys are model ids or globs; `currency` is "USD" or "CHF"; `cache_read` /
     `cache_write` default to the input price.
  2. In managed inference (`llm` mode `included`): the gateway's tier catalogue
     (`chf_per_mtok_in/out`, CHF).
* CHF prices are converted with `SOKKAN_FX_USD_PER_CHF` (default 1.30 — on the
  high side on purpose: a CHF-priced run never overspends a USD budget).
* Unknown price → the run is capped in TOKENS: `SOKKAN_AGENTS_MAX_TOKENS_PER_RUN`
  (default 5,000,000 — input + output + cache, never unlimited). The token cap
  also backs up a known price on a non-Claude model. The UI says which applies.

Runs on Claude through Anthropic keep the SDK's own figures and `max_budget_usd`.
"""
from __future__ import annotations

import fnmatch
import json
import os
import time
from pathlib import Path

import llm

CLAUDE_ALIASES = ("", "sonnet", "opus", "haiku", "default", "opusplan")
DEFAULT_MAX_TOKENS = 5_000_000
DEFAULT_FX = 1.30
_TIER_TTL_S = 600
_tier_cache: dict = {"at": 0.0, "tiers": []}


def max_tokens_per_run() -> int:
    raw = (os.environ.get("SOKKAN_AGENTS_MAX_TOKENS_PER_RUN") or "").strip()
    try:
        v = int(float(raw)) if raw else DEFAULT_MAX_TOKENS
    except ValueError:
        v = DEFAULT_MAX_TOKENS
    return v if v > 0 else DEFAULT_MAX_TOKENS  # 0 / negative would mean unlimited: no


def fx_usd_per_chf() -> float:
    raw = (os.environ.get("SOKKAN_FX_USD_PER_CHF") or "").strip()
    try:
        v = float(raw) if raw else DEFAULT_FX
    except ValueError:
        v = DEFAULT_FX
    return v if v > 0 else DEFAULT_FX


def _prices_file() -> Path:
    p = (os.environ.get("SOKKAN_MODEL_PRICES") or "").strip()
    if p:
        return Path(p)
    data = os.environ.get("SOKKAN_DATA_DIR") or os.path.expanduser("~/.local/share/sokkan")
    return Path(data) / "model-prices.json"


def _table() -> dict:
    try:
        d = json.loads(_prices_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    m = d.get("models") if isinstance(d, dict) else None
    return m if isinstance(m, dict) else {}


def _tiers() -> list[dict]:
    now = time.time()
    if now - _tier_cache["at"] > _TIER_TTL_S:
        try:
            _tier_cache["tiers"] = llm.price_tiers() or []
        except Exception:  # noqa: BLE001 — gateway down = price unknown, not a crash
            _tier_cache["tiers"] = []
        _tier_cache["at"] = now
    return _tier_cache["tiers"]


def _endpoint() -> str:
    """'anthropic' when the sessions talk to Anthropic, else 'gateway' / 'custom'."""
    c = llm.load() or {}
    mode = c.get("mode")
    if mode == "included":
        return "gateway"
    if mode == "custom":
        # the SOKKAN Router engine drives the gateway's Anthropic door (3.4.4)
        return "gateway" if c.get("engine") == "sokkan_router" else "custom"
    if mode == "byok":
        return "anthropic"
    base = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
    return "custom" if base and "anthropic.com" not in base else "anthropic"


def effective_model(agent_model: str | None) -> str:
    """Same resolution as agentchat: the agent's model, else SOKKAN_AGENT_MODEL, else
    the instance's included/custom model, else what the CLI would pick."""
    return (agent_model or os.environ.get("SOKKAN_AGENT_MODEL") or llm.session_model()
            or os.environ.get("ANTHROPIC_MODEL") or "").strip()


def _is_claude(model: str) -> bool:
    m = model.lower().split("[", 1)[0]
    return m in CLAUDE_ALIASES or m.startswith("claude")


def _price_for(model: str, endpoint: str) -> dict | None:
    # 3.4.4: the gateway answers `sokkan/<served tier>`; an escalated message is priced
    # at the tier that served it
    if model.startswith("sokkan/"):
        model = model.split("/", 1)[1]
    table = _table()
    entry = table.get(model)
    if entry is None:
        entry = next((v for k, v in table.items() if fnmatch.fnmatch(model, k)), None)
    if isinstance(entry, dict) and "input" in entry and "output" in entry:
        cur = str(entry.get("currency") or "USD").upper()
        if cur in ("USD", "CHF"):
            return {"currency": cur, "input": float(entry["input"]),
                    "output": float(entry["output"]),
                    "cache_read": float(entry.get("cache_read", entry["input"])),
                    "cache_write": float(entry.get("cache_write", entry["input"])),
                    "source": "price table"}
    if endpoint == "gateway":
        for t in _tiers():
            if t.get("id") == model and t.get("chf_per_mtok_in") is not None:
                pin = float(t["chf_per_mtok_in"])
                cached = t.get("chf_per_mtok_cached")
                return {"currency": "CHF", "input": pin,
                        "output": float(t.get("chf_per_mtok_out") or 0),
                        "cache_read": float(cached) if cached is not None else pin,
                        "cache_write": pin,
                        "source": "SOKKAN Inference tiers"}
    return None


def metering(agent_model: str | None) -> dict:
    """How a run of this agent is metered — shown in Settings, used by the run."""
    endpoint = _endpoint()
    model = effective_model(agent_model)
    if endpoint == "anthropic" and _is_claude(model):
        return {"basis": "sdk", "model": model or "default", "endpoint": endpoint,
                "price": None, "max_tokens_per_run": None,
                "note": "Claude on Anthropic: the SDK reports the cost and enforces the budget."}
    if endpoint == "gateway" and not model:
        model = llm.DEFAULT_INCLUDED_MODEL
    price = _price_for(model, endpoint) if model else None
    cap = max_tokens_per_run()
    if price:
        p = price
        note = (f"Non-Claude model ({model or 'endpoint default'}): SOKKAN computes the cost "
                f"from tokens × {p['currency']} {p['input']:g} in / {p['output']:g} out per M "
                f"tokens ({p['source']}); the run also stops at {cap:,} tokens.")
    else:
        note = (f"Price of {model or 'the endpoint default model'} unknown: the USD budget "
                f"cannot be checked, the run is capped at {cap:,} tokens instead. Add the "
                "model to the price table (docs/AGENTS.md § Budget).")
    return {"basis": "sokkan", "model": model or "", "endpoint": endpoint, "price": price,
            "max_tokens_per_run": cap, "fx_usd_per_chf": fx_usd_per_chf(), "note": note}


class Meter:
    """Running count of one run: tokens per API message (deduplicated — the CLI
    emits one assistant message per content block, each carrying the usage of the
    whole API message) and its cost from the price table."""

    def __init__(self, m: dict, budget_usd: float = 0.0):
        self.m = m
        self.budget_usd = float(budget_usd or 0)
        self._by_msg: dict[str, dict] = {}
        self._loose: list[dict] = []
        self.max_msg_usd = 0.0      # the most expensive single API message so far (3.4.3)

    @property
    def active(self) -> bool:
        return self.m.get("basis") == "sokkan"

    def add(self, usage: dict | None, message_id: str | None = None) -> None:
        if not isinstance(usage, dict):
            return
        u = {k: int(usage.get(k) or 0) for k in (
            "input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")}
        if message_id:
            prev = self._by_msg.get(message_id) or {}
            self._by_msg[message_id] = {k: max(v, prev.get(k, 0)) for k, v in u.items()}
            self.max_msg_usd = max(self.max_msg_usd, self._cost_of(self._by_msg[message_id]))
        else:
            self._loose.append(u)
            self.max_msg_usd = max(self.max_msg_usd, self._cost_of(u))

    @property
    def seen(self) -> bool:
        return bool(self._by_msg or self._loose)

    def _sum(self) -> dict:
        tot = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 0}
        for u in [*self._by_msg.values(), *self._loose]:
            for k in tot:
                tot[k] += u.get(k, 0)
        return tot

    @property
    def tokens(self) -> int:
        return sum(self._sum().values())

    def _cost_of(self, t: dict) -> float:
        p = self.m.get("price")
        if not p:
            return 0.0
        native = (t.get("input_tokens", 0) * p["input"] + t.get("output_tokens", 0) * p["output"]
                  + t.get("cache_read_input_tokens", 0) * p["cache_read"]
                  + t.get("cache_creation_input_tokens", 0) * p["cache_write"]) / 1e6
        return native * (fx_usd_per_chf() if p["currency"] == "CHF" else 1.0)

    @property
    def cost_usd(self) -> float:
        return self._cost_of(self._sum()) if self.m.get("price") else 0.0

    def over(self) -> str | None:
        """Why the run must stop now, or None."""
        if not self.active:
            return None
        cap = self.m.get("max_tokens_per_run") or max_tokens_per_run()
        if self.tokens >= cap:
            return (f"token cap reached ({self.tokens:,} ≥ {cap:,} tokens per run"
                    + ("" if self.m.get("price") else f"; price of {self.m.get('model') or 'the model'} unknown")
                    + ")")
        if self.m.get("price") and self.budget_usd and self.cost_usd >= self.budget_usd:
            return (f"run budget ${self.budget_usd:.2f} reached (${self.cost_usd:.4f} computed by "
                    f"SOKKAN from tokens × the {self.m['price']['currency']} price of "
                    f"{self.m.get('model')})")
        # 3.4.3: the cost of a message is known only once it exists, so a run used to end
        # well past its budget ($0.198 for a $0.10 cap, seen live). The context only grows:
        # the next call costs at least as much as the dearest one so far — stop while the
        # budget still covers it.
        if self.m.get("price") and self.budget_usd and self.max_msg_usd \
                and self.cost_usd + self.max_msg_usd > self.budget_usd:
            return (f"run budget ${self.budget_usd:.2f} would be exceeded by the next call "
                    f"(${self.cost_usd:.4f} so far, the dearest call cost ${self.max_msg_usd:.4f}; "
                    f"computed by SOKKAN from tokens × the {self.m['price']['currency']} price of "
                    f"{self.m.get('model')})")
        return None
