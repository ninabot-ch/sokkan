#!/usr/bin/env python3
"""modelkeys.py — SOKKAN 3.2 (lot 7, `byok_admin`; also used by `connect_ai`): the model
provider keys of the instance, stored ENCRYPTED.

* encrypted with the data key of the `vault` context (Fernet; 3.3: through the secrets
  provider — vault.key in file mode, wrapped by OpenBao transit in openbao mode), file
  ``$SOKKAN_DATA_DIR/modelkeys.json`` (0600) — never in llm.json, never in a log;
* a key is never served back: the API shows ``…abcd`` (4 last characters), when and who;
* a key is keyed by ``(scope, provider)``. Scope is ``instance`` today; ``project:<slug>``
  is reserved for BYOK per project (decision of 07.10: per instance for the POC) and
  refused until it ships;
* sessions get the key through the existing mechanism: llm.json holds a REFERENCE
  (``"key_ref": "instance:anthropic"``) that `llm.load()` resolves in memory;
* when a SOKKAN gateway is configured (SOKKAN_GATEWAY_URL or SOKKAN_INFER_BASE_URL +
  SOKKAN_GATEWAY_ADMIN_TOKEN + SOKKAN_GATEWAY_CLIENT), an Anthropic key is pushed to its
  BYOK endpoint (``PUT /admin/tenant/{client}/byok``) and removed with it
  (``DELETE /admin/tenant/{client}/byok/anthropic``).

The validity test calls the provider's model list with the key and reports ok / the HTTP
status only — the key, the headers and the provider's error body are never logged nor
returned.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

PROVIDERS: dict[str, dict] = {
    "anthropic": {"label": "Anthropic", "hint": "sk-ant-…", "sessions": True,
                  "test_url": "https://api.anthropic.com/v1/models",
                  "test_headers": lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}},
    "openai": {"label": "OpenAI", "hint": "sk-…", "sessions": False,
               "test_url": "https://api.openai.com/v1/models",
               "test_headers": lambda k: {"authorization": f"Bearer {k}"}},
    "gemini": {"label": "Google Gemini", "hint": "AIza…", "sessions": False,
               "test_url": "https://generativelanguage.googleapis.com/v1beta/models",
               "test_headers": lambda k: {"x-goog-api-key": k}},
    "openrouter": {"label": "OpenRouter", "hint": "sk-or-…", "sessions": False,
                   "test_url": "https://openrouter.ai/api/v1/key",
                   "test_headers": lambda k: {"authorization": f"Bearer {k}"}},
    # 3.4.4: tested against the Anthropic door (SOKKAN Inference) with the key — /usage
    # answers 401 to a wrong key (before: a public catalogue, so any key was « valid »)
    "sokkan_router": {"label": "SOKKAN Router", "hint": "SOKKAN inference key (sik_…)",
                      "sessions": False, "test_url": "", "test_path": "/usage",
                      "default_base_env": "SOKKAN_ROUTER_URL",
                      "default_base": "https://infer.sokkan.ch",
                      "test_headers": lambda k: {"x-api-key": k}},
    "claude_login": {"label": "Claude login (setup-token)", "hint": "token from claude setup-token",
                     "sessions": True, "test_url": "", "test_headers": lambda k: {}},
    "custom": {"label": "Other (Anthropic-compatible endpoint)", "hint": "endpoint key",
               "sessions": False, "test_url": "",
               "test_headers": lambda k: {"x-api-key": k, "authorization": f"Bearer {k}"}},
}
SCOPES_LIVE = ("instance",)
_SCOPE_RE = re.compile(r"^(instance|project:[a-z0-9][a-z0-9-]{0,40})$")
_lock = threading.Lock()

# HTTP client, replaced in tests (the gateway push and the validity test go through it)
_http = None


def _client():
    if _http is not None:
        return _http
    import httpx
    return httpx


def _data_dir() -> str:
    return os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan"))


def store_path() -> str:
    return os.environ.get("SOKKAN_MODELKEYS_STORE") or os.path.join(_data_dir(), "modelkeys.json")


def _fernet():
    import secrets_provider
    return secrets_provider.active().fernet("vault")


def reencrypt() -> int:
    """Data-key rotation (scripts/secrets-rotate.py): every key re-encrypted with the primary."""
    from cryptography.fernet import InvalidToken
    f = _fernet()
    n = 0
    with _lock:
        d = _load()
        for _scope, recs in d.items():
            if not isinstance(recs, dict):
                continue
            for _prov, rec in recs.items():
                if isinstance(rec, dict) and rec.get("ct"):
                    try:
                        rec["ct"] = f.rotate(rec["ct"].encode()).decode()
                        n += 1
                    except InvalidToken:
                        continue
        if n:
            _save(d)
    return n


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


class KeyError_(ValueError):
    """A request a human can fix (400)."""


def check_scope(scope: str) -> str:
    scope = (scope or "instance").strip()
    if not _SCOPE_RE.match(scope):
        raise KeyError_("scope: instance | project:<slug>")
    if scope not in SCOPES_LIVE:
        raise KeyError_("per-project keys are planned (BYOK per instance for now)")
    return scope


def check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise KeyError_(f"provider: one of {', '.join(PROVIDERS)}")
    return provider


def mask(key: str) -> str:
    return "…" + key[-4:] if len(key) >= 8 else "…"


def ref(scope: str, provider: str) -> str:
    return f"{scope}:{provider}"


def set_key(scope: str, provider: str, key: str, by: str, base_url: str = "") -> dict:
    scope, provider = check_scope(scope), check_provider(provider)
    key = (key or "").strip()
    if len(key) < 8 or len(key) > 4096 or any(c.isspace() for c in key):
        raise KeyError_("key: 8 to 4096 characters, no spaces")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise KeyError_("base_url must start with http(s)://")
    rec = {"ct": _fernet().encrypt(key.encode()).decode(), "last4": key[-4:] if len(key) >= 8 else "",
           "set_by": by, "set_at": time.time(), "base_url": base_url.rstrip("/"),
           "tested_at": None, "test_ok": None, "test_detail": "",
           "pushed_at": None, "push_error": ""}
    with _lock:
        d = _load()
        d.setdefault(scope, {})[provider] = rec
        _save(d)
    return public(scope, provider, rec)


def delete_key(scope: str, provider: str) -> bool:
    scope, provider = check_scope(scope), check_provider(provider)
    with _lock:
        d = _load()
        gone = d.get(scope, {}).pop(provider, None) is not None
        if gone:
            _save(d)
    return gone


def get_plain(key_ref: str) -> str | None:
    """The key behind a reference (``instance:anthropic``) — in memory only, for a session."""
    scope, _, provider = (key_ref or "").rpartition(":")
    rec = _load().get(scope, {}).get(provider)
    if not rec:
        return None
    try:
        return _fernet().decrypt(rec["ct"].encode()).decode()
    except Exception:  # noqa: BLE001 — a wrong vault key = no key, never a crash
        return None


def record(scope: str, provider: str) -> dict | None:
    return _load().get(scope, {}).get(provider)


def _update(scope: str, provider: str, **fields) -> None:
    with _lock:
        d = _load()
        rec = d.get(scope, {}).get(provider)
        if rec is not None:
            rec.update(fields)
            _save(d)


def public(scope: str, provider: str, rec: dict) -> dict:
    """What the cockpit may see of a key: never the key."""
    return {"scope": scope, "provider": provider, "label": PROVIDERS[provider]["label"],
            "masked": "…" + rec.get("last4", "") if rec.get("last4") else "…",
            "set_by": rec.get("set_by", ""), "set_at": rec.get("set_at"),
            "base_url": rec.get("base_url", ""),
            "tested_at": rec.get("tested_at"), "test_ok": rec.get("test_ok"),
            "test_detail": rec.get("test_detail", ""),
            "pushed_at": rec.get("pushed_at"), "push_error": rec.get("push_error", "")}


def list_keys() -> list[dict]:
    out = []
    for scope, provs in sorted(_load().items()):
        for provider, rec in sorted(provs.items()):
            if provider in PROVIDERS:
                out.append(public(scope, provider, rec))
    return out


def testable(provider: str, rec: dict | None = None) -> bool:
    """A validity test exists: a fixed URL, a default base (SOKKAN Router), or the key's
    own base URL (custom endpoints)."""
    spec = PROVIDERS[provider]
    return bool(spec["test_url"] or spec.get("default_base") or (rec or {}).get("base_url"))


def test_key(scope: str, provider: str) -> dict:
    """Optional validity test: one GET with the key. Records ok/status, returns it. Never
    logs nor returns the key or the provider's response body."""
    scope, provider = check_scope(scope), check_provider(provider)
    key = get_plain(ref(scope, provider))
    if not key:
        raise KeyError_("no key for that provider")
    spec = PROVIDERS[provider]
    url = spec["test_url"]
    rec = record(scope, provider) or {}
    if not url:
        base = rec.get("base_url") or (
            (os.environ.get(spec.get("default_base_env", "")) or "").strip()
            or spec.get("default_base", ""))
        if base:
            url = base.rstrip("/") + spec.get("test_path", "/v1/models")
    if not url:
        res = {"ok": None, "detail": "no test available for this provider"}
    else:
        try:
            r = _client().get(url, headers=spec["test_headers"](key), timeout=10)
            ok = 200 <= r.status_code < 300
            res = {"ok": ok, "detail": "valid" if ok else
                   ("rejected (HTTP %d)" % r.status_code if r.status_code in (401, 403)
                    else "provider answered HTTP %d" % r.status_code)}
        except Exception as e:  # noqa: BLE001 — the message may carry the URL, not the key
            res = {"ok": None, "detail": f"provider unreachable ({type(e).__name__})"}
    _update(scope, provider, tested_at=time.time(), test_ok=res["ok"], test_detail=res["detail"])
    return res


# ---- SOKKAN gateway BYOK push ------------------------------------------------------------

def gateway() -> dict | None:
    url = (os.environ.get("SOKKAN_GATEWAY_URL") or os.environ.get("SOKKAN_INFER_BASE_URL") or "").strip()
    tok = (os.environ.get("SOKKAN_GATEWAY_ADMIN_TOKEN") or "").strip()
    client = (os.environ.get("SOKKAN_GATEWAY_CLIENT") or "").strip()
    if not (url and tok and client):
        return None
    return {"url": url.rstrip("/"), "token": tok, "client": client}


def gateway_public() -> dict:
    g = gateway()
    return {"configured": bool(g), "client": g["client"] if g else "",
            "url": g["url"] if g else ""}


def push_gateway(scope: str, provider: str, delete: bool = False) -> dict:
    """Push (or remove) the instance's Anthropic key at the gateway. No gateway = no-op."""
    g = gateway()
    if g is None or provider != "anthropic" or scope != "instance":
        return {"pushed": False, "detail": "no gateway configured" if g is None else "not pushed"}
    from urllib.parse import quote
    base = f"{g['url']}/admin/tenant/{quote(g['client'], safe='')}/byok"
    hdr = {"authorization": f"Bearer {g['token']}"}
    try:
        if delete:
            r = _client().delete(f"{base}/anthropic", headers=hdr, timeout=10)
            ok = r.status_code in (200, 204, 404)
        else:
            key = get_plain(ref(scope, provider)) or ""
            r = _client().put(base, json={"key": key}, headers=hdr, timeout=10)
            ok = 200 <= r.status_code < 300
        detail = "ok" if ok else f"gateway answered HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"gateway unreachable ({type(e).__name__})"
    if not delete:
        _update(scope, provider, pushed_at=time.time() if ok else None,
                push_error="" if ok else detail)
    return {"pushed": ok, "detail": detail}
