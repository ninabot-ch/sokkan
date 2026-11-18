"""models.py — the real engine: direct model calls through SOKKAN's inference tiers.

No tools, no session: a role gets its system prompt, the brief, the project's documents as
untrusted data, and what was said so far. Where each call goes:

  Variables (read by prefix below): SOKKAN_COUNCIL_SHIP_URL, SOKKAN_COUNCIL_SHIP_MODEL,
  SOKKAN_COUNCIL_SHIP_KEY, SOKKAN_COUNCIL_SHIP_KEY_FILE, SOKKAN_COUNCIL_SHIP_EXTRA and the same
  five with DEEP instead of SHIP: SOKKAN_COUNCIL_DEEP_URL, SOKKAN_COUNCIL_DEEP_MODEL,
  SOKKAN_COUNCIL_DEEP_KEY, SOKKAN_COUNCIL_DEEP_KEY_FILE, SOKKAN_COUNCIL_DEEP_EXTRA.

  ship    the machine's model. SOKKAN_COUNCIL_SHIP_URL / _MODEL / _KEY[_FILE], else the local
          reviewer of the quality gate (SOKKAN_REVIEW_LOCAL_*), else the router's own
          SOKKAN_SHIP_URL / _MODEL / _TOKEN (OpenAI-compatible /v1).
  deep    the stronger model (SOKKAN_COUNCIL_DEEP_*, else the quality gate's strong reviewer
          SOKKAN_REVIEW_*, else SOKKAN_DEEP_URL / _MODEL / _TOKEN). It leaves the machine, so the
          same ceiling as the review applies (`qualitygate.remote_max_level`, default « project »):
          a more confidential project is answered by the local model instead, said in the journal.
  claude  Anthropic, ONLY when the CEO chose it AND the project is not confidential: the rule D9
          of `claudetier` (HARD_MAX), checked here at EVERY call, and the PII sandwich of 4.0.1
          (`pii.PseudoMap`: personal data replaced by stable pseudonyms before the request leaves,
          put back in the answer). A refusal stops the council with a sentence — never a silent
          fall back to another model.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import engine as eng

CLAUDE_VERSION = "2023-06-01"
TIMEOUT_S = 180.0
# CHF per million tokens (input, output) of the non-Claude tiers: an ESTIMATE, configurable with
# SOKKAN_COUNCIL_PRICES='{"ship":[0.2,0.8],"deep":[1.0,4.0]}'. A local model costs the instance
# nothing per token; the figure only makes the budget ceiling meaningful.
DEFAULT_PRICES = {"ship": (0.2, 0.8), "deep": (1.0, 4.0)}


def prices() -> dict:
    out = dict(DEFAULT_PRICES)
    try:
        raw = json.loads(os.environ.get("SOKKAN_COUNCIL_PRICES") or "{}")
        for k, v in raw.items():
            out[k] = (float(v[0]), float(v[1]))
    except (ValueError, TypeError, IndexError):
        pass
    return out


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _key(var: str) -> str:
    v = _env(var)
    f = _env(var + "_FILE")
    if not v and f:
        try:
            v = Path_read(f)
        except OSError:
            v = ""
    return v


def Path_read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read().strip()


# A reasoning model spends max_tokens thinking and leaves `content` empty: the council asks for no
# reasoning BY DEFAULT on both tiers. SOKKAN_COUNCIL_SHIP_EXTRA / _DEEP_EXTRA adjust it (a JSON
# object merged over this; a key set to null is removed, e.g. {"reasoning_effort": null}).
DEFAULT_EXTRA = {"reasoning_effort": "none"}


def _extra(pre: str, base: dict | None = None) -> dict:
    out = {**DEFAULT_EXTRA, **(base or {})}
    try:
        mine = json.loads(_env(pre + "_EXTRA") or "{}")
        if isinstance(mine, dict):
            out.update(mine)
    except ValueError:
        pass
    return {k: v for k, v in out.items() if v is not None}


def endpoint(tier: str, level: int) -> dict | None:
    """{url, model, key, route, extra} for a tier, or None when none is configured. For `deep`
    a project above the review ceiling gets the local endpoint (route `ship`, `downgraded`)."""
    def ship() -> dict | None:
        for pre in ("SOKKAN_COUNCIL_SHIP", "SOKKAN_REVIEW_LOCAL"):
            if _env(pre + "_URL") and _env(pre + "_MODEL"):
                base = {"chat_template_kwargs": {"enable_thinking": False}} if pre == "SOKKAN_REVIEW_LOCAL" else {}
                return {"url": _env(pre + "_URL"), "model": _env(pre + "_MODEL"), "key": _key(pre + "_KEY"),
                        "route": "ship", "extra": _extra(pre, base)}
        if _env("SOKKAN_SHIP_URL"):
            return {"url": _env("SOKKAN_SHIP_URL").rstrip("/") + "/v1", "model": _env("SOKKAN_SHIP_MODEL"),
                    "key": _env("SOKKAN_SHIP_TOKEN"), "route": "ship",
                    "extra": _extra("SOKKAN_COUNCIL_SHIP", {"chat_template_kwargs": {"enable_thinking": False}})}
        return None

    def deep() -> dict | None:
        for pre in ("SOKKAN_COUNCIL_DEEP", "SOKKAN_REVIEW"):
            if _env(pre + "_URL") and _env(pre + "_MODEL"):
                return {"url": _env(pre + "_URL"), "model": _env(pre + "_MODEL"), "key": _key(pre + "_KEY"),
                        "route": "deep", "extra": _extra("SOKKAN_COUNCIL_DEEP" if pre == "SOKKAN_COUNCIL_DEEP" else pre)}
        if _env("SOKKAN_DEEP_URL") and _env("SOKKAN_DEEP_TOKEN"):
            return {"url": _env("SOKKAN_DEEP_URL").rstrip("/") + "/v1", "model": _env("SOKKAN_DEEP_MODEL") or "sokkan-deep",
                    "key": _env("SOKKAN_DEEP_TOKEN"), "route": "deep",
                    "extra": _extra("SOKKAN_COUNCIL_DEEP")}
        return None

    if tier == "deep":
        import qualitygate
        if level > qualitygate.remote_max_level():
            e = ship()
            return {**e, "downgraded": True} if e else None
        return deep() or (ship() and {**ship(), "downgraded": True})
    return ship()


def claude_gate(project: str, model: str = "", lang: str = "fr") -> dict:
    """May the council use Claude for this project? {allowed, code, reason, model}. The same
    rule as « Write with Claude » (backend/claudetier.py): the project's classification decides,
    `HARD_MAX` (confidential and restricted never) cannot be raised by any variable."""
    import claudetier
    fr = lang == "fr"
    lvl, cap = claudetier.project_level(project), claudetier.max_level()
    out = {"allowed": False, "code": "", "reason": "", "model": ""}
    if not claudetier.enabled():
        out.update(code="off", reason="Claude n'est pas proposé sur cette instance." if fr
                   else "Claude is not offered on this instance.")
    elif lvl > cap:
        out.update(code="level", reason=(
            f"Ce projet est classé « {claudetier.level_label(lvl, lang)} » : le Conseil reste sur le modèle de la machine."
            if fr else f"This project is classified “{claudetier.level_label(lvl, lang)}”: the council stays on the machine's model."))
    elif not claudetier.key_present():
        out.update(code="no_key", reason="Aucune clé Anthropic n'est saisie : l'administrateur la saisit dans Réglages."
                   if fr else "No Anthropic key: the administrator enters it in Settings.")
    else:
        m = model or claudetier.default_model()
        if m not in claudetier.available_models():
            out.update(code="model", reason=f"Modèle non proposé : {m}." if fr else f"Model not offered: {m}.")
        else:
            out.update(allowed=True, model=m)
    return out


def cost_chf(route: str, model: str, tin: int, tout: int) -> float:
    if route == "claude":
        try:
            import agentcost
            p = agentcost._claude_price(model)
            if p:
                usd = (tin * p["input"] + tout * p["output"]) / 1e6
                return round(usd / agentcost.fx_usd_per_chf(), 6)
        except Exception:  # noqa: BLE001
            pass
        a, b = (3.0, 15.0)          # unknown Claude model: a prudent estimate, never zero
        return round((tin * a + tout * b) / 1e6, 6)
    a, b = prices().get(route, (0.0, 0.0))
    return round((tin * a + tout * b) / 1e6, 6)


def _post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    last, status = "", 0
    for wait in (2, 6, 0):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 — configured endpoint
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last, status = f"HTTP {e.code}", e.code
            if e.code in (429, 502, 503) and wait:
                time.sleep(wait)
                continue
            break
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = f"{type(e).__name__}"
            break
    err = eng.EngineError(f"model call failed ({last})", "model")
    err.status = status
    raise err


class ModelEngine:
    def complete(self, call: eng.Call) -> eng.Reply:
        t0 = time.monotonic()
        if call.tier == "claude":
            return self._claude(call, t0)
        ep = endpoint(call.tier, call.level)
        if not ep:
            raise eng.EngineError(f"no model configured for the tier {call.tier}", "no_model")
        body = {"model": ep["model"], "temperature": 0.3, "max_tokens": call.max_tokens,
                "messages": [{"role": "system", "content": call.system}, {"role": "user", "content": call.user}],
                **(ep.get("extra") or {})}
        if call.retry:           # second attempt: no reasoning, and room if the model thinks anyway
            body["reasoning_effort"] = "none"
            body["max_tokens"] = min(call.max_tokens * 2, 16000)
        url, hdr = ep["url"].rstrip("/") + "/chat/completions", ({"Authorization": f"Bearer {ep['key']}"} if ep.get("key") else {})
        try:
            d = _post(url, hdr, body)
        except eng.EngineError as e:
            # a tier that does not take `reasoning_effort` answers 400: ask again without it
            if getattr(e, "status", 0) == 400 and "reasoning_effort" in body:
                body.pop("reasoning_effort")
                d = _post(url, hdr, body)
            else:
                raise
        u = d.get("usage") or {}
        choice = (d.get("choices") or [{}])[0]
        msg = (choice.get("message") or {})
        tin, tout = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        route = ep["route"]
        return eng.Reply(text=msg.get("content") or "", tokens_in=tin, tokens_out=tout,
                         ms=int((time.monotonic() - t0) * 1000), model=str(d.get("model") or ep["model"]),
                         route=route + ("*" if ep.get("downgraded") else ""),
                         cost_chf=cost_chf(route, ep["model"], tin, tout), finish=str(choice.get("finish_reason") or ""))

    def _claude(self, call: eng.Call, t0: float) -> eng.Reply:
        g = claude_gate(call.project, call.claude_model)
        if not g["allowed"]:
            raise eng.EngineError(g["reason"] or "Claude refused", "claude_refused")
        import modelkeys
        import pii
        key = modelkeys.get_plain("instance:anthropic") or ""
        if not key:
            raise eng.EngineError("no Anthropic key", "claude_refused")
        pmap = call.pii if call.pii is not None else pii.PseudoMap()
        system, _ = pmap.anonymize(call.system, call.who)
        user, _ = pmap.anonymize(call.user, call.who)
        url = (_env("SOKKAN_CLAUDE_URL") or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        d = _post(url, {"x-api-key": key, "anthropic-version": CLAUDE_VERSION},
                  {"model": g["model"], "max_tokens": min(call.max_tokens * (2 if call.retry else 1), 16000), "system": system,
                   "messages": [{"role": "user", "content": user}]})
        text = "".join(b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text")
        u = d.get("usage") or {}
        tin, tout = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        return eng.Reply(text=pmap.restore(text), tokens_in=tin, tokens_out=tout,
                         ms=int((time.monotonic() - t0) * 1000), model=str(d.get("model") or g["model"]),
                         route="claude", cost_chf=cost_chf("claude", g["model"], tin, tout),
                         finish="length" if d.get("stop_reason") == "max_tokens" else "")


_default: eng.Engine | None = None


def default() -> eng.Engine:
    """The engine the service uses (tests replace it with `set_default`)."""
    return _default or ModelEngine()


def set_default(e: eng.Engine | None) -> None:
    global _default
    _default = e
