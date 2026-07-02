#!/usr/bin/env python3
"""session.py — SOKKAN : session applicative (cookie JWT HS256) après login OIDC/LDAPS."""
from __future__ import annotations

import os
import time

import jwt
from fastapi import Request

SECRET = os.environ.get("SOKKAN_SESSION_SECRET", "")
if not SECRET:  # self-host : secret généré au 1er boot et persisté dans le data dir
    import secrets as _secrets
    _sf = os.path.join(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "session-secret")
    try:
        SECRET = open(_sf).read().strip()
    except OSError:
        SECRET = _secrets.token_hex(32)
        os.makedirs(os.path.dirname(_sf), exist_ok=True)
        with open(_sf, "w") as f:
            f.write(SECRET)
        os.chmod(_sf, 0o600)
COOKIE = "sokkan_session"
TTL = 24 * 3600


def make(email: str, name: str = "") -> str:
    now = int(time.time())
    return jwt.encode(
        {"email": email.lower(), "name": name or email, "iat": now, "exp": now + TTL},
        SECRET, algorithm="HS256",
    )


def email_from_request(request: Request) -> str | None:
    tok = request.cookies.get(COOKIE)
    if not tok or not SECRET:
        return None
    try:
        return (jwt.decode(tok, SECRET, algorithms=["HS256"]).get("email") or "").lower() or None
    except Exception:  # noqa: BLE001 — cookie absent/expiré/altéré
        return None
