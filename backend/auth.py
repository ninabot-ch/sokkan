#!/usr/bin/env python3
"""auth.py — SOKKAN : abstraction d'authentification (provider par instance).

`SOKKAN_AUTH_MODE` choisit le provider :
- `local` (défaut, self-host) : single-user. Si `SOKKAN_LOCAL_TOKEN` est défini,
  un login par token pose le cookie de session ; sinon l'accès est ouvert et
  l'identité = `SOKKAN_OWNER_EMAIL` (instance sur réseau de confiance).
- `cf-access` : identité = JWT Cloudflare Access validé.
- `oidc`      : login OIDC (Authentik, Keycloak, …) → session cookie.
- `ldaps`     : bind LDAPS → session cookie (à venir).

`current_user(request)` est utilisable comme dépendance FastAPI.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request

import iam

MODE = os.environ.get("SOKKAN_AUTH_MODE", "local").strip()
DEV_USER = os.environ.get("SOKKAN_DEV_USER", "")
OWNER_EMAIL = os.environ.get("SOKKAN_OWNER_EMAIL", "owner@localhost")
OWNER_NAME = os.environ.get("SOKKAN_OWNER_NAME", "Owner")
LOCAL_TOKEN = os.environ.get("SOKKAN_LOCAL_TOKEN", "")


def _cf_token(request: Request) -> str | None:
    tok = request.headers.get("cf-access-jwt-assertion")
    if tok:
        return tok
    for part in (request.headers.get("cookie") or "").split(";"):
        part = part.strip()
        if part.startswith("CF_Authorization="):
            return part[len("CF_Authorization="):]
    return None


def _email_cf_access(request: Request) -> str:
    import cfaccess

    # identité = JWT CF Access vérifié cryptographiquement (header email NON fiable).
    token = _cf_token(request)
    if cfaccess.ENABLED and token:
        try:
            email = cfaccess.validate(token)
        except Exception:  # noqa: BLE001
            raise HTTPException(401, "CF Access JWT invalide")
        if not email:
            raise HTTPException(401, "CF Access JWT sans email")
        return email
    return DEV_USER or OWNER_EMAIL  # accès loopback direct (dev) sans token


def _email_session(request: Request) -> str:
    import session

    email = session.email_from_request(request)
    if not email:
        raise HTTPException(401, "login requis")
    return email


def _email_local(request: Request) -> str:
    if not LOCAL_TOKEN:
        return OWNER_EMAIL  # pas de token configuré → single-user ouvert
    return _email_session(request)  # cookie posé par POST /api/auth/local


def resolve_email(request: Request) -> str:
    if MODE == "local":
        return _email_local(request)
    if MODE == "cf-access":
        return _email_cf_access(request)
    if MODE in ("oidc", "ldaps"):
        return _email_session(request)
    raise HTTPException(500, f"SOKKAN_AUTH_MODE inconnu: {MODE}")


def current_user(request: Request) -> dict:
    user = iam.get_user(resolve_email(request))
    if not user["known"] and iam.DEFAULT_ROLE == "none":
        raise HTTPException(403, "account not provisioned on this instance")
    return user


def auth_info() -> dict:
    """Info pour le frontend : faut-il afficher /login, et lequel ?"""
    return {
        "mode": MODE,
        "login_required": MODE in ("oidc", "ldaps") or (MODE == "local" and bool(LOCAL_TOKEN)),
    }
