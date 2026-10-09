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
        out = {"basis": "sdk", "model": model or "default", "endpoint": endpoint,
               "price": None, "max_tokens_per_run": None,
               "note": "Claude on Anthropic: the SDK reports the cost and enforces the budget."}
        out["first_call_usd"] = first_call_estimate(out)
        return out
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
    out = {"basis": "sokkan", "model": model or "", "endpoint": endpoint, "price": price,
           "max_tokens_per_run": cap, "fx_usd_per_chf": fx_usd_per_chf(), "note": note}
    out["first_call_usd"] = first_call_estimate(out)
    return out


DEFAULT_FIRST_CALL_TOKENS = 20_000


def first_call_tokens() -> int:
    """What the first API call of a run writes to the prompt cache at least: the CLI's
    system prompt, the tool and MCP definitions, the agent's brief. Measured on CLI 2.1.29x:
    16k (haiku, 3 tools) to 40k tokens (MCP servers). `SOKKAN_AGENTS_FIRST_CALL_TOKENS`."""
    raw = (os.environ.get("SOKKAN_AGENTS_FIRST_CALL_TOKENS") or "").strip()
    try:
        v = int(float(raw)) if raw else DEFAULT_FIRST_CALL_TOKENS
    except ValueError:
        v = DEFAULT_FIRST_CALL_TOKENS
    return v if v > 0 else DEFAULT_FIRST_CALL_TOKENS


def _first_call_file() -> Path:
    data = os.environ.get("SOKKAN_DATA_DIR") or os.path.expanduser("~/.local/share/sokkan")
    return Path(data) / "agent-first-call.json"


def _model_key(m: dict) -> str:
    return (m.get("model") or "").strip() or "default"


def observed_first_call(m: dict) -> float | None:
    """The cost of the first call of the last run on this model (USD), if one was seen."""
    try:
        v = json.loads(_first_call_file().read_text(encoding="utf-8")).get(_model_key(m))
        return float(v) if v else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def record_first_call(m: dict, usd: float) -> None:
    if not usd or usd <= 0:
        return
    f = _first_call_file()
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    d[_model_key(m)] = round(float(usd), 6)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(f)
    except OSError:
        pass


def _claude_price(model: str) -> dict | None:
    """A Claude model's price as an agentcost price (USD, cache write 5 min = input x 1.25)."""
    try:
        import pricing
        p = pricing.price(model or "")
        mult = float(pricing.table()["cache_write_multiplier"]["5m"])
    except Exception:  # noqa: BLE001 — an unreadable table = no estimate, never a crash
        return None
    if not p:
        return None
    return {"currency": "USD", "input": p["input"], "output": p["output"],
            "cache_read": p["cache_read"], "cache_write": p["input"] * mult,
            "source": "Claude price table"}


def _dearest_claude_price() -> dict | None:
    """The most expensive Claude model of the table — the prudent price of a run whose
    model is the CLI's own default (`""`): which one it picks is not known here (3.4.5)."""
    try:
        import pricing
        models = pricing.table()["models"]
    except Exception:  # noqa: BLE001
        return None
    best = max(models, key=lambda k: float(models[k].get("input") or 0), default=None)
    return _claude_price(best) if best else None


def first_call_estimate(m: dict) -> float | None:
    """The least a run's first API call costs (USD): its prompt written to the cache, at
    the model's price — or what the last run on this model measured, if more. None = no
    price and nothing measured yet (the per-call guard of the Meter still applies).
    A Claude run on the CLI's default model (no model set: 3.4.4 had no estimate and let a
    $0.10 run spend $0.1963 on its first call) is estimated at the dearest Claude model."""
    if m.get("basis") == "sokkan":
        p = m.get("price")
    else:
        model = (m.get("model") or "").strip()
        p = _claude_price(model) if model and model != "default" else None
        if p is None:
            p = _dearest_claude_price()
    floor = None
    if p:
        floor = first_call_tokens() * p["cache_write"] / 1e6
        floor *= fx_usd_per_chf() if p.get("currency") == "CHF" else 1.0
    seen = observed_first_call(m)
    vals = [v for v in (floor, seen) if v]
    return max(vals) if vals else None


def usd(v: float) -> str:
    """A budget as people set it: $0.005 stays $0.005 (not "$0.01"), $0.1 → $0.10."""
    v = float(v or 0)
    return f"${v:.2f}" if round(v, 2) == v else f"${v:.4f}".rstrip("0")


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
        self.first_msg_usd = 0.0    # the run's first API message (3.4.4: calibrates the next run)
        self._first_id: str | None = None
        self._sdk_by_msg: dict[str, float] = {}   # Claude on Anthropic: per-message USD

    @property
    def guarding(self) -> bool:
        """3.4.4: a Claude run priced by the SDK — the SDK's `max_budget_usd` only acts once a
        turn is over its budget, so SOKKAN also prices each message and stops before the next
        call would cross it."""
        return self.m.get("basis") == "sdk" and self.budget_usd > 0

    def add_sdk(self, usage: dict | None, message_id: str | None, model: str | None) -> None:
        if not isinstance(usage, dict):
            return
        try:
            import pricing
            c = pricing.cost(model or self.m.get("model") or "", usage)
        except Exception:  # noqa: BLE001
            c = None
        if not c:
            return
        key = message_id or f"_loose{len(self._sdk_by_msg)}"
        self._sdk_by_msg[key] = max(self._sdk_by_msg.get(key, 0.0), c["total"])
        self.max_msg_usd = max(self.max_msg_usd, self._sdk_by_msg[key])
        self._note_first(key, self._sdk_by_msg[key])

    def _note_first(self, key: str, cost: float) -> None:
        if self._first_id is None:
            self._first_id = key
        if key == self._first_id:
            self.first_msg_usd = max(self.first_msg_usd, cost)

    @property
    def sdk_cost_usd(self) -> float:
        return sum(self._sdk_by_msg.values())

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
            c = self._cost_of(self._by_msg[message_id])
            self.max_msg_usd = max(self.max_msg_usd, c)
            self._note_first(message_id, c)
        else:
            self._loose.append(u)
            c = self._cost_of(u)
            self.max_msg_usd = max(self.max_msg_usd, c)
            self._note_first(f"_loose{len(self._loose)}", c)

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
        if self.guarding:
            spent = self.sdk_cost_usd
            if self.max_msg_usd and spent + self.max_msg_usd > self.budget_usd:
                return (f"run budget {usd(self.budget_usd)} would be exceeded by the next call "
                        f"(${spent:.4f} so far, the dearest call cost ${self.max_msg_usd:.4f}; "
                        f"Claude price table, {self.m.get('model') or 'default model'})")
            return None
        if not self.active:
            return None
        cap = self.m.get("max_tokens_per_run") or max_tokens_per_run()
        if self.tokens >= cap:
            return (f"token cap reached ({self.tokens:,} ≥ {cap:,} tokens per run"
                    + ("" if self.m.get("price") else f"; price of {self.m.get('model') or 'the model'} unknown")
                    + ")")
        if self.m.get("price") and self.budget_usd and self.cost_usd >= self.budget_usd:
            return (f"run budget {usd(self.budget_usd)} reached (${self.cost_usd:.4f} computed by "
                    f"SOKKAN from tokens × the {self.m['price']['currency']} price of "
                    f"{self.m.get('model')})")
        # 3.4.3: the cost of a message is known only once it exists, so a run used to end
        # well past its budget ($0.198 for a $0.10 cap, seen live). The context only grows:
        # the next call costs at least as much as the dearest one so far — stop while the
        # budget still covers it.
        if self.m.get("price") and self.budget_usd and self.max_msg_usd \
                and self.cost_usd + self.max_msg_usd > self.budget_usd:
            return (f"run budget {usd(self.budget_usd)} would be exceeded by the next call "
                    f"(${self.cost_usd:.4f} so far, the dearest call cost ${self.max_msg_usd:.4f}; "
                    f"computed by SOKKAN from tokens × the {self.m['price']['currency']} price of "
                    f"{self.m.get('model')})")
        return None
