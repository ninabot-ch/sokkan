"""Setup › Secrets — state of the secrets provider and « Test connection » (instance admin)."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

import audit
import auth
import iam
import secrets_provider

router = APIRouter()
_last: dict = {}


def _admin(user: dict = Depends(auth.current_user)) -> dict:
    if iam.rank(user.get("role") or "") < iam.rank("admin"):
        raise HTTPException(403, "role 'admin' required")
    return user


@router.get("/api/admin/secrets-provider")
def provider_status(_u: dict = Depends(_admin)) -> dict:
    """Selected provider, why, its non-secret configuration, warnings, last test (no secret,
    no token, no key — names of files at most)."""
    return {**secrets_provider.status(), "last_test": _last.get("result")}


@router.post("/api/admin/secrets-provider/test")
def provider_test(u: dict = Depends(_admin)) -> dict:
    """Live health check: OpenBao reachable, unsealed, authenticated, transit round trip, KV
    listable, data keys unwrappable — or the key files of the file provider."""
    t0 = time.time()
    try:
        h = secrets_provider.active().health().as_dict()
    except secrets_provider.SecretsError as e:
        h = {"ok": False, "provider": secrets_provider.selected()[0], "detail": str(e),
             "checks": {}, "checked_at": time.time()}
    h["duration_ms"] = int((time.time() - t0) * 1000)
    _last["result"] = h
    audit.log(u["email"], "secrets.test", h["provider"], "ok" if h["ok"] else h["detail"][:200])
    return h
