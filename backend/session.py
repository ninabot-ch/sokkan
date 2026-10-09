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


def _ttl() -> int:
    """Durée de vie d'une session de cockpit : 8 h par défaut depuis 3.2 (24 h avant) —
    un compte désactivé dans l'IdP perd l'accès au plus tard à la fin de sa journée.
    SOKKAN_SESSION_TTL_S, borné à [5 min, 24 h]."""
    try:
        v = int(os.environ.get("SOKKAN_SESSION_TTL_S") or 8 * 3600)
    except ValueError:
        v = 8 * 3600
    return max(300, min(v, 24 * 3600))


TTL = _ttl()


def make(email: str, name: str = "", sid: str = "") -> str:
    """`sid` = the IdP's session id (OIDC `sid` claim) when it sent one: an OIDC
    back-channel / front-channel logout of that IdP session then ends this cookie only."""
    now = int(time.time())
    claims = {"email": email.lower(), "name": name or email, "iat": now, "exp": now + TTL}
    if sid:
        claims["sid"] = str(sid)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _claims(tok: str | None) -> dict | None:
    if not tok or not SECRET:
        return None
    try:
        return jwt.decode(tok, SECRET, algorithms=["HS256"])
    except Exception:  # noqa: BLE001 — cookie absent/expiré/altéré
        return None


def sid_from_cookies(cookies) -> str:
    """The IdP session id carried by a cockpit cookie ('' if none / invalid)."""
    try:
        c = _claims(cookies.get(COOKIE))
    except Exception:  # noqa: BLE001
        c = None
    return str((c or {}).get("sid") or "")


def name_from_request(request: Request) -> str:
    """The display name the login carried ('' if none, or if it is just the address)."""
    c = _claims(request.cookies.get(COOKIE)) or {}
    n = str(c.get("name") or "").strip()
    return "" if "@" in n else n


def email_from_request(request: Request) -> str | None:
    claims = _claims(request.cookies.get(COOKIE))
    if claims is None:
        return None
    # 3.2 : la durée se compte depuis l'émission avec la TTL EN VIGUEUR — un cookie de 24 h
    # émis par une 3.1 ne survit pas à la mise à jour au-delà de la nouvelle limite
    if int(time.time()) - int(claims.get("iat") or 0) > TTL:
        return None
    email = (claims.get("email") or "").lower() or None
    # 3.2 lot 6 : « Revoke now » / SCIM — un cookie émis avant la révocation ne vaut plus rien
    import revocation
    if email and not revocation.cookie_ok(email, claims.get("iat") or 0):
        return None
    # 3.4 : OIDC back-channel / front-channel logout of the IdP session this cookie came from
    if email and claims.get("sid") and not revocation.sid_ok(str(claims["sid"])):
        return None
    return email
