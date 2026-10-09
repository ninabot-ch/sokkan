#!/usr/bin/env python3
"""connectai.py — SOKKAN `connect_ai`: « Connect your AI ».

One screen (Setup › Engines, with the instance model keys) where the engines a SOKKAN instance can drive are
cards: Claude (login or key), OpenAI / Codex, Gemini, OpenRouter, SOKKAN Router,
Ollama / local, Magnitude (your GPUs). Two modes:

* **personal** (community default): connect whatever you like; SOKKAN Router is the
  preselected card, with a « welcome credit » link when SOKKAN_ROUTER_WELCOME_URL is set
  (no amount in the code: the offer lives behind the link);
* **governed** (enterprise default; SOKKAN_CONNECT_AI_MODE forces either): the instance
  admin sets the allowed engines, the allowed zones and the SOKKAN tiers; people only see
  the allowed engines; a project maintainer can pick the project's engine when the
  policy allows a choice per project.

Sessions always run the Claude Code engine. An engine drives them through the Anthropic
Messages API: natively (Claude), or through an endpoint that speaks it (base URL — the
provider's own Anthropic-compatible door, or a proxy such as LiteLLM in front of OpenAI,
Gemini, Ollama, a Magnitude node). Keys are stored encrypted by `modelkeys` (never in
connect_ai.json, never served back).

* default engine → written to llm.json as a reference (`key_ref`), so the existing
  session mechanism (`llm.session_env`) applies it;
* an agent (Crew card) whose model is ``engine:<id>`` runs on that engine;
* a session of a project with a chosen engine runs on it (governed, per project).
"""
from __future__ import annotations

import json
import os
import threading
import time

import modelkeys

MODES = ("personal", "governed")
ZONES = ("CH", "EU", "US", "local")
LOGIN_NOTE = ("Check your provider's terms before using a personal subscription with "
              "SOKKAN — SOKKAN makes no promise about what a consumer plan allows.")
_lock = threading.Lock()

# bridge: native = Anthropic itself; anthropic = needs a base URL speaking the Anthropic
# Messages API (the provider's own door or a proxy); the UI says which.
ENGINES: tuple[dict, ...] = (
    {"id": "sokkan_router", "label": "SOKKAN Router", "vendor": "NINABOT", "avatar": "SR",
     "auths": ["key"], "bridge": "anthropic", "provider": "sokkan_router",
     # 3.4.4: sessions speak the Anthropic Messages API, so the card points at the
     # Anthropic door of SOKKAN Inference (Ship → Deep by difficulty). router.sokkan.ch
     # only speaks the OpenAI API (/v1/messages = 404): every session failed on it.
     "base_url_env": "SOKKAN_ROUTER_URL", "base_url": "https://infer.sokkan.ch",
     "model_env": "SOKKAN_ROUTER_MODEL", "model": "sokkan-ship",
     "blurb": "Routes each request to the right model by difficulty; Swiss/EU zones.",
     "recommended": True},
    {"id": "claude", "label": "Claude", "vendor": "Anthropic", "avatar": "C",
     "auths": ["key", "login"], "bridge": "native", "provider": "anthropic",
     "blurb": "The reference engine of Claude Code: an API key, or a login token "
              "(`claude setup-token`)."},
    {"id": "openai", "label": "OpenAI / Codex", "vendor": "OpenAI", "avatar": "O",
     "auths": ["key"], "bridge": "anthropic", "provider": "openai", "base_url": "",
     "blurb": "GPT / Codex models through an Anthropic-compatible proxy (e.g. LiteLLM)."},
    {"id": "gemini", "label": "Gemini", "vendor": "Google", "avatar": "G",
     "auths": ["key"], "bridge": "anthropic", "provider": "gemini", "base_url": "",
     "blurb": "Gemini models through an Anthropic-compatible proxy (e.g. LiteLLM)."},
    {"id": "openrouter", "label": "OpenRouter", "vendor": "OpenRouter", "avatar": "OR",
     "auths": ["key"], "bridge": "anthropic", "provider": "openrouter", "base_url": "",
     "blurb": "Many models behind one key; give the Anthropic-compatible base URL."},
    {"id": "ollama", "label": "Ollama / local", "vendor": "your machine", "avatar": "L",
     "auths": ["none", "key"], "bridge": "anthropic", "provider": "custom",
     "base_url": "http://host.docker.internal:11434", "zone": "local",
     "blurb": "A model on your own machine; the endpoint must speak the Anthropic API "
              "(recent Ollama, or a LiteLLM proxy)."},
    {"id": "magnitude", "label": "Magnitude", "vendor": "your GPUs", "avatar": "M",
     "auths": ["none"], "bridge": "anthropic", "provider": "custom", "base_url": "",
     "zone": "local",
     "blurb": "Your GPUs, served by Magnitude (Setup › Magnitude): paste the node's endpoint."},
)
BY_ID = {e["id"]: e for e in ENGINES}


class ConnectError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


# ---- store ----------------------------------------------------------------------------

def store_path() -> str:
    return os.environ.get("SOKKAN_CONNECT_AI_STORE") or os.path.join(
        os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")),
        "connect_ai.json")


def _load() -> dict:
    try:
        with open(store_path()) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    p = store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def mode() -> str:
    m = (os.environ.get("SOKKAN_CONNECT_AI_MODE") or "").strip().lower()
    if m in MODES:
        return m
    import features
    return "governed" if features.edition() == "enterprise" else "personal"


def welcome_url() -> str:
    u = (os.environ.get("SOKKAN_ROUTER_WELCOME_URL") or "").strip()
    return u if u.startswith("https://") else ""


def default_base_url(e: dict) -> str:
    env = e.get("base_url_env")
    return ((os.environ.get(env) or "").strip() if env else "") or e.get("base_url", "")


def default_model(e: dict) -> str:
    env = e.get("model_env")
    return ((os.environ.get(env) or "").strip() if env else "") or e.get("model", "")


def policy() -> dict:
    p = _load().get("policy") or {}
    return {"allowed": [x for x in p.get("allowed", []) if x in BY_ID],
            "zones": {k: v for k, v in (p.get("zones") or {}).items() if k in BY_ID and v in ZONES},
            "allowed_zones": [z for z in p.get("allowed_zones", []) if z in ZONES],
            "tiers": [str(t) for t in p.get("tiers", [])][:20],
            "per_project": bool(p.get("per_project"))}


def zone_of(eid: str) -> str:
    return policy()["zones"].get(eid) or BY_ID[eid].get("zone", "")


def allowed(eid: str) -> bool:
    """Personal mode: every engine. Governed: on the admin's list AND in an allowed zone
    (when zones are restricted, an engine without a declared zone is not allowed)."""
    if eid not in BY_ID:
        return False
    if mode() != "governed":
        return True
    p = policy()
    if eid not in p["allowed"]:
        return False
    if p["allowed_zones"]:
        return zone_of(eid) in p["allowed_zones"]
    return True


def connection(eid: str) -> dict | None:
    return (_load().get("connections") or {}).get(eid)


def connected(eid: str) -> bool:
    c = connection(eid)
    if not c:
        return False
    if c.get("auth") in ("key", "login"):
        return modelkeys.get_plain(c.get("key_ref", "")) is not None
    return bool(c.get("base_url"))


def _provider_for(eid: str, auth: str) -> str:
    return "claude_login" if (eid == "claude" and auth == "login") else BY_ID[eid]["provider"]


# ---- admin: connect / disconnect / default / policy ------------------------------------

def connect(eid: str, by: str, auth: str, key: str = "", base_url: str = "", model: str = "",
            small_model: str = "") -> dict:
    e = BY_ID.get(eid)
    if e is None:
        raise ConnectError(404, "unknown engine")
    if not allowed(eid):
        raise ConnectError(403, "this engine is not allowed on this instance (governed mode)")
    if auth not in e["auths"]:
        raise ConnectError(400, f"auth: one of {', '.join(e['auths'])}")
    base_url = (base_url or default_base_url(e)).strip().rstrip("/")
    if e["bridge"] == "anthropic":
        if not base_url.startswith(("http://", "https://")):
            raise ConnectError(400, "base_url (an endpoint speaking the Anthropic API) required")
    else:
        base_url = ""
    model = ((model or "").strip() or default_model(e))[:120]
    small_model = (small_model or "").strip()[:120]
    p = policy()
    if eid == "sokkan_router" and mode() == "governed" and p["tiers"] and model not in p["tiers"]:
        raise ConnectError(400, f"model: one of the allowed tiers ({', '.join(p['tiers'])})")
    rec = {"auth": auth, "base_url": base_url, "model": model, "small_model": small_model,
           "connected_by": by, "connected_at": time.time()}
    if auth in ("key", "login"):
        prov = _provider_for(eid, auth)
        if key.strip():
            modelkeys.set_key("instance", prov, key, by, base_url=base_url)
        elif modelkeys.record("instance", prov) is None:
            raise ConnectError(400, "key required")
        rec["key_ref"] = modelkeys.ref("instance", prov)
    with _lock:
        d = _load()
        d.setdefault("connections", {})[eid] = rec
        _save(d)
    if (_load().get("default") == eid):
        apply_default(eid)              # keep llm.json in line with the new settings
    return rec


def disconnect(eid: str) -> None:
    with _lock:
        d = _load()
        rec = (d.get("connections") or {}).pop(eid, None)
        if d.get("default") == eid:
            d["default"] = None
        for slug, x in list((d.get("project_engines") or {}).items()):
            if x == eid:
                d["project_engines"].pop(slug)
        _save(d)
    if rec and rec.get("key_ref"):
        scope, _, prov = rec["key_ref"].rpartition(":")
        # the key may serve the Model keys screen too: only drop it if no engine uses it
        users = [c for c in (_load().get("connections") or {}).values()
                 if c.get("key_ref") == rec["key_ref"]]
        if not users and prov == "claude_login":
            modelkeys.delete_key(scope, prov)


def llm_config_for(eid: str) -> dict:
    """The llm.json content that makes `eid` the sessions' engine (references, no key)."""
    c = connection(eid) or {}
    if eid == "claude":
        field = "claude_oauth_token" if c.get("auth") == "login" else "anthropic_api_key"
        return {"mode": "byok", "key_ref": c["key_ref"], "key_field": field, "engine": eid}
    cfg = {"mode": "custom", "base_url": c["base_url"], "model": c.get("model") or "",
           "small_model": c.get("small_model") or "", "engine": eid}
    if c.get("key_ref"):
        cfg.update({"key_ref": c["key_ref"], "key_field": "auth_token"})
    else:
        cfg["auth_token"] = "none"      # local endpoints without auth still need a value
    return cfg


def apply_default(eid: str) -> None:
    import llm
    llm.save(llm_config_for(eid))


def set_default(eid: str) -> None:
    if eid not in BY_ID or not connected(eid):
        raise ConnectError(400, "connect the engine first")
    if not allowed(eid):
        raise ConnectError(403, "this engine is not allowed on this instance (governed mode)")
    c = connection(eid) or {}
    if eid != "claude" and not c.get("model"):
        raise ConnectError(400, "set the model this engine should use first")
    apply_default(eid)
    with _lock:
        d = _load()
        d["default"] = eid
        _save(d)


def set_policy(allowed_ids: list[str], zones: dict, allowed_zones: list[str], tiers: list[str],
               per_project: bool) -> dict:
    bad = [x for x in allowed_ids if x not in BY_ID]
    if bad:
        raise ConnectError(400, f"unknown engines: {', '.join(bad)}")
    badz = [z for z in list(zones.values()) + list(allowed_zones) if z not in ZONES]
    if badz:
        raise ConnectError(400, f"zones: {', '.join(ZONES)}")
    with _lock:
        d = _load()
        d["policy"] = {"allowed": list(dict.fromkeys(allowed_ids)),
                       "zones": {k: v for k, v in zones.items() if k in BY_ID},
                       "allowed_zones": list(dict.fromkeys(allowed_zones)),
                       "tiers": [t.strip() for t in tiers if t.strip()][:20],
                       "per_project": bool(per_project)}
        _save(d)
    return policy()


def project_engine(slug: str | None) -> str | None:
    if not slug:
        return None
    eid = (_load().get("project_engines") or {}).get(slug)
    return eid if eid and allowed(eid) and connected(eid) else None


def set_project_engine(slug: str, eid: str | None) -> None:
    if mode() != "governed" or not policy()["per_project"]:
        raise ConnectError(400, "the engine is chosen per project only in governed mode, "
                                "when the policy allows it")
    if eid and (not allowed(eid) or not connected(eid)):
        raise ConnectError(400, "pick an allowed, connected engine")
    with _lock:
        d = _load()
        pe = d.setdefault("project_engines", {})
        if eid:
            pe[slug] = eid
        else:
            pe.pop(slug, None)
        _save(d)


# ---- sessions and Crew ------------------------------------------------------------------

def engine_env(eid: str) -> tuple[dict, str]:
    """(env, model) for a session on `eid` — same variables as `llm.session_env`."""
    import llm
    cfg = llm.resolve_refs(llm_config_for(eid))
    if cfg["mode"] == "byok":
        if cfg.get("claude_oauth_token"):
            return ({"CLAUDE_CODE_OAUTH_TOKEN": cfg["claude_oauth_token"], "ANTHROPIC_API_KEY": "",
                     "ANTHROPIC_BASE_URL": "", "ANTHROPIC_AUTH_TOKEN": ""}, "")
        return ({"ANTHROPIC_API_KEY": cfg.get("anthropic_api_key") or "", "ANTHROPIC_BASE_URL": "",
                 "ANTHROPIC_AUTH_TOKEN": "", "CLAUDE_CODE_OAUTH_TOKEN": ""}, "")
    tok = cfg.get("auth_token") or "none"
    env = {"ANTHROPIC_BASE_URL": cfg["base_url"], "ANTHROPIC_AUTH_TOKEN": tok,
           "ANTHROPIC_API_KEY": tok, "CLAUDE_CODE_OAUTH_TOKEN": ""}
    small = cfg.get("small_model") or cfg.get("model")
    if small:
        env["ANTHROPIC_SMALL_FAST_MODEL"] = small
    return env, cfg.get("model") or ""


def crew_engines() -> list[dict]:
    """Engines a Crew card may pick (model value ``engine:<id>``)."""
    import features
    if not features.enabled("connect_ai"):
        return []
    out = []
    for e in ENGINES:
        if allowed(e["id"]) and connected(e["id"]):
            c = connection(e["id"]) or {}
            out.append({"value": f"engine:{e['id']}", "label": e["label"],
                        "model": c.get("model") or ""})
    return out


def session_overrides(session_id: str, model: str | None) -> tuple[dict, str | None]:
    """Engine of a session: a Crew card's ``engine:<id>``, else the session's project engine
    (governed, per project). Returns (env overrides, model) — model None = unchanged.
    An engine that is gone / not allowed falls back to the instance default (never to a
    stale key)."""
    import features
    if not features.enabled("connect_ai"):
        if model and model.startswith("engine:"):
            import llm
            return {}, llm.session_model() or ""
        return {}, None
    eid = None
    if model and model.startswith("engine:"):
        eid = model.split(":", 1)[1]
        if not (allowed(eid) and connected(eid)):
            import llm
            return {}, llm.session_model() or ""
    elif not model:
        try:
            import board
            eid = project_engine(board.get_session_project(session_id))
        except Exception:  # noqa: BLE001
            eid = None
    if not eid:
        return {}, None
    env, m = engine_env(eid)
    return env, m or ""


def engine_providers(eid: str) -> list[str]:
    """The model-key providers an engine's key lives under (Claude: API key or login)."""
    e = BY_ID[eid]
    return ["anthropic", "claude_login"] if eid == "claude" else [e["provider"]]


def engine_keys(eid: str) -> list[dict]:
    """The instance keys of this engine's provider(s), masked (…last4, by, when, test)."""
    out = []
    for prov in engine_providers(eid):
        rec = modelkeys.record("instance", prov)
        if rec:
            k = modelkeys.public("instance", prov, rec)
            k["testable"] = modelkeys.testable(prov, rec)
            out.append(k)
    return out


# ---- the screen ---------------------------------------------------------------------------

def _demo() -> bool:
    import demo_captains
    return demo_captains.enabled()


def view(user: dict, project: str | None, is_admin: bool, project_role: str | None) -> dict:
    import llm
    m = mode()
    d = _load()
    p = policy()
    engines = []
    for e in ENGINES:
        ok = allowed(e["id"])
        if not ok and not (is_admin and m == "governed"):
            continue                      # people only see what the admin allowed
        c = connection(e["id"])
        conn = None
        if c:
            k = modelkeys.record(*c["key_ref"].rsplit(":", 1)) if c.get("key_ref") else None
            # 3.2.2 Captains demo: a non-admin sees that an engine is connected, never by whom
            # nor where (base URL), and never a key tail
            hide = not is_admin and _demo()
            conn = {"auth": c["auth"], "base_url": "" if hide else c.get("base_url", ""),
                    "model": c.get("model", ""), "small_model": c.get("small_model", ""),
                    "by": "" if hide else c.get("connected_by", ""), "at": c.get("connected_at"),
                    # 3.2.2: the key's tail is the admin's business (Setup › Engines)
                    "masked": ("…" + k["last4"]) if is_admin and k and k.get("last4") else None}
        engines.append({
            "id": e["id"], "label": e["label"], "vendor": e["vendor"], "avatar": e["avatar"],
            "auths": e["auths"], "bridge": e["bridge"], "blurb": e["blurb"],
            "default_base_url": default_base_url(e), "zone": zone_of(e["id"]),
            "allowed": ok, "connected": connected(e["id"]), "connection": conn,
            "recommended": bool(e.get("recommended")),
            "preselected": m == "personal" and bool(e.get("recommended")),
            "is_default": d.get("default") == e["id"],
            "crew_value": f"engine:{e['id']}",
            # 3.2.2 Setup › Engines: the card carries the instance key(s) of its provider for
            # the admin — the same records as /api/admin/model-keys (one store, modelkeys)
            "keys": engine_keys(e["id"]) if is_admin else []})
    st = llm.status()
    return {"mode": m, "engines": engines, "default": d.get("default"),
            "welcome_url": welcome_url() if m == "personal" else "",
            "login_note": LOGIN_NOTE, "zones": list(ZONES),
            "policy": p if (is_admin or m == "governed") else None,
            "can_admin": is_admin, "operator_managed": bool(st.get("operator_managed")),
            "llm": {"mode": st.get("mode"), "configured": st.get("configured")},
            "project": {"slug": project, "engine": (d.get("project_engines") or {}).get(project or ""),
                        "can_choose": bool(m == "governed" and p["per_project"] and project_role
                                           in ("maintainer", "admin"))}}
